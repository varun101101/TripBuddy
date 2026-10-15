"""Unit tests for the group merge and vote rules (no database, no AI)."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from group_logic import build_group_brief, merge_members, tally_votes  # noqa: E402


def member(name, **kw):
    base = {"name": name, "origin": None, "budget_max": None, "available_from": None,
            "available_to": None, "food_pref": "no_preference", "must_see": None, "notes": None}
    base.update(kw)
    return base


def test_lowest_budget_wins_and_is_attributed():
    rules = merge_members([member("Varun", budget_max=60000), member("Riya", budget_max=40000)])
    assert rules["budget_per_person"] == 40000
    assert rules["budget_set_by"] == ["Riya"]


def test_large_budget_gap_is_a_warning():
    rules = merge_members([member("A", budget_max=30000), member("B", budget_max=90000)])
    assert any("differ a lot" in w for w in rules["warnings"])


def test_date_overlap_and_trip_shortened():
    rules = merge_members(
        [member("A", available_from="2026-12-10", available_to="2026-12-20"),
         member("B", available_from="2026-12-15", available_to="2026-12-18")],
        requested_days=7,
    )
    assert rules["window_start"] == "2026-12-15"
    assert rules["window_end"] == "2026-12-18"
    assert rules["window_days"] == 4
    assert rules["trip_days"] == 4
    assert any("shortened to 4 days" in w for w in rules["warnings"])
    assert rules["conflicts"] == []


def test_no_shared_dates_is_a_hard_conflict():
    rules = merge_members(
        [member("A", available_from="2026-12-01", available_to="2026-12-05"),
         member("B", available_from="2026-12-10", available_to="2026-12-15")]
    )
    assert len(rules["conflicts"]) == 1
    assert "A is free only until 05 Dec 2026" in rules["conflicts"][0]
    assert "B is free only from 10 Dec 2026" in rules["conflicts"][0]


def test_members_without_dates_are_flexible():
    rules = merge_members(
        [member("A", available_from="2026-12-10", available_to="2026-12-20"), member("B")]
    )
    assert rules["window_days"] == 11
    assert any("B did not give dates" in w for w in rules["warnings"])


def test_food_rules():
    rules = merge_members([member("A", food_pref="jain"), member("B", food_pref="non_vegetarian"),
                           member("C", food_pref="vegan")])
    assert rules["meal_rules"][0].startswith("All shared meals vegetarian (because of A and C)")
    assert any("Jain" in r and "A" in r for r in rules["meal_rules"])
    assert any("Vegan" in r and "C" in r for r in rules["meal_rules"])


def test_must_see_deduplicated_with_requesters():
    rules = merge_members([member("A", must_see="Solang Valley, Old Manali"),
                           member("B", must_see="old manali\nHadimba Temple")])
    first = rules["must_see"][0]
    assert first["place"] == "Old Manali"
    assert first["requested_by"] == ["A", "B"]
    assert len(rules["must_see"]) == 3


def test_multiple_origins_warning_and_brief():
    rules = merge_members([member("A", origin="ahmedabad", budget_max=150000),
                           member("B", origin="Mumbai", budget_max=150000)])
    assert set(rules["origins"]) == {"Ahmedabad", "Mumbai"}
    brief = build_group_brief(rules)
    assert "₹1,50,000 per person" in brief
    assert "Ahmedabad (A)" in brief and "Mumbai (B)" in brief


def test_tally_all_rule():
    votes = [{"name": "A", "decision": "approve", "feedback": ""},
             {"name": "B", "decision": "revise", "feedback": "cheaper hotel"}]
    result = tally_votes(votes, "all")
    assert result["approved"] is False
    assert result["combined_feedback"] == "- B: cheaper hotel"
    assert result["summary"].startswith("1 of 2 approved")


def test_tally_majority_rule():
    votes = [{"name": "A", "decision": "approve", "feedback": ""},
             {"name": "B", "decision": "approve", "feedback": ""},
             {"name": "C", "decision": "revise", "feedback": "add a rest day"}]
    assert tally_votes(votes, "majority")["approved"] is True
    tie = votes[:1] + [{"name": "B", "decision": "revise", "feedback": "x"}]
    assert tally_votes(tie, "majority")["approved"] is False


def test_tally_keeps_notes_left_with_an_approval():
    votes = [{"name": "A", "decision": "approve", "feedback": "add a rest day"},
             {"name": "B", "decision": "approve", "feedback": ""},
             {"name": "C", "decision": "revise", "feedback": "cheaper hotel"}]
    result = tally_votes(votes, "majority")
    assert result["approved"] is True
    assert result["combined_feedback"] == "- A (approved): add a rest day\n- C: cheaper hotel"
