from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Literal
import traceback

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from backend import (
    get_group_plan,
    resume_group_plan,
    resume_travel_agent,
    run_group_plan,
    run_travel_agent,
)
import group_store as store
from group_logic import APPROVAL_RULES, FOOD_PREFERENCES, MAX_REVISION_ROUNDS, merge_members

# This is kept from the original project to allow the existing synchronous
# agent functions to call async MCP helpers inside FastAPI.
import nest_asyncio

nest_asyncio.apply()

BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(
    title="TripMate AI",
    description=(
        "LangGraph Multi-Agent Travel Planner with Supervisor, Guardrails, "
        "Human-in-the-Loop, and FastAPI Frontend"
    ),
    version="2.0.0",
)

app.mount(
    "/static",
    StaticFiles(directory=str(BASE_DIR / "static")),
    name="static",
)

templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


class TravelRequest(BaseModel):
    message: str
    thread_id: str | None = None


class ApprovalRequest(BaseModel):
    thread_id: str = Field(min_length=1)
    approved: bool
    feedback: str = ""


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={},
    )


@app.post("/api/travel")
async def travel_planner(request_data: TravelRequest):
    try:
        user_message = request_data.message.strip()

        if not user_message:
            return JSONResponse(
                status_code=400,
                content={
                    "success": False,
                    "error": "Message cannot be empty.",
                },
            )

        result = run_travel_agent(
            user_input=user_message,
            thread_id=request_data.thread_id,
        )

        return JSONResponse(
            content={
                "success": True,
                **result,
            }
        )

    except Exception as exc:
        print("ERROR:", exc)
        traceback.print_exc()

        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "error": str(exc),
            },
        )


@app.post("/api/travel/approve")
async def approve_travel_plan(request_data: ApprovalRequest):
    try:
        if not request_data.approved and not request_data.feedback.strip():
            return JSONResponse(
                status_code=400,
                content={
                    "success": False,
                    "error": "Please provide revision feedback when rejecting the draft.",
                },
            )

        result = resume_travel_agent(
            thread_id=request_data.thread_id,
            approved=request_data.approved,
            feedback=request_data.feedback,
        )

        return JSONResponse(
            content={
                "success": True,
                **result,
            }
        )

    except Exception as exc:
        print("APPROVAL ERROR:", exc)
        traceback.print_exc()

        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "error": str(exc),
            },
        )


@app.get("/health")
async def health_check():
    return {
        "status": "ok",
        "message": "TripMate AI API is running",
        "features": [
            "supervisor_agent",
            "input_guardrail",
            "human_in_the_loop",
            "group_trip_mode",
        ],
    }


@app.get("/favicon.ico")
async def favicon():
    return JSONResponse(content={})


# =====================================================================
# Group trip mode
#
# These endpoints are plain `def` (not `async def`), so FastAPI runs them
# in worker threads. While one group's plan is being generated, other
# members can still load the page and see live status.
# =====================================================================
store.setup()

FoodPref = Literal["no_preference", "vegetarian", "jain", "vegan", "non_vegetarian"]
STALE_AFTER = timedelta(minutes=15)


class MemberIn(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    origin: str | None = Field(default=None, max_length=60)
    budget_max: int | None = Field(default=None, gt=0, le=10_000_000)
    available_from: date | None = None
    available_to: date | None = None
    food_pref: FoodPref = "no_preference"
    must_see: str | None = Field(default=None, max_length=500)
    notes: str | None = Field(default=None, max_length=300)


class TripCreate(BaseModel):
    title: str = Field(min_length=1, max_length=80)
    description: str = Field(min_length=5, max_length=600)
    trip_days: int | None = Field(default=None, ge=1, le=30)
    approval_rule: Literal["all", "majority"] = "all"
    organiser: MemberIn


class JoinRequest(MemberIn):
    member_id: str | None = None  # present = update my details


class PlanRequest(BaseModel):
    member_id: str = Field(min_length=1)


class VoteRequest(BaseModel):
    member_id: str = Field(min_length=1)
    decision: Literal["approve", "revise"]
    feedback: str = Field(default="", max_length=600)


def _error(message: str, status_code: int = 400) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"success": False, "error": message})


def _recover_if_stale(trip: dict) -> dict:
    """A server restart mid-plan would leave a trip stuck; release it."""
    if trip["status"] not in (store.PLANNING, store.PROCESSING):
        return trip
    updated = datetime.fromisoformat(trip["updated_at"])
    if datetime.now(timezone.utc) - updated < STALE_AFTER:
        return trip
    fallback = store.COLLECTING if trip["status"] == store.PLANNING else store.AWAITING_VOTES
    store.set_status(trip["trip_id"], fallback)
    return store.get_trip(trip["trip_id"])


def _member_for_plan(m: dict) -> dict:
    """The details agents see. member_id is a private key, so it's left out."""
    return {k: m.get(k) for k in store.MEMBER_FIELDS}


