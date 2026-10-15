"""
Group voting scenarios: what happens to change requests.

The fake itinerary agent copies the requests it is given into its revised
draft ("What changed: ..."), so these tests can check that the draft the
group votes on next really contains every requested change.

Run with:
    TEST_DATABASE_URL=postgresql://user:pass@localhost/tripbuddy_test?sslmode=disable \
        python -m pytest tests/test_group_voting.py -q
"""

import os
import re
import sys

import pytest

TEST_DB = os.getenv("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DB, reason="set TEST_DATABASE_URL to run")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_group_api import create_trip, env, join, person, vote  # noqa: E402,F401


@pytest.fixture
def echoing_planner(env):
    """Revised drafts list the requests they were given, like a model that applies them."""
    llm = env["llm"]
    real = llm.invoke

    def invoke(messages):
        reply = real(messages)
        if isinstance(messages, str) or "expert travel planner" not in messages[0].content:
            return reply
        prompt = messages[-1].content
        match = re.search(r"Requested changes, by member:\n(.*?)\n\nPrevious draft:", prompt, re.S)
        if match:
            from langchain_core.messages import AIMessage
            return AIMessage(content=f"{reply.content.splitlines()[0]}\nWhat changed:\n{match.group(1)}")
        return reply

    llm.invoke = invoke
    yield llm
    llm.invoke = real


def view_of(client, trip_id, member):
    return client.get(f"/api/trips/{trip_id}", params={"member_id": member}).json()


def three_member_trip(client, rule="all"):
    trip_id, varun = create_trip(client, rule=rule)
    riya = join(client, trip_id, person("Riya"))
    aman = join(client, trip_id, person("Aman"))
    r = client.post(f"/api/trips/{trip_id}/plan", json={"member_id": varun})
    assert r.json()["trip"]["status"] == "awaiting_votes"
    return trip_id, varun, riya, aman


def last_prompt(llm, system_word):
    return [p for s, p in llm.prompts if system_word in s][-1]


def test_everyone_asks_for_changes_and_the_next_draft_has_them(env, echoing_planner):
    client, llm = env["client"], echoing_planner
    trip_id, varun, riya, aman = three_member_trip(client)
    first_draft = view_of(client, trip_id, varun)["plan"]["answer"]

    vote(client, trip_id, varun, "revise", "Add a rest day")
    vote(client, trip_id, riya, "revise", "Cheaper hotel")
    view = vote(client, trip_id, aman, "revise", "More street food").json()

    # The agents rewrote the draft with all three requests before the next vote
    prompt = last_prompt(llm, "expert travel planner")
    assert "REVISION 1 of 2" in prompt
    for line in ("- Varun: Add a rest day", "- Riya: Cheaper hotel", "- Aman: More street food"):
        assert line in prompt
    assert first_draft in prompt  # the previous draft is what gets revised

    assert view["trip"]["status"] == "awaiting_votes"
    assert view["trip"]["current_round"] == 2
    draft = view["plan"]["answer"]
    assert draft != first_draft
    for request in ("Add a rest day", "Cheaper hotel", "More street food"):
        assert request in draft  # the draft now being voted on has every change

    # Voters can see what the previous round asked for
    asked = {(v["name"], v["feedback"]) for v in view["previous_votes"]}
    assert asked == {("Varun", "Add a rest day"), ("Riya", "Cheaper hotel"), ("Aman", "More street food")}
    assert view["votes"] == []  # round 2 starts empty

    for member in (varun, riya):
        vote(client, trip_id, member, "approve")
    view = vote(client, trip_id, aman, "approve").json()
    assert view["trip"]["status"] == "completed"
    assert view["plan"]["approved"] is True
    assert draft in last_prompt(llm, "booking assistant")  # the final plan polishes the revised draft


def test_one_request_with_everyone_must_approve_starts_a_revision(env, echoing_planner):
    client, llm = env["client"], echoing_planner
    trip_id, varun, riya, aman = three_member_trip(client, rule="all")

    vote(client, trip_id, varun, "approve")
    vote(client, trip_id, riya, "approve")
    view = vote(client, trip_id, aman, "revise", "Skip the scuba dive").json()

    assert view["trip"]["status"] == "awaiting_votes" and view["trip"]["current_round"] == 2
    assert "Skip the scuba dive" in view["plan"]["answer"]
    assert "- Aman: Skip the scuba dive" in last_prompt(llm, "expert travel planner")


def test_one_request_with_majority_rule_is_applied_in_the_final_plan(env, echoing_planner):
    client, llm = env["client"], echoing_planner
    trip_id, varun, riya, aman = three_member_trip(client, rule="majority")

    vote(client, trip_id, varun, "approve")
    vote(client, trip_id, riya, "approve")
    view = vote(client, trip_id, aman, "revise", "Skip the scuba dive").json()

    # 2 of 3 approved, so the plan is final, but Aman's request isn't dropped:
    assert view["trip"]["status"] == "completed"
    final_prompt = last_prompt(llm, "booking assistant")
    assert "- Aman: Skip the scuba dive" in final_prompt
    assert "Apply them only where they don't break a group rule" in final_prompt
    assert "say how each of these requests was handled" in final_prompt


def test_no_agreement_after_three_rounds_is_not_reported_as_approved(env, echoing_planner):
    client, llm = env["client"], echoing_planner
    trip_id, varun = create_trip(client)
    bina = join(client, trip_id, person("Bina"))
    client.post(f"/api/trips/{trip_id}/plan", json={"member_id": varun})
    for round_no in (1, 2, 3):
        vote(client, trip_id, varun, "approve")
        view = vote(client, trip_id, bina, "revise", f"change {round_no}").json()

    assert view["trip"]["status"] == "completed"
    assert view["plan"]["approved"] is False  # the page must not say "approved"
    final_prompt = last_prompt(llm, "booking assistant")
    assert "did not approve after 2 revisions" in final_prompt
    assert "- Bina: change 3" in final_prompt
