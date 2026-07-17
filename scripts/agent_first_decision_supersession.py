"""Append-only supersession and conditional follow-up validation."""

from __future__ import annotations

from typing import Any


def supersession_error(root: dict[str, Any]) -> str | None:
    decisions = root["decisions"]
    by_id = {item["id"]: item for item in decisions}
    positions = {
        decision_id: index
        for index, decision_id in enumerate(root["question_order"])
    }
    superseded_targets: set[str] = set()
    used_followups: set[str] = set()
    for index, successor in enumerate(decisions):
        if not successor["supersedes"]:
            continue
        label = f"decisions[{index}].supersedes"
        if successor["status"] != "resolved":
            return f"{label}:superseder_not_resolved"
        successor_options = {
            option["id"] for option in successor["options"]
        }
        for target_id in successor["supersedes"]:
            target = by_id[target_id]
            if target["status"] != "resolved":
                return f"{label}:target_not_resolved"
            if target_id in superseded_targets:
                return f"{label}:duplicate_target"
            superseded_targets.add(target_id)
            if not set(target["affected_units"]) <= set(
                successor["affected_units"]
            ):
                return f"{label}:affected_units"
            children = [
                item
                for item in decisions
                if item["activation"] is not None
                and item["activation"]["decision_id"] == target_id
            ]
            for child in children:
                trigger = child["activation"]["selected_option_id"]
                if trigger not in successor_options:
                    return f"{label}:activation_option_missing"
                candidates = [
                    item
                    for item in decisions
                    if item["id"] != child["id"]
                    and item["activation"] is not None
                    and item["activation"]["decision_id"] == successor["id"]
                    and item["activation"]["selected_option_id"] == trigger
                ]
                if len(candidates) != 1:
                    return f"{label}:conditional_followup_count"
                followup = candidates[0]
                if followup["id"] in used_followups:
                    return f"{label}:conditional_followup_reused"
                used_followups.add(followup["id"])
                expected_dependencies = [
                    successor["id"] if item == target_id else item
                    for item in child["depends_on"]
                ]
                frozen_fields = (
                    "scope",
                    "title",
                    "question",
                    "effects",
                    "affected_units",
                    "options",
                    "recommended_option_id",
                )
                if (
                    positions[followup["id"]] <= positions[successor["id"]]
                    or followup["depends_on"] != expected_dependencies
                    or followup["required_by_slice"]
                    != child["required_by_slice"]
                    or any(
                        followup[field] != child[field]
                        for field in frozen_fields
                    )
                    or not set(child["source_refs"])
                    <= set(followup["source_refs"])
                ):
                    return f"{label}:conditional_followup_contract"
    return None
