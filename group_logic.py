"""
Group trip logic for TripBuddy.

Everything in this file is plain Python: no language model and no network.
It merges every member's limits into one set of group rules, detects clashes,
and counts votes. Because it is ordinary code, the results are exact and
repeatable, which is the part a chatbot cannot guarantee.
"""

from __future__ import annotations

from datetime import date
from typing import Any

FOOD_PREFERENCES = {
    "no_preference": "No preference",
    "vegetarian": "Vegetarian",
    "jain": "Jain",
    "vegan": "Vegan",
    "non_vegetarian": "Non-vegetarian",
}

# Preferences that force every shared meal to be vegetarian.
VEG_ONLY = {"vegetarian", "jain", "vegan"}

APPROVAL_RULES = {
    "all": "Everyone must approve",
    "majority": "More than half must approve",
}

MAX_REVISION_ROUNDS = 2
MAX_MUST_SEE = 15


def _to_date(value: Any) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def _names(names: list[str]) -> str:
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]


def _rupees(amount: int) -> str:
    """Format a number the Indian way: 1,50,000."""
    digits = str(int(amount))
    if len(digits) <= 3:
        return f"₹{digits}"
    last3 = digits[-3:]
    rest = digits[:-3]
    groups = []
    while len(rest) > 2:
        groups.insert(0, rest[-2:])
        rest = rest[:-2]
    if rest:
        groups.insert(0, rest)
    return "₹" + ",".join(groups + [last3])


def split_places(text: str | None) -> list[str]:
    if not text:
        return []
    parts = [p.strip() for chunk in text.splitlines() for p in chunk.split(",")]
    return [p for p in parts if p]


def merge_members(
    members: list[dict[str, Any]],
    requested_days: int | None = None,
) -> dict[str, Any]:
    """
    Combine every member's details into one set of group rules.

    Rules (all deterministic):
    - Budget: the plan must fit the LOWEST per-person budget anyone gave.
    - Dates: the plan must fall inside the overlap of everyone's dates.
      No overlap is a hard conflict, so planning stops before any AI call.
    - Trip length: shortened to the overlap if the overlap is shorter.
    - Food: one vegetarian/Jain/vegan member makes shared meals vegetarian.
    - Must-see places: combined, duplicates removed, with who asked for each.

    Returns a dict with the merged rules plus `conflicts` (hard, planning is
    blocked) and `warnings` (soft, planning continues).
    """
    conflicts: list[str] = []
    warnings: list[str] = []
    names = [m["name"] for m in members]

    # ---- Budget: the lowest limit wins -------------------------------
    budgets = [(int(m["budget_max"]), m["name"]) for m in members if m.get("budget_max")]
    budget_per_person = None
    budget_set_by: list[str] = []
    if budgets:
        budget_per_person = min(b for b, _ in budgets)
        budget_set_by = [n for b, n in budgets if b == budget_per_person]
        highest = max(b for b, _ in budgets)
        if highest >= 2 * budget_per_person:
            warnings.append(
                f"Budgets differ a lot ({_rupees(budget_per_person)} to {_rupees(highest)}). "
                f"The plan follows the lowest, set by {_names(budget_set_by)}."
            )
        missing = [m["name"] for m in members if not m.get("budget_max")]
        if missing:
            warnings.append(f"{_names(missing)} did not give a budget.")
    else:
        warnings.append("Nobody gave a budget, so costs will be general estimates.")

    # ---- Dates: overlap of everyone's window --------------------------
    windows = []
    for m in members:
        start, end = _to_date(m.get("available_from")), _to_date(m.get("available_to"))
        if start and end:
            windows.append((start, end, m["name"]))
    window_start = window_end = None
    window_days = None
    if windows:
        window_start = max(w[0] for w in windows)
        window_end = min(w[1] for w in windows)
        if window_start > window_end:
            latest_start = max(windows, key=lambda w: w[0])
            earliest_end = min(windows, key=lambda w: w[1])
            conflicts.append(
                "No dates work for everyone: "
                f"{earliest_end[2]} is free only until {earliest_end[1]:%d %b %Y}, "
                f"but {latest_start[2]} is free only from {latest_start[0]:%d %b %Y}."
            )
            window_start = window_end = None
        else:
            window_days = (window_end - window_start).days + 1
        no_dates = [m["name"] for m in members
                    if not (m.get("available_from") and m.get("available_to"))]
        if no_dates:
            warnings.append(f"{_names(no_dates)} did not give dates, so they are treated as flexible.")
    else:
        warnings.append("Nobody gave dates, so the plan will not be tied to specific days.")

    # ---- Trip length ---------------------------------------------------
    trip_days = requested_days
    if requested_days and window_days and window_days < requested_days:
        trip_days = window_days
        warnings.append(
            f"The trip was asked for {requested_days} days, but the dates everyone shares "
            f"cover only {window_days}. The plan is shortened to {window_days} days."
        )

    # ---- Starting cities ----------------------------------------------
    origins: dict[str, list[str]] = {}
    for m in members:
        city = (m.get("origin") or "").strip()
        if city:
            origins.setdefault(city.title(), []).append(m["name"])
    if len(origins) > 1:
        warnings.append(
            "Members start from different cities ("
            + "; ".join(f"{c}: {_names(n)}" for c, n in origins.items())
            + "). The plan includes travel from each."
        )

    # ---- Food -----------------------------------------------------------
    diets: dict[str, list[str]] = {}
    for m in members:
        pref = m.get("food_pref") or "no_preference"
        diets.setdefault(pref, []).append(m["name"])
    veg_members = [n for p, ns in diets.items() if p in VEG_ONLY for n in ns]
    meal_rules = []
    if veg_members:
        meal_rules.append(f"All shared meals vegetarian (because of {_names(veg_members)})")
    if diets.get("jain"):
        meal_rules.append(f"Jain food, with no onion, garlic or root vegetables, for {_names(diets['jain'])}")
    if diets.get("vegan"):
        meal_rules.append(f"Vegan food, with no dairy, for {_names(diets['vegan'])}")

    # ---- Must-see places ---------------------------------------------
    must_see: dict[str, dict[str, Any]] = {}
    for m in members:
        for place in split_places(m.get("must_see")):
            key = place.lower()
            entry = must_see.setdefault(key, {"place": place, "requested_by": []})
            if m["name"] not in entry["requested_by"]:
                entry["requested_by"].append(m["name"])
    must_see_list = sorted(must_see.values(), key=lambda e: -len(e["requested_by"]))
    if len(must_see_list) > MAX_MUST_SEE:
        warnings.append(
            f"{len(must_see_list)} must-see places were listed. The {MAX_MUST_SEE} "
            "most requested are kept."
        )
        must_see_list = must_see_list[:MAX_MUST_SEE]

    notes = [{"name": m["name"], "note": m["notes"].strip()} for m in members if (m.get("notes") or "").strip()]

    return {
        "group_size": len(members),
        "member_names": names,
        "budget_per_person": budget_per_person,
        "budget_set_by": budget_set_by,
        "window_start": window_start.isoformat() if window_start else None,
        "window_end": window_end.isoformat() if window_end else None,
        "window_days": window_days,
        "trip_days": trip_days,
        "origins": origins,
        "diets": diets,
        "meal_rules": meal_rules,
        "must_see": must_see_list,
        "notes": notes,
        "conflicts": conflicts,
        "warnings": warnings,
    }


