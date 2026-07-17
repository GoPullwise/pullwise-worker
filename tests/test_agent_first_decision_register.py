from __future__ import annotations

import copy
import unittest
from pathlib import Path

from scripts.agent_first_decision_catalog import QUESTION_ORDER, REQUIRED_CATALOG
from scripts.agent_first_decision_register import (
    DecisionRegisterFormatError,
    canonical_resolution_sha256,
    load_register,
    validate_register,
    verify_register,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
REGISTER_PATH = REPO_ROOT / "contracts" / "agent-first" / "spec-decision-register.json"


def _resolution(
    decision_id: str,
    *,
    selected_option_id: str | None,
    custom_text: str | None = None,
    authority: str = "user",
    supersedes: str | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "kind": "custom" if custom_text is not None else "option",
        "selected_option_id": selected_option_id,
        "custom_text": custom_text,
        "decision_text": custom_text or f"Confirmed option {selected_option_id}.",
        "authority": authority,
        "decided_at": "2026-07-17",
        "evidence_refs": ["conversation:synthetic-test"],
        "supersedes_resolution_sha256": supersedes,
    }
    payload["resolution_sha256"] = canonical_resolution_sha256(decision_id, payload)
    return payload


def _resolve(
    register: dict[str, object], decision_id: str, option_id: str
) -> dict[str, object]:
    changed = copy.deepcopy(register)
    decision = next(item for item in changed["decisions"] if item["id"] == decision_id)
    decision["status"] = "resolved"
    decision["resolution"] = _resolution(
        decision_id, selected_option_id=option_id
    )
    return changed


class AgentFirstDecisionRegisterTest(unittest.TestCase):
    def test_current_register_is_complete_valid_pending_and_documented(self) -> None:
        register = load_register(REGISTER_PATH)
        report = verify_register(register, REPO_ROOT)

        self.assertEqual("valid_pending", report["status"])
        self.assertTrue(report["valid"])
        self.assertFalse(report["ready"])
        self.assertEqual([], report["failures"])
        self.assertEqual("D1", report["active_decision_id"])
        self.assertEqual(26, report["pending_decision_count"])
        self.assertEqual(0, report["resolved_decision_count"])
        self.assertTrue(report["document_matches"])
        self.assertEqual(list(QUESTION_ORDER), register["question_order"])
        self.assertEqual(
            [item["id"] for item in REQUIRED_CATALOG],
            [item["id"] for item in register["decisions"]],
        )

    def test_required_catalog_and_single_slice_ordinal_are_frozen(self) -> None:
        register = load_register(REGISTER_PATH)
        removed = copy.deepcopy(register)
        removed["decisions"].pop()
        with self.assertRaisesRegex(DecisionRegisterFormatError, "required_catalog"):
            validate_register(removed)

        changed = copy.deepcopy(register)
        changed["decisions"][0]["required_by_slice"] = "S3"
        with self.assertRaisesRegex(DecisionRegisterFormatError, "required_catalog"):
            validate_register(changed)

    def test_pending_decision_cannot_carry_resolution_or_history(self) -> None:
        register = load_register(REGISTER_PATH)
        changed = copy.deepcopy(register)
        changed["decisions"][0]["resolution"] = _resolution(
            "D1", selected_option_id="pullwise_full_scan"
        )
        with self.assertRaisesRegex(
            DecisionRegisterFormatError, r"decisions\[0\]\.resolution:pending"
        ):
            validate_register(changed)

        changed = copy.deepcopy(register)
        changed["decisions"][0]["superseded_resolutions"] = [
            _resolution("D1", selected_option_id="pullwise_full_scan")
        ]
        with self.assertRaisesRegex(
            DecisionRegisterFormatError, r"decisions\[0\]\.resolution:pending"
        ):
            validate_register(changed)

    def test_resolution_supports_option_or_explicit_custom_text(self) -> None:
        register = load_register(REGISTER_PATH)
        option = _resolve(register, "D1", "pullwise_full_scan")
        option["active_decision_id"] = "D3"
        validate_register(option)

        custom = copy.deepcopy(register)
        custom["decisions"][0]["status"] = "resolved"
        custom["decisions"][0]["resolution"] = _resolution(
            "D1", selected_option_id=None, custom_text="Use a bounded hybrid scope."
        )
        custom["active_decision_id"] = "D3"
        validate_register(custom)

    def test_resolution_rejects_untrusted_authority_and_digest_tampering(self) -> None:
        register = load_register(REGISTER_PATH)
        changed = _resolve(register, "D1", "pullwise_full_scan")
        changed["active_decision_id"] = "D3"
        changed["decisions"][0]["resolution"]["authority"] = "agent"
        with self.assertRaisesRegex(DecisionRegisterFormatError, "authority"):
            validate_register(changed)

        changed = _resolve(register, "D1", "pullwise_full_scan")
        changed["active_decision_id"] = "D3"
        changed["decisions"][0]["resolution"]["decision_text"] = "Tampered."
        with self.assertRaisesRegex(DecisionRegisterFormatError, "resolution_sha256"):
            validate_register(changed)

    def test_resolved_decision_requires_resolved_or_inactive_dependencies(self) -> None:
        register = load_register(REGISTER_PATH)
        changed = _resolve(register, "D3", "mvp_r0_r1_reject_r2")
        with self.assertRaisesRegex(DecisionRegisterFormatError, "depends_on:unresolved"):
            validate_register(changed)

    def test_d2_activation_is_derived_from_d1_resolution(self) -> None:
        register = load_register(REGISTER_PATH)
        pullwise = _resolve(register, "D1", "pullwise_full_scan")
        pullwise["active_decision_id"] = "D3"
        validate_register(pullwise)
        report = verify_register(pullwise, REPO_ROOT, check_document=False)
        self.assertIn("D2", report["inactive_decision_ids"])

        generic = _resolve(register, "D1", "generic_agent_worker")
        generic["active_decision_id"] = "D2"
        validate_register(generic)
        report = verify_register(generic, REPO_ROOT, check_document=False)
        self.assertEqual("D2", report["active_decision_id"])

    def test_active_decision_uses_question_order_not_numeric_readiness(self) -> None:
        register = load_register(REGISTER_PATH)
        changed = copy.deepcopy(register)
        changed["active_decision_id"] = "D7"
        with self.assertRaisesRegex(
            DecisionRegisterFormatError, "active_decision_id:first_ready_pending"
        ):
            validate_register(changed)

    def test_required_slice_block_does_not_make_register_invalid(self) -> None:
        register = load_register(REGISTER_PATH)
        report = verify_register(
            register, REPO_ROOT, require_slice="S2", check_document=False
        )

        self.assertEqual("blocked", report["status"])
        self.assertTrue(report["valid"])
        self.assertFalse(report["ready"])
        blocker = next(
            item for item in report["failures"]
            if item["code"] == "slice_blocked_by_pending_decisions"
        )
        self.assertEqual(["D1"], blocker["decision_ids"])

    def test_machine_entrypoint_is_documented(self) -> None:
        command = "python scripts/agent_first_decision_register.py check --repo-root ."
        manifest = "contracts/agent-first/spec-decision-register.json"
        for relative in ("AGENTS.md", "docs/agent-first-worker-mvp-implementation-design.md"):
            text = (REPO_ROOT / relative).read_text(encoding="utf-8")
            with self.subTest(path=relative):
                self.assertIn(command, text)
                self.assertIn(manifest, text)


if __name__ == "__main__":
    unittest.main()
