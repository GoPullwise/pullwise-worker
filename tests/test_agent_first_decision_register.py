from __future__ import annotations

import copy
import unittest
from pathlib import Path

from scripts.agent_first_decision_register import (
    DecisionRegisterFormatError,
    load_register,
    validate_register,
    verify_register,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
REGISTER_PATH = (
    REPO_ROOT / "contracts" / "agent-first" / "spec-decision-register.json"
)


class AgentFirstDecisionRegisterTest(unittest.TestCase):
    def test_current_register_is_valid_pending_and_documented(self) -> None:
        register = load_register(REGISTER_PATH)

        report = verify_register(register, REPO_ROOT)

        self.assertEqual("valid_pending", report["status"])
        self.assertTrue(report["valid"])
        self.assertEqual([], report["failures"])
        self.assertEqual("D1", report["active_decision_id"])
        self.assertGreater(report["pending_decision_count"], 1)
        self.assertEqual(0, report["resolved_decision_count"])
        self.assertTrue(report["document_matches"])

    def test_pending_decision_cannot_carry_a_resolution(self) -> None:
        register = load_register(REGISTER_PATH)
        changed = copy.deepcopy(register)
        changed["decisions"][0]["resolution"] = {
            "selected_option_id": changed["decisions"][0]["options"][0]["id"],
            "decided_by": "user",
            "decided_at": "2026-07-17",
            "evidence_refs": ["conversation:synthetic"],
        }

        with self.assertRaisesRegex(
            DecisionRegisterFormatError, "decisions\\[0\\]\\.resolution:pending"
        ):
            validate_register(changed)

    def test_resolved_decision_requires_selected_option_and_evidence(self) -> None:
        register = load_register(REGISTER_PATH)
        changed = copy.deepcopy(register)
        changed["decisions"][0]["status"] = "resolved"
        changed["decisions"][0]["resolution"] = {
            "selected_option_id": "not-an-option",
            "decided_by": "user",
            "decided_at": "2026-07-17",
            "evidence_refs": ["conversation:synthetic"],
        }

        with self.assertRaisesRegex(
            DecisionRegisterFormatError,
            "decisions\\[0\\]\\.resolution.selected_option_id",
        ):
            validate_register(changed)

    def test_dependency_cycle_is_rejected(self) -> None:
        register = load_register(REGISTER_PATH)
        changed = copy.deepcopy(register)
        first, second = changed["decisions"][:2]
        first["depends_on"] = [second["id"]]
        second["depends_on"] = [first["id"]]

        with self.assertRaisesRegex(DecisionRegisterFormatError, "decisions:cycle"):
            validate_register(changed)

    def test_active_decision_must_be_first_ready_pending_decision(self) -> None:
        register = load_register(REGISTER_PATH)
        changed = copy.deepcopy(register)
        changed["active_decision_id"] = changed["decisions"][1]["id"]

        with self.assertRaisesRegex(
            DecisionRegisterFormatError, "active_decision_id:first_ready_pending"
        ):
            validate_register(changed)

    def test_required_slice_fails_closed_while_decisions_are_pending(self) -> None:
        register = load_register(REGISTER_PATH)

        report = verify_register(register, REPO_ROOT, require_slice="S2")

        self.assertEqual("blocked", report["status"])
        self.assertFalse(report["valid"])
        self.assertIn(
            {
                "code": "slice_blocked_by_pending_decisions",
                "slice": "S2",
                "decision_ids": ["D1"],
            },
            report["failures"],
        )

    def test_machine_entrypoint_is_documented(self) -> None:
        command = (
            "python scripts/agent_first_decision_register.py check --repo-root ."
        )
        manifest = "contracts/agent-first/spec-decision-register.json"
        for relative in (
            "AGENTS.md",
            "docs/agent-first-worker-mvp-implementation-design.md",
        ):
            text = (REPO_ROOT / relative).read_text(encoding="utf-8")
            with self.subTest(path=relative):
                self.assertIn(command, text)
                self.assertIn(manifest, text)


if __name__ == "__main__":
    unittest.main()