def _trip_view(trip_id: str, member_id: str | None) -> JSONResponse:
    trip = store.get_trip(trip_id)
    if trip is None:
        return _error("Trip not found.", 404)
    trip = _recover_if_stale(trip)
    members = store.get_members(trip_id)
    me = next((m for m in members if m["member_id"] == member_id), None)

    if me is None:
        # Not a member yet: show only what's needed to decide to join.
        return JSONResponse(content={
            "success": True,
            "trip": {
                "trip_id": trip_id,
                "title": trip["title"],
                "description": trip["description"],
                "status": trip["status"],
                "can_join": trip["status"] in store.EDITABLE_STATUSES,
            },
            "you": None,
            "members": [{"name": m["name"], "is_organiser": m["is_organiser"]} for m in members],
        })

    public_members = [
        {
            **_member_for_plan(m),
            "food_label": FOOD_PREFERENCES.get(m["food_pref"], m["food_pref"]),
            "is_organiser": m["is_organiser"],
            "is_you": m["member_id"] == member_id,
        }
        for m in members
    ]
    preview = merge_members([_member_for_plan(m) for m in members], trip["trip_days"])

    plan = get_group_plan(trip["thread_id"]) if trip["thread_id"] else None
    round_no = trip["current_round"]
    votes = store.votes_for_round(trip_id, round_no) if round_no else []
    voted_names = {v["name"] for v in votes}

    return JSONResponse(content={
        "success": True,
        "trip": {
            **{k: trip[k] for k in ("trip_id", "title", "description", "trip_days",
                                    "approval_rule", "status", "current_round")},
            "approval_rule_label": APPROVAL_RULES[trip["approval_rule"]],
            "max_revisions": MAX_REVISION_ROUNDS,
            "can_edit": trip["status"] in store.EDITABLE_STATUSES,
        },
        "you": {
            "name": me["name"],
            "is_organiser": me["is_organiser"],
            "details": _member_for_plan(me),
            "my_vote": next((v for v in votes if v["name"] == me["name"]), None),
        },
        "members": public_members,
        "preview": preview,
        "plan": plan,
        "votes": votes,
        "waiting_for": [m["name"] for m in members if round_no and m["name"] not in voted_names],
    })


@app.get("/group", response_class=HTMLResponse)
def group_home(request: Request):
    return templates.TemplateResponse(request=request, name="group.html", context={})


@app.get("/trip/{trip_id}", response_class=HTMLResponse)
def group_trip_page(request: Request, trip_id: str):
    return templates.TemplateResponse(request=request, name="group.html", context={})


@app.post("/api/trips")
def create_group_trip(data: TripCreate):
    try:
        trip_id, member_id = store.create_trip(
            data.title, data.description, data.trip_days,
            data.approval_rule, data.organiser.model_dump(),
        )
    except store.TripError as exc:
        return _error(str(exc), exc.status_code)
    return {"success": True, "trip_id": trip_id, "member_id": member_id,
            "join_url": f"/trip/{trip_id}"}


@app.post("/api/trips/{trip_id}/members")
def join_or_update_member(trip_id: str, data: JoinRequest):
    details = data.model_dump(exclude={"member_id"})
    try:
        if data.member_id:
            store.update_member(trip_id, data.member_id, details)
            member_id = data.member_id
        else:
            if store.get_trip(trip_id) is None:
                return _error("Trip not found.", 404)
            member_id = store.add_member(trip_id, details)
    except store.TripError as exc:
        return _error(str(exc), exc.status_code)
    return {"success": True, "member_id": member_id}


@app.get("/api/trips/{trip_id}")
def get_group_trip(trip_id: str, member_id: str | None = None):
    return _trip_view(trip_id, member_id)


@app.post("/api/trips/{trip_id}/plan")
def plan_group_trip(trip_id: str, data: PlanRequest):
    try:
        trip = store.claim_planning(trip_id, data.member_id)
    except store.TripError as exc:
        return _error(str(exc), exc.status_code)

    members = [_member_for_plan(m) for m in store.get_members(trip_id)]
    try:
        result = run_group_plan(
            trip_id=trip_id,
            thread_id=trip["thread_id"],
            description=f"{trip['title']}. {trip['description']}",
            members=members,
            approval_rule=trip["approval_rule"],
            trip_days=trip["trip_days"],
        )
    except Exception as exc:
        traceback.print_exc()
        store.set_status(trip_id, store.COLLECTING)
        return _error(f"Planning failed: {exc}. Try again.", 500)

    if result["requires_approval"]:
        store.set_status(trip_id, store.AWAITING_VOTES, current_round=1)
    elif not result["guardrail_allowed"]:
        store.set_status(trip_id, store.BLOCKED)
    elif result["group_rules"].get("conflicts"):
        store.set_status(trip_id, store.NEEDS_CHANGES)
    else:
        store.set_status(trip_id, store.COMPLETED)
    return _trip_view(trip_id, data.member_id)


@app.post("/api/trips/{trip_id}/vote")
def vote_on_group_trip(trip_id: str, data: VoteRequest):
    try:
        outcome = store.record_vote(trip_id, data.member_id, data.decision, data.feedback)
    except store.TripError as exc:
        return _error(str(exc), exc.status_code)

    if outcome["complete"]:
        # This request recorded the last vote, and the database lock made
        # sure it's the only one that continues the plan.
        votes = [
            {"name": v["name"], "decision": v["decision"], "feedback": v["feedback"]}
            for v in store.votes_for_round(trip_id, outcome["round"])
        ]
        try:
            result = resume_group_plan(outcome["thread_id"], votes)
        except Exception as exc:
            traceback.print_exc()
            store.set_status(trip_id, store.AWAITING_VOTES)
            return _error(
                f"Couldn't continue the plan: {exc}. Any member can vote again to retry.", 500
            )
        if result["requires_approval"]:
            store.set_status(trip_id, store.AWAITING_VOTES, current_round=outcome["round"] + 1)
        else:
            store.set_status(trip_id, store.COMPLETED)

    return _trip_view(trip_id, data.member_id)


if __name__ == "__main__":
    uvicorn.run(
        "app:app",
        host="127.0.0.1",
        port=8000,
        reload=True,
    )