def build_group_brief(merged: dict[str, Any]) -> str:
    """Turn the merged rules into the text every agent receives."""
    lines = [
        f"GROUP TRIP: {merged['group_size']} travellers ({_names(merged['member_names'])}).",
    ]
    if merged["budget_per_person"]:
        lines.append(
            f"Budget limit: {_rupees(merged['budget_per_person'])} per person, set by "
            f"{_names(merged['budget_set_by'])}. The whole plan must fit this for every member."
        )
    if merged["window_start"]:
        lines.append(
            f"Dates: only between {merged['window_start']} and {merged['window_end']} "
            f"({merged['window_days']} days everyone is free)."
        )
    if merged["trip_days"]:
        lines.append(f"Trip length: {merged['trip_days']} days.")
    if merged["origins"]:
        lines.append(
            "Starting cities: "
            + "; ".join(f"{c} ({_names(n)})" for c, n in merged["origins"].items())
            + ". Include travel from each starting city."
        )
    for rule in merged["meal_rules"]:
        lines.append(f"Meals: {rule}.")
    if merged["must_see"]:
        lines.append(
            "Must-see places, each must appear in the plan: "
            + "; ".join(f"{e['place']} (asked by {_names(e['requested_by'])})" for e in merged["must_see"])
            + "."
        )
    for n in merged["notes"]:
        lines.append(f"Note from {n['name']}: {n['note']}")
    return "\n".join(lines)


def tally_votes(votes: list[dict[str, Any]], rule: str = "all") -> dict[str, Any]:
    """
    Count one round of votes.

    votes: [{"name", "decision": "approve" | "revise", "feedback"}]
    rule:  "all" (everyone approves) or "majority" (more than half).
    """
    total = len(votes)
    approvals = sum(1 for v in votes if v["decision"] == "approve")
    if rule == "majority":
        approved = approvals * 2 > total
    else:
        approved = total > 0 and approvals == total

    change_requests = [
        f"- {v['name']}: {v['feedback'].strip()}"
        for v in votes
        if v["decision"] == "revise" and (v.get("feedback") or "").strip()
    ]
    return {
        "approved": approved,
        "approvals": approvals,
        "total": total,
        "summary": f"{approvals} of {total} approved ({APPROVAL_RULES.get(rule, rule)})",
        "combined_feedback": "\n".join(change_requests),
    }
