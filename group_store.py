"""
Database access for group trips.

Three tables of TripBuddy's own sit beside LangGraph's checkpoint tables:

- trips          one row per group trip (status, approval rule, which
                 LangGraph thread holds its current plan)
- trip_members   one row per person in a trip (their budget, dates, food,
                 must-see places). member_id doubles as that person's
                 private key, so it is never shown to other members.
- member_votes   one row per member per voting round

trips.thread_id links a trip to the LangGraph checkpoints of its plan.
"""

from __future__ import annotations

import os
import secrets
from datetime import date, datetime
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from group_logic import APPROVAL_RULES, FOOD_PREFERENCES

# Status values a trip moves through.
COLLECTING = "collecting"        # members are joining / editing details
PLANNING = "planning"            # agents are writing the first draft
AWAITING_VOTES = "awaiting_votes"  # draft is ready, waiting for every vote
PROCESSING = "processing"        # last vote is in, graph is resuming
COMPLETED = "completed"          # final plan is ready
NEEDS_CHANGES = "needs_changes"  # hard conflict (e.g. no shared dates)
BLOCKED = "blocked"              # guardrail rejected the trip description

EDITABLE_STATUSES = (COLLECTING, NEEDS_CHANGES, BLOCKED)

SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS trips (
        trip_id        TEXT PRIMARY KEY,
        title          TEXT NOT NULL,
        description    TEXT NOT NULL,
        trip_days      INTEGER CHECK (trip_days BETWEEN 1 AND 30),
        approval_rule  TEXT NOT NULL DEFAULT 'all'
                       CHECK (approval_rule IN ('all', 'majority')),
        status         TEXT NOT NULL DEFAULT 'collecting',
        thread_id      TEXT,
        plan_attempts  INTEGER NOT NULL DEFAULT 0,
        current_round  INTEGER NOT NULL DEFAULT 0,
        created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS trip_members (
        member_id      TEXT PRIMARY KEY,
        trip_id        TEXT NOT NULL REFERENCES trips(trip_id) ON DELETE CASCADE,
        name           TEXT NOT NULL,
        is_organiser   BOOLEAN NOT NULL DEFAULT FALSE,
        origin         TEXT,
        budget_max     INTEGER CHECK (budget_max > 0),
        available_from DATE,
        available_to   DATE,
        food_pref      TEXT NOT NULL DEFAULT 'no_preference',
        must_see       TEXT,
        notes          TEXT,
        joined_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
        CHECK (available_from IS NULL OR available_to IS NULL
               OR available_from <= available_to)
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS trip_members_unique_name
        ON trip_members (trip_id, lower(name))
    """,
    """
    CREATE TABLE IF NOT EXISTS member_votes (
        trip_id    TEXT NOT NULL REFERENCES trips(trip_id) ON DELETE CASCADE,
        member_id  TEXT NOT NULL REFERENCES trip_members(member_id) ON DELETE CASCADE,
        round      INTEGER NOT NULL,
        decision   TEXT NOT NULL CHECK (decision IN ('approve', 'revise')),
        feedback   TEXT NOT NULL DEFAULT '',
        voted_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (trip_id, member_id, round)
    )
    """,
]

MEMBER_FIELDS = (
    "name", "origin", "budget_max", "available_from", "available_to",
    "food_pref", "must_see", "notes",
)


class TripError(Exception):
    """A request that breaks a trip rule. status_code is used by the API."""

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


def _database_url() -> str:
    url = os.getenv("DATABASE_URL")
    if not url:
        raise ValueError("DATABASE_URL is missing. Add it to your .env file.")
    if "sslmode=" not in url:
        url += ("&" if "?" in url else "?") + "sslmode=require"
    return url


_pool: ConnectionPool | None = None


def pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        _pool = ConnectionPool(
            _database_url(),
            min_size=1,
            max_size=5,
            kwargs={"row_factory": dict_row},
            open=True,
        )
    return _pool


def setup() -> None:
    with pool().connection() as conn:
        for statement in SCHEMA:
            conn.execute(statement)


def _clean(row: dict[str, Any] | None) -> dict[str, Any] | None:
    """Make a database row JSON-friendly (dates become ISO strings)."""
    if row is None:
        return None
    return {
        k: (v.isoformat() if isinstance(v, (date, datetime)) else v)
        for k, v in row.items()
    }


def _member_values(data: dict[str, Any]) -> dict[str, Any]:
    values = {field: data.get(field) for field in MEMBER_FIELDS}
    values["name"] = (values["name"] or "").strip()
    if not values["name"]:
        raise TripError("Please enter your name.")
    if values["food_pref"] not in FOOD_PREFERENCES:
        values["food_pref"] = "no_preference"
    for field in ("origin", "must_see", "notes"):
        values[field] = (values[field] or "").strip() or None
    start, end = values["available_from"], values["available_to"]
    if bool(start) != bool(end):
        raise TripError("Give both an available-from and an available-to date, or neither.")
    if start and end and str(start) > str(end):
        raise TripError("Your available-from date is after your available-to date.")
    return values


def _insert_member(conn, trip_id: str, values: dict[str, Any], is_organiser: bool,
                   user_id: str | None = None) -> str:
    """user_id is the signed-in Clerk user's ID (None for guests).
    The clerk_user_id column is added by accounts.setup()."""
    member_id = secrets.token_urlsafe(16)
    try:
        conn.execute(
            """
            INSERT INTO trip_members (member_id, trip_id, is_organiser, name, origin,
                budget_max, available_from, available_to, food_pref, must_see, notes, clerk_user_id)
            VALUES (%(member_id)s, %(trip_id)s, %(is_organiser)s, %(name)s, %(origin)s,
                %(budget_max)s, %(available_from)s, %(available_to)s, %(food_pref)s,
                %(must_see)s, %(notes)s, %(user_id)s)
            """,
            {**values, "member_id": member_id, "trip_id": trip_id,
             "is_organiser": is_organiser, "user_id": user_id},
        )
    except psycopg.errors.UniqueViolation:
        raise TripError(f"Someone called {values['name']} has already joined this trip. Use a different name.")
    return member_id


# ---------------------------------------------------------------------
# Trips and members
# ---------------------------------------------------------------------
def create_trip(title: str, description: str, trip_days: int | None,
                approval_rule: str, organiser: dict[str, Any],
                user_id: str | None = None) -> tuple[str, str]:
    title, description = (title or "").strip(), (description or "").strip()
    if not title or not description:
        raise TripError("Give the trip a title and a description.")
    if approval_rule not in APPROVAL_RULES:
        raise TripError("Unknown approval rule.")
    values = _member_values(organiser)
    trip_id = secrets.token_urlsafe(6)
    with pool().connection() as conn:
        conn.execute(
            """
            INSERT INTO trips (trip_id, title, description, trip_days, approval_rule)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (trip_id, title, description, trip_days, approval_rule),
        )
        member_id = _insert_member(conn, trip_id, values, is_organiser=True, user_id=user_id)
    return trip_id, member_id


def get_trip(trip_id: str) -> dict[str, Any] | None:
    with pool().connection() as conn:
        row = conn.execute("SELECT * FROM trips WHERE trip_id = %s", (trip_id,)).fetchone()
    return _clean(row)


def get_members(trip_id: str) -> list[dict[str, Any]]:
    with pool().connection() as conn:
        rows = conn.execute(
            "SELECT * FROM trip_members WHERE trip_id = %s ORDER BY joined_at, name",
            (trip_id,),
        ).fetchall()
    return [_clean(r) for r in rows]


def get_member(trip_id: str, member_id: str | None) -> dict[str, Any] | None:
    if not member_id:
        return None
    with pool().connection() as conn:
        row = conn.execute(
            "SELECT * FROM trip_members WHERE trip_id = %s AND member_id = %s",
            (trip_id, member_id),
        ).fetchone()
    return _clean(row)


def _lock_trip(conn, trip_id: str) -> dict[str, Any]:
    trip = conn.execute(
        "SELECT * FROM trips WHERE trip_id = %s FOR UPDATE", (trip_id,)
    ).fetchone()
    if trip is None:
        raise TripError("Trip not found.", 404)
    return trip


def add_member(trip_id: str, data: dict[str, Any], user_id: str | None = None) -> str:
    values = _member_values(data)
    with pool().connection() as conn:
        trip = _lock_trip(conn, trip_id)
        if user_id and conn.execute(
            "SELECT 1 FROM trip_members WHERE trip_id = %s AND clerk_user_id = %s",
            (trip_id, user_id),
        ).fetchone():
            raise TripError("Your account has already joined this trip.", 409)
        if trip["status"] not in EDITABLE_STATUSES:
            raise TripError("Planning has already started, so new members can't join this trip.", 409)
        return _insert_member(conn, trip_id, values, is_organiser=False, user_id=user_id)


def update_member(trip_id: str, member_id: str, data: dict[str, Any]) -> None:
    values = _member_values(data)
    with pool().connection() as conn:
        trip = _lock_trip(conn, trip_id)
        if trip["status"] not in EDITABLE_STATUSES:
            raise TripError("Details can't be changed while a plan is being made or voted on.", 409)
        try:
            cur = conn.execute(
                """
                UPDATE trip_members SET name = %(name)s, origin = %(origin)s,
                    budget_max = %(budget_max)s, available_from = %(available_from)s,
                    available_to = %(available_to)s, food_pref = %(food_pref)s,
                    must_see = %(must_see)s, notes = %(notes)s
                WHERE trip_id = %(trip_id)s AND member_id = %(member_id)s
                """,
                {**values, "trip_id": trip_id, "member_id": member_id},
            )
        except psycopg.errors.UniqueViolation:
            raise TripError(f"Someone called {values['name']} has already joined this trip.")
        if cur.rowcount == 0:
            raise TripError("Member not found.", 404)
        if trip["status"] != COLLECTING:
            conn.execute(
                "UPDATE trips SET status = %s, updated_at = now() WHERE trip_id = %s",
                (COLLECTING, trip_id),
            )


# ---------------------------------------------------------------------
# Planning and voting
# ---------------------------------------------------------------------
def claim_planning(trip_id: str, member_id: str) -> dict[str, Any]:
    """
    Atomically move a trip into 'planning' so only one plan runs at a time.
    Returns the updated trip row, including its new thread_id.
    """
    with pool().connection() as conn:
        trip = _lock_trip(conn, trip_id)
        organiser = conn.execute(
            "SELECT member_id FROM trip_members WHERE trip_id = %s AND is_organiser",
            (trip_id,),
        ).fetchone()
        if not organiser or organiser["member_id"] != member_id:
            raise TripError("Only the organiser can start planning.", 403)
        if trip["status"] not in EDITABLE_STATUSES:
            raise TripError("A plan is already being made or voted on for this trip.", 409)
        count = conn.execute(
            "SELECT count(*) AS n FROM trip_members WHERE trip_id = %s", (trip_id,)
        ).fetchone()["n"]
        if count < 2:
            raise TripError("A group trip needs at least 2 members. Share the link first.")
        attempt = trip["plan_attempts"] + 1
        thread_id = f"trip_{trip_id}_{attempt}"
        row = conn.execute(
            """
            UPDATE trips SET status = %s, plan_attempts = %s, thread_id = %s,
                current_round = 0, updated_at = now()
            WHERE trip_id = %s RETURNING *
            """,
            (PLANNING, attempt, thread_id, trip_id),
        ).fetchone()
    return _clean(row)


def set_status(trip_id: str, status: str, current_round: int | None = None) -> None:
    with pool().connection() as conn:
        if current_round is None:
            conn.execute(
                "UPDATE trips SET status = %s, updated_at = now() WHERE trip_id = %s",
                (status, trip_id),
            )
        else:
            conn.execute(
                """UPDATE trips SET status = %s, current_round = %s, updated_at = now()
                   WHERE trip_id = %s""",
                (status, current_round, trip_id),
            )


def votes_for_round(trip_id: str, round_no: int) -> list[dict[str, Any]]:
    with pool().connection() as conn:
        rows = conn.execute(
            """
            SELECT m.name, v.decision, v.feedback, v.voted_at
            FROM member_votes v JOIN trip_members m ON m.member_id = v.member_id
            WHERE v.trip_id = %s AND v.round = %s
            ORDER BY m.joined_at
            """,
            (trip_id, round_no),
        ).fetchall()
    return [_clean(r) for r in rows]


def record_vote(trip_id: str, member_id: str, decision: str, feedback: str) -> dict[str, Any]:
    """
    Save (or change) one member's vote for the current round.

    When the last missing vote arrives, the trip is switched to 'processing'
    inside the same locked transaction, so exactly one request resumes the
    graph even if two people vote at the same moment.
    """
    if decision not in ("approve", "revise"):
        raise TripError("Vote must be approve or revise.")
    feedback = (feedback or "").strip()
    if decision == "revise" and not feedback:
        raise TripError("Say what should change when you ask for changes.")
    with pool().connection() as conn:
        trip = _lock_trip(conn, trip_id)
        if trip["status"] != AWAITING_VOTES:
            raise TripError("There is no draft waiting for votes right now.", 409)
        member = conn.execute(
            "SELECT name FROM trip_members WHERE trip_id = %s AND member_id = %s",
            (trip_id, member_id),
        ).fetchone()
        if member is None:
            raise TripError("You are not a member of this trip.", 403)
        round_no = trip["current_round"]

        def count_votes():
            return conn.execute(
                """
                SELECT (SELECT count(*) FROM trip_members WHERE trip_id = %(t)s) AS members,
                       (SELECT count(*) FROM member_votes WHERE trip_id = %(t)s AND round = %(r)s) AS votes
                """,
                {"t": trip_id, "r": round_no},
            ).fetchone()

        # Once everyone has voted, the round is closed. The trip can only be
        # back in 'awaiting_votes' with a full round if continuing the plan
        # failed (e.g. an OpenRouter timeout). The graph may already have counted
        # these votes before failing, so a changed vote would be ignored by
        # the plan while the page showed it. Keep the votes as they were and
        # treat this request as a retry.
        counts = count_votes()
        if counts["votes"] < counts["members"]:
            conn.execute(
                """
                INSERT INTO member_votes (trip_id, member_id, round, decision, feedback)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (trip_id, member_id, round)
                DO UPDATE SET decision = EXCLUDED.decision, feedback = EXCLUDED.feedback,
                              voted_at = now()
                """,
                (trip_id, member_id, round_no, decision, feedback),
            )
            counts = count_votes()
        complete = counts["votes"] >= counts["members"]
        if complete:
            conn.execute(
                "UPDATE trips SET status = %s, updated_at = now() WHERE trip_id = %s",
                (PROCESSING, trip_id),
            )
    return {
        "complete": complete,
        "round": round_no,
        "thread_id": trip["thread_id"],
        "approval_rule": trip["approval_rule"],
        "votes_in": counts["votes"],
        "members": counts["members"],
    }
