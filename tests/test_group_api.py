"""
End-to-end tests for group trip mode: real PostgreSQL, real LangGraph graph,
fake AI model and fake MCP tools (so no API keys or quota are used).

Run with a throwaway database:
    TEST_DATABASE_URL=postgresql://user:pass@localhost/tripbuddy_test?sslmode=disable \
        python -m pytest tests/test_group_api.py -q
"""

import os
import sys
import threading

import pytest

TEST_DB = os.getenv("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DB, reason="set TEST_DATABASE_URL to run")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


class FakeLLM:
    """Answers like the real model would, and records every prompt it gets."""

    def __init__(self):
        self.prompts = []
        self.drafts = 0

    def invoke(self, messages):
        from langchain_core.messages import AIMessage

        if isinstance(messages, str):
            system, prompt = "", messages
        else:
            system, prompt = messages[0].content, messages[-1].content
        self.prompts.append((system, prompt))

        if "Extract only the destination" in prompt:
            text = "Manali"
        elif "input guardrail" in system:
            blocked = "hack" in prompt.lower()
            text = '{"allowed": %s, "reason": "%s"}' % (
                "false" if blocked else "true", "Not a travel request." if blocked else "")
        elif "route work" in system:
            text = ('{"selected_agents": ["hotel_agent", "weather_agent", "budget_agent", '
                    '"itinerary_agent"], "trip_constraints": {"destination": "Manali"}, '
                    '"reasoning": "Needs stay, weather and cost."}')
        elif "flight planner" in system:
            text = "Fly AMD to KUU."
        elif "budget analyst" in system:
            text = "Estimated cost fits."
        elif "expert travel planner" in system:
            self.drafts += 1
            text = f"DRAFT {self.drafts}"
            if "REVISION" in prompt:
                text += "\nWhat changed: cheaper hotel"
        elif "booking assistant" in system:
            text = "FINAL PLAN"
        else:
            text = "OK"
        return AIMessage(content=text)

    def count(self):
        return len(self.prompts)


@pytest.fixture(scope="module")
def env():
    os.environ["DATABASE_URL"] = TEST_DB
    os.environ["GROQ_API_KEY"] = "test-key"
    import backend
    import mcp_client

    fake = FakeLLM()
    backend.llm = fake
    mcp_client.llm = fake

    async def fake_search(query):
        return f"Hotels found for: {query}"

    async def fake_weather(city):
        return {"city": city, "temperature_c": 4}

    async def fake_aviation(tool, args=None):
        return {"ok": True, "data": []}

    backend.tavily_mcp_search = fake_search
    backend.weather_mcp_search = fake_weather
    backend.forecast_mcp_search = fake_weather
    backend.aviation_mcp_call = fake_aviation

    import app as app_module
    from fastapi.testclient import TestClient

    return {"client": TestClient(app_module.app), "llm": fake, "backend": backend}


def person(name, **kw):
    base = {"name": name, "food_pref": "no_preference"}
    base.update(kw)
    return base


def create_trip(client, rule="all", organiser=None, days=7, description="Snow, cafes and a relaxed pace."):
    r = client.post("/api/trips", json={
        "title": "Manali in December",
        "description": description,
        "trip_days": days,
        "approval_rule": rule,
        "organiser": organiser or person(
            "Varun", origin="Ahmedabad", budget_max=60000, available_from="2026-12-10",
            available_to="2026-12-20", food_pref="vegetarian", must_see="Solang Valley, Old Manali"),
    })
    assert r.status_code == 200, r.text
    body = r.json()
    return body["trip_id"], body["member_id"]


def join(client, trip_id, data):
    r = client.post(f"/api/trips/{trip_id}/members", json=data)
    assert r.status_code == 200, r.text
    return r.json()["member_id"]


def vote(client, trip_id, member_id, decision, feedback=""):
    return client.post(f"/api/trips/{trip_id}/vote",
                       json={"member_id": member_id, "decision": decision, "feedback": feedback})


def test_full_group_flow_with_revision(env):
    client, llm = env["client"], env["llm"]
    trip_id, varun = create_trip(client)
    riya = join(client, trip_id, person(
        "Riya", origin="Mumbai", budget_max=40000, available_from="2026-12-12",
        available_to="2026-12-18", food_pref="jain", must_see="Hadimba Temple, old manali"))
    aman = join(client, trip_id, person("Aman", origin="ahmedabad", budget_max=90000,
                                        food_pref="non_vegetarian"))

    # Live preview before any AI runs
    view = client.get(f"/api/trips/{trip_id}", params={"member_id": varun}).json()
    p = view["preview"]
    assert p["budget_per_person"] == 40000 and p["budget_set_by"] == ["Riya"]
    assert (p["window_start"], p["window_end"], p["window_days"]) == ("2026-12-12", "2026-12-18", 7)
    assert set(p["origins"]) == {"Ahmedabad", "Mumbai"}
    assert p["must_see"][0]["place"] == "Old Manali"
    assert "budget_max" in view["members"][0]
    assert all("member_id" not in m for m in view["members"])  # private keys never shared

    # Outsiders see names only
    outsider = client.get(f"/api/trips/{trip_id}").json()
    assert outsider["you"] is None and "budget_max" not in outsider["members"][0]

    # Only the organiser can plan
    r = client.post(f"/api/trips/{trip_id}/plan", json={"member_id": riya})
    assert r.status_code == 403

    before = llm.count()
    r = client.post(f"/api/trips/{trip_id}/plan", json={"member_id": varun})
    assert r.status_code == 200, r.text
    view = r.json()
    assert view["trip"]["status"] == "awaiting_votes"
    assert view["trip"]["current_round"] == 1
    assert view["plan"]["requires_approval"] is True
    assert view["plan"]["answer"] == "DRAFT 1"
    assert llm.count() > before
    supervisor_prompt = next(p for s, p in llm.prompts if "route work" in s)
    assert "GROUP TRIP: 3 travellers" in supervisor_prompt
    assert "₹40,000 per person, set by Riya" in supervisor_prompt
    assert view["plan"]["trip_constraints"]["budget"] == "INR 40000 per person"

    # Nobody can join or edit while voting is open
    r = client.post(f"/api/trips/{trip_id}/members", json=person("Late"))
    assert r.status_code == 409

    # Round 1: one asks for changes
    assert vote(client, trip_id, varun, "approve").status_code == 200
    assert vote(client, trip_id, riya, "revise").status_code == 400  # feedback required
    view = vote(client, trip_id, riya, "revise", "Cheaper hotel please").json()
    assert view["trip"]["status"] == "awaiting_votes"
    assert view["waiting_for"] == ["Aman"]
    view = vote(client, trip_id, aman, "approve").json()

    # Revision round 2 with Riya's request passed to the itinerary agent
    assert view["trip"]["status"] == "awaiting_votes", view
    assert view["trip"]["current_round"] == 2
    assert view["plan"]["answer"].startswith("DRAFT 2")
    revision_prompt = [p for s, p in llm.prompts if "expert travel planner" in s][-1]
    assert "REVISION 1 of 2" in revision_prompt
    assert "- Riya: Cheaper hotel please" in revision_prompt
    assert "DRAFT 1" in revision_prompt  # previous draft included

    # Round 2: everyone approves
    for m in (varun, riya):
        vote(client, trip_id, m, "approve")
    view = vote(client, trip_id, aman, "approve").json()
    assert view["trip"]["status"] == "completed"
    assert view["plan"]["answer"] == "FINAL PLAN"
    assert view["plan"]["vote_summary"].startswith("3 of 3 approved")
    final_prompt = [p for s, p in llm.prompts if "booking assistant" in s][-1]
    assert "8. Group Summary" in final_prompt

    # Voting is closed now
    assert vote(client, trip_id, varun, "approve").status_code == 409

    # The plan's steps really are in PostgreSQL
    import psycopg
    with psycopg.connect(TEST_DB) as conn:
        n = conn.execute("SELECT count(*) FROM checkpoints WHERE thread_id = %s",
                         (f"trip_{trip_id}_1",)).fetchone()[0]
        votes = conn.execute("SELECT count(*) FROM member_votes WHERE trip_id = %s",
                             (trip_id,)).fetchone()[0]
    assert n >= 10
    assert votes == 6


def test_conflict_stops_before_any_ai_call_then_replan(env):
    client, llm = env["client"], env["llm"]
    trip_id, varun = create_trip(client, organiser=person(
        "Varun", available_from="2026-12-01", available_to="2026-12-05"))
    neha = join(client, trip_id, person("Neha", available_from="2026-12-10",
                                        available_to="2026-12-15"))
    view = client.get(f"/api/trips/{trip_id}", params={"member_id": varun}).json()
    assert view["preview"]["conflicts"]

    before = llm.count()
    view = client.post(f"/api/trips/{trip_id}/plan", json={"member_id": varun}).json()
    assert llm.count() == before  # zero model calls
    assert view["trip"]["status"] == "needs_changes"
    assert "No dates work for everyone" in view["plan"]["answer"]

    # Neha fixes her dates; organiser plans again on a fresh thread
    r = client.post(f"/api/trips/{trip_id}/members", json={
        **person("Neha", available_from="2026-12-03", available_to="2026-12-08"), "member_id": neha})
    assert r.status_code == 200
    view = client.post(f"/api/trips/{trip_id}/plan", json={"member_id": varun}).json()
    assert view["trip"]["status"] == "awaiting_votes"
    assert view["plan"]["thread_id"] == f"trip_{trip_id}_2"
    assert view["plan"]["group_rules"]["window_days"] == 3


def test_guardrail_blocks_group_trip(env):
    client = env["client"]
    trip_id, varun = create_trip(client, description="How to hack my college wifi")
    join(client, trip_id, person("Neha"))
    view = client.post(f"/api/trips/{trip_id}/plan", json={"member_id": varun}).json()
    assert view["trip"]["status"] == "blocked"
    assert view["plan"]["guardrail_allowed"] is False


def test_majority_rule_finalises_and_passes_minor_requests(env):
    client, llm = env["client"], env["llm"]
    trip_id, varun = create_trip(client, rule="majority")
    b = join(client, trip_id, person("Bina"))
    c = join(client, trip_id, person("Chetan"))
    client.post(f"/api/trips/{trip_id}/plan", json={"member_id": varun})
    vote(client, trip_id, varun, "approve")
    vote(client, trip_id, b, "approve")
    view = vote(client, trip_id, c, "revise", "Add a rest day").json()
    assert view["trip"]["status"] == "completed"
    final_prompt = [p for s, p in llm.prompts if "booking assistant" in s][-1]
    assert "- Chetan: Add a rest day" in final_prompt
    assert "2 of 3 approved" in final_prompt


def test_revision_limit_then_final(env):
    client, llm = env["client"], env["llm"]
    trip_id, varun = create_trip(client)
    b = join(client, trip_id, person("Bina"))
    client.post(f"/api/trips/{trip_id}/plan", json={"member_id": varun})
    for round_no in (1, 2, 3):
        vote(client, trip_id, varun, "approve")
        view = vote(client, trip_id, b, "revise", f"change {round_no}").json()
        if round_no < 3:
            assert view["trip"]["status"] == "awaiting_votes"
            assert view["trip"]["current_round"] == round_no + 1
    assert view["trip"]["status"] == "completed"
    final_prompt = [p for s, p in llm.prompts if "booking assistant" in s][-1]
    assert "did not approve after 2 revisions" in final_prompt
    assert "- Bina: change 3" in final_prompt


def test_simultaneous_last_votes_resume_only_once(env):
    import group_store as store

    client = env["client"]
    trip_id, varun = create_trip(client)
    b = join(client, trip_id, person("Bina"))
    c = join(client, trip_id, person("Chetan"))
    client.post(f"/api/trips/{trip_id}/plan", json={"member_id": varun})
    store.record_vote(trip_id, varun, "approve", "")

    results = []
    barrier = threading.Barrier(2)

    def cast(member):
        barrier.wait()
        results.append(store.record_vote(trip_id, member, "approve", "")["complete"])

    threads = [threading.Thread(target=cast, args=(m,)) for m in (b, c)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == [False, True]
    assert store.get_trip(trip_id)["status"] == store.PROCESSING


def test_solo_mode_still_works(env):
    client = env["client"]
    r = client.post("/api/travel", json={"message": "Plan 3 days in Goa from Mumbai"})
    body = r.json()
    assert body["success"] and body["requires_approval"]
    r = client.post("/api/travel/approve",
                    json={"thread_id": body["thread_id"], "approved": True, "feedback": ""})
    assert r.json()["answer"] == "FINAL PLAN"


def test_trip_stuck_by_a_crash_is_released(env):
    import psycopg

    client = env["client"]
    trip_id, varun = create_trip(client)
    join(client, trip_id, person("Bina"))
    with psycopg.connect(TEST_DB, autocommit=True) as conn:
        conn.execute("UPDATE trips SET status = 'planning', updated_at = now() - interval '20 minutes' "
                     "WHERE trip_id = %s", (trip_id,))
    view = client.get(f"/api/trips/{trip_id}", params={"member_id": varun}).json()
    assert view["trip"]["status"] == "collecting"
    assert view["trip"]["can_edit"] is True
