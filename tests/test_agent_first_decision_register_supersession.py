from __future__ import annotations

import copy
import unittest

from scripts.agent_first_decision_gate import resolved_history_failures
from scripts.agent_first_decision_register import (
    DecisionRegisterFormatError,
    load_register,
    validate_register,
)
from scripts.agent_first_decision_render import render_document
from tests.test_agent_first_decision_register_gate import (
    REGISTER_PATH,
    _append_decision,
    _append_followup,
    _pending_d1,
    _resolve,
    _resolved_d1,
)


def _decision(register: dict[str, object], decision_id: str) -> dict[str, object]:
    return next(
        item for item in register["decisions"] if item["id"] == decision_id
    )


def _drop_decision(
    register: dict[str, object], decision_id: str
) -> dict[str, object]:
    changed = copy.deepcopy(register)
    changed["decisions"] = [
        item for item in changed["decisions"] if item["id"] != decision_id
    ]
    changed["question_order"].remove(decision_id)
    for unit in changed["normative_units"]:
        if decision_id in unit["decision_ids"]:
            unit["decision_ids"].remove(decision_id)
    return changed


class AgentFirstDecisionRegisterSupersessionTest(unittest.TestCase):
    def test_explicit_successor_requires_structural_followup(self) -> None:
        prior = _resolved_d1()
        pending = _append_followup(_append_decision(prior))
        pending["active_decision_id"] = "D27"
        validate_register(pending)

        invalid = copy.deepcopy(pending)
        _decision(invalid, "D27")["supersedes"] = ["D1"]
        with self.assertRaisesRegex(
            DecisionRegisterFormatError, "superseder_not_resolved"
        ):
            validate_register(invalid)

        current = _resolve(
            pending,
            "D27",
            _decision(pending, "D27")["options"][0]["id"],
            supersedes=("D1",),
        )
        current["active_decision_id"] = "D3"
        validate_register(current)
        self.assertEqual([], resolved_history_failures(current, [prior]))
        self.assertIn("**Supersedes:** D1", render_document(current))

        missing = _drop_decision(pending, "D28")
        missing = _resolve(
            missing,
            "D27",
            _decision(missing, "D27")["options"][0]["id"],
            supersedes=("D1",),
        )
        missing["active_decision_id"] = "D3"
        with self.assertRaisesRegex(
            DecisionRegisterFormatError, "conditional_followup_count"
        ):
            validate_register(missing)

        rewritten = copy.deepcopy(current)
        _decision(rewritten, "D28")["options"][0]["summary"] = (
            "Unrelated replacement question"
        )
        with self.assertRaisesRegex(
            DecisionRegisterFormatError, "conditional_followup_contract"
        ):
            validate_register(rewritten)

    def test_target_and_duplicate_supersession_are_rejected(self) -> None:
        target_pending = _append_followup(
            _append_decision(_pending_d1())
        )
        target_pending = _resolve(
            target_pending,
            "D27",
            _decision(target_pending, "D27")["options"][0]["id"],
            supersedes=("D1",),
        )
        with self.assertRaisesRegex(
            DecisionRegisterFormatError, "target_not_resolved"
        ):
            validate_register(target_pending)

        prior = _resolved_d1()
        current = _append_followup(_append_decision(prior))
        current = _resolve(
            current,
            "D27",
            _decision(current, "D27")["options"][0]["id"],
            supersedes=("D1",),
        )
        current["active_decision_id"] = "D3"
        validate_register(current)

        duplicate = _append_decision(
            current, decision_id="D29", question_index=3
        )
        duplicate = _append_followup(
            duplicate,
            source_id="D29",
            decision_id="D30",
            question_index=4,
        )
        duplicate = _resolve(
            duplicate,
            "D29",
            _decision(duplicate, "D29")["options"][0]["id"],
            supersedes=("D1",),
        )
        with self.assertRaisesRegex(
            DecisionRegisterFormatError, "duplicate_target"
        ):
            validate_register(duplicate)


if __name__ == "__main__":
    unittest.main()
