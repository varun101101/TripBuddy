from __future__ import annotations

import base64
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import jwt
import psycopg
from cryptography.hazmat.primitives.serialization import load_pem_public_key
from dotenv import load_dotenv
from fastapi import Request

from group_store import PLANNING, PROCESSING, TripError, _clean, _lock_trip, pool

load_dotenv(Path(__file__).resolve().parent / ".env")

SESSION_COOKIE = "__session"  # where Clerk keeps the token for same-site requests
CLOCK_LEEWAY_SECONDS = 10     # Clerk tokens last ~60 s; allow small clock differences

SCHEMA = [
    "ALTER TABLE trip_members ADD COLUMN IF NOT EXISTS clerk_user_id TEXT",
    """
    CREATE UNIQUE INDEX IF NOT EXISTS trip_members_one_per_clerk_user
        ON trip_members (trip_id, clerk_user_id) WHERE clerk_user_id IS NOT NULL
    """,
    "CREATE INDEX IF NOT EXISTS trip_members_by_clerk_user ON trip_members (clerk_user_id)",
    """
    CREATE TABLE IF NOT EXISTS solo_plans (
        thread_id      TEXT PRIMARY KEY,
        clerk_user_id  TEXT NOT NULL,
        title          TEXT NOT NULL,
        status         TEXT NOT NULL DEFAULT 'draft'
                       CHECK (status IN ('draft', 'final', 'blocked')),
        created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS solo_plans_by_user ON solo_plans (clerk_user_id, created_at DESC)",
]


def setup() -> None:
    """Create the account columns and tables. Run after group_store.setup()."""
    with pool().connection() as conn:
        for statement in SCHEMA:
            conn.execute(statement)


# ---------------------------------------------------------------------
# Clerk settings
# ---------------------------------------------------------------------
def publishable_key() -> str:
    return (os.getenv("CLERK_PUBLISHABLE_KEY") or "").strip()


def frontend_api(key: str | None = None) -> str | None:
    """
    Clerk's Frontend API host for this app, e.g. "happy-cat-12.clerk.accounts.dev".
    It is encoded inside the publishable key: pk_test_<base64(host + "$")>.
    """
    key = key if key is not None else publishable_key()
    parts = key.split("_", 2)
    if len(parts) != 3 or parts[0] != "pk":
        return None
    encoded = parts[2] + "=" * (-len(parts[2]) % 4)
    try:
        host = base64.b64decode(encoded).decode()
    except (ValueError, UnicodeDecodeError):
        return None
    return host[:-1] if host.endswith("$") else None


def enabled() -> bool:
    return frontend_api() is not None


def template_context() -> dict[str, Any]:
    """What every page needs to load Clerk in the browser."""
    host = frontend_api()
    return {
        "clerk_publishable_key": publishable_key() if host else "",
        "clerk_frontend_api": host or "",
    }


@lru_cache(maxsize=4)
def _jwks_client(host: str) -> jwt.PyJWKClient:
    # Clerk publishes its public keys here; PyJWKClient caches them.
    return jwt.PyJWKClient(f"https://{host}/.well-known/jwks.json", cache_keys=True)


@lru_cache(maxsize=4)
def _pem_key(pem: str):
    return load_pem_public_key(pem.encode())


def _signing_key(token: str):
    pem = (os.getenv("CLERK_JWT_KEY") or "").strip().replace("\\n", "\n")
    if pem:
        return _pem_key(pem)  # checked offline, no network call
    host = frontend_api()
    if not host:
        return None
    return _jwks_client(host).get_signing_key_from_jwt(token).key


def _allowed_origins(request: Request) -> set[str]:
    own = str(request.base_url).rstrip("/")
    extra = os.getenv("CLERK_AUTHORIZED_PARTIES") or ""
    return {own, *(o.strip().rstrip("/") for o in extra.split(",") if o.strip())}


def verify_session_token(token: str, request: Request) -> dict[str, Any] | None:
    """Return the token's claims if Clerk really issued it for this site, else None."""
    try:
        key = _signing_key(token)
        if key is None:
            return None
        claims = jwt.decode(
            token,
            key,
            algorithms=["RS256"],               # never accept other algorithms
            leeway=CLOCK_LEEWAY_SECONDS,        # also checks exp and nbf
            options={"require": ["exp", "sub"], "verify_aud": False},
        )
    except jwt.PyJWKClientError as exc:
        print(f"WARNING: couldn't fetch Clerk's public keys, treating request as a guest: {exc}")
        return None
    except jwt.PyJWTError:
        return None
    azp = claims.get("azp")
    if azp and azp.rstrip("/") not in _allowed_origins(request):
        return None  # token was made for a different website
    if claims.get("sts") == "pending":
        return None
    return claims


def current_user(request: Request) -> dict[str, Any] | None:
    """The signed-in Clerk user for this request ({"user_id": ...}), or None for guests."""
    if not enabled() and not os.getenv("CLERK_JWT_KEY"):
        return None
    header = request.headers.get("authorization") or ""
    token = header[7:].strip() if header.lower().startswith("bearer ") else ""
    token = token or request.cookies.get(SESSION_COOKIE) or ""
    if not token:
        return None
    claims = verify_session_token(token, request)
    return {"user_id": claims["sub"]} if claims else None


# ---------------------------------------------------------------------
# Group trips owned by a Clerk user
# ---------------------------------------------------------------------
def link_member(trip_id: str, member_id: str, user_id: str) -> bool:
    """
    Attach a guest membership (joined before signing in, on this browser)
    to the Clerk user. Only links a member that isn't linked yet, and only
    if this user has no other membership in the same trip.
    """
    try:
        with pool().connection() as conn:
            cur = conn.execute(
                """
                UPDATE trip_members SET clerk_user_id = %(u)s
                WHERE trip_id = %(t)s AND member_id = %(m)s AND clerk_user_id IS NULL
                  AND NOT EXISTS (SELECT 1 FROM trip_members
                                  WHERE trip_id = %(t)s AND clerk_user_id = %(u)s)
                """,
                {"t": trip_id, "m": member_id, "u": user_id},
            )
    except psycopg.errors.UniqueViolation:
        return False
    return cur.rowcount == 1


# ---------------------------------------------------------------------
# Solo plans saved for a Clerk user
# ---------------------------------------------------------------------
def save_plan(user_id: str, thread_id: str, title: str, status: str) -> None:
    title = " ".join((title or "").split())[:120] or "Travel plan"
    with pool().connection() as conn:
        conn.execute(
            """
            INSERT INTO solo_plans (thread_id, clerk_user_id, title, status)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (thread_id) DO UPDATE
                SET status = EXCLUDED.status, updated_at = now()
                WHERE solo_plans.clerk_user_id = EXCLUDED.clerk_user_id
            """,
            (thread_id, user_id, title, status),
        )


def update_plan_status(thread_id: str, status: str) -> None:
    with pool().connection() as conn:
        conn.execute(
            "UPDATE solo_plans SET status = %s, updated_at = now() WHERE thread_id = %s",
            (status, thread_id),
        )


def get_saved_plan(user_id: str, thread_id: str) -> dict[str, Any] | None:
    with pool().connection() as conn:
        row = conn.execute(
            "SELECT * FROM solo_plans WHERE thread_id = %s AND clerk_user_id = %s",
            (thread_id, user_id),
        ).fetchone()
    return _clean(row)


# ---------------------------------------------------------------------
# "My trips"
# ---------------------------------------------------------------------
def my_trips(user_id: str) -> dict[str, list[dict[str, Any]]]:
    with pool().connection() as conn:
        group_rows = conn.execute(
            """
            SELECT t.trip_id, t.title, t.status, t.trip_days, t.created_at, t.updated_at,
                   m.is_organiser,
                   (SELECT count(*) FROM trip_members x WHERE x.trip_id = t.trip_id) AS members
            FROM trip_members m JOIN trips t ON t.trip_id = m.trip_id
            WHERE m.clerk_user_id = %s
            ORDER BY t.updated_at DESC
            """,
            (user_id,),
        ).fetchall()
        plan_rows = conn.execute(
            """
            SELECT thread_id, title, status, created_at, updated_at
            FROM solo_plans WHERE clerk_user_id = %s
            ORDER BY created_at DESC
            """,
            (user_id,),
        ).fetchall()
    return {
        "group_trips": [_clean(r) for r in group_rows],
        "solo_plans": [_clean(r) for r in plan_rows],
    }


# ---------------------------------------------------------------------
# Deleting from "My trips"
# ---------------------------------------------------------------------
def delete_solo_plan(user_id: str, thread_id: str) -> bool:
    """Delete one of this user's saved solo plans. False if it isn't theirs."""
    with pool().connection() as conn:
        cur = conn.execute(
            "DELETE FROM solo_plans WHERE thread_id = %s AND clerk_user_id = %s",
            (thread_id, user_id),
        )
    return cur.rowcount == 1


def remove_group_trip(user_id: str, trip_id: str) -> tuple[str, list[str]]:
    """
    The organiser deletes the trip for everyone: the trip, its members and
    votes go (and the caller removes the plan threads returned). Any other
    member only takes it off their own "My trips"; they stay in the group,
    so the group's votes still work.

    Returns ("deleted", thread_ids) or ("removed", []).
    """
    with pool().connection() as conn:
        trip = _lock_trip(conn, trip_id)
        member = conn.execute(
            "SELECT member_id, is_organiser FROM trip_members WHERE trip_id = %s AND clerk_user_id = %s",
            (trip_id, user_id),
        ).fetchone()
        if member is None:
            raise TripError("This trip isn't in your account.", 404)

        if not member["is_organiser"]:
            conn.execute(
                "UPDATE trip_members SET clerk_user_id = NULL WHERE member_id = %s",
                (member["member_id"],),
            )
            return "removed", []

        if trip["status"] in (PLANNING, PROCESSING):
            raise TripError("The agents are working on this trip right now. Try again in a minute.", 409)
        conn.execute("DELETE FROM trips WHERE trip_id = %s", (trip_id,))  # members and votes cascade
        threads = [f"trip_{trip_id}_{n}" for n in range(1, trip["plan_attempts"] + 1)]
        return "deleted", threads
