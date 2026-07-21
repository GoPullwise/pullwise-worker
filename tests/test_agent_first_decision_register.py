from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts.agent_first_decision_catalog import (
    NORMATIVE_UNIT_CATALOG,
    QUESTION_ORDER,
    REQUIRED_CATALOG,
)
import scripts.agent_first_decision_definition as decision_definition
from scripts.agent_first_decision_gate import _generated_document_matches
from scripts.agent_first_decision_register import (
    DecisionRegisterFormatError,
    canonical_resolution_sha256,
    load_register,
    load_repo_register,
    validate_register,
    verify_register,
)
from scripts.agent_first_decision_render import (
    render_generated_file,
    sync_generated_file,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
REGISTER_PATH = REPO_ROOT / "contracts" / "agent-first" / "spec-decision-register.json"


def _resolution(
    decision_id: str,
    *,
    selected_option_id: str,
    custom_text: str | None = None,
    authority: str = "user",
) -> dict[str, object]:
    payload: dict[str, object] = {
        "kind": "custom" if custom_text is not None else "option",
        "selected_option_id": selected_option_id,
        "custom_text": custom_text,
        "decision_text": custom_text or f"Confirmed option {selected_option_id}.",
        "authority": authority,
        "decided_at": "2026-07-17",
        "evidence_refs": ["conversation:synthetic-test"],
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


def _pending_d1_register() -> dict[str, object]:
    changed = copy.deepcopy(load_register(REGISTER_PATH))
    required_ids = {item["id"] for item in REQUIRED_CATALOG}
    changed["decisions"] = changed["decisions"][: len(REQUIRED_CATALOG)]
    changed["question_order"] = list(QUESTION_ORDER)
    for unit in changed["normative_units"]:
        unit["decision_ids"] = [item for item in unit["decision_ids"] if item in required_ids]
    for decision in changed["decisions"]:
        decision["status"] = "pending"
        decision["resolution"] = None
    changed["active_decision_id"] = "D1"
    return validate_register(changed)


class AgentFirstDecisionRegisterTest(unittest.TestCase):
    def test_current_register_has_the_user_resolved_decision_prefix(self) -> None:
        register = load_register(REGISTER_PATH)
        report = verify_register(register, REPO_ROOT)

        self.assertEqual("valid_pending", report["status"])
        self.assertTrue(report["valid"])
        self.assertFalse(report["ready"])
        self.assertEqual([], report["failures"])
        self.assertEqual("D20", report["active_decision_id"])
        self.assertEqual(7, report["pending_decision_count"])
        self.assertEqual(19, report["resolved_decision_count"])
        self.assertEqual(1, report["inactive_decision_count"])
        self.assertEqual(["D2"], report["inactive_decision_ids"])
        self.assertTrue(report["document_matches"])
        expected_resolutions = {
            "D1": ("pullwise_full_scan", "ab117e7c86472b7ce57bf2433978df0efe1299353ad747b7eabbff723fec469a"),
            "D3": ("mvp_r0_r1_reject_r2", "0126d5ee3329c0f954e88e08979e8f0883086b3846315e2904cd7d323b97b07a"),
            "D4": ("field_by_field_ownership", "b009c68af93c965837e562d57cd20328e037b5fca0da30cc694125e0fee79654"),
            "D5": ("per_control_transaction", "859647945022b9d62bca4c6cf16b290c48e4e9bdb2f10700a40553194748b74a"),
            "D6": ("single_claim_owner_transaction", "e1ad16c135ae5f0880123becdd640bf685c0f201b44dd941830590b0b39174d8"),
            "D7": ("persist_elapsed_consumption", "5d7916e9389c0203185fb7e2e64be49df0ea52557d875f661f5d0180e093f5ea"),
            "D8": ("task_active_attempt_fenced", "e895f73c3a0962937cbab61b4c8037f9ccba9daa6e6de89d5004005dd830b98a"),
            "D9": ("internal_result_cas_authoritative", "3e8a5cf9d69cccd50667009c80e9a3176501d3c0150d5bec931ee71fb1cc46ce"),
            "D10": ("global_safety_first_matrix", "1daae4c66d41bd95a3eef8e24756590c8e6f75a05899548dc3126bfd39172e31"),
            "D11": ("partial_delivery_manifest", "dc65778d9f60563e39a9c3262200f8e26efd8c48c29aa0141087793186032a7e"),
            "D12": ("new_generation_supersession", "b459cd0e371c34702e654761aa89caa21238f5c5314020e9d9c7484d60902764"),
            "D13": ("prepublish_cancel_postpublish_reconcile", "4a90df4dce3840e2f726d952fa0b49ef9294e73e851208f968df30642720e5a7"),
            "D14": ("separate_bundle_integrity_manifest", "1798cd24165aa5be17f5e5b256e3ecfd61a2f02e63d064e9f5da60edcf30a889"),
            "D15": ("separate_predicate_registry", "47cf85a523a63a4c26775fe6929bdd132fb37ac82f5cdda41128ee248827cb1b"),
            "D16": ("remove_q0_success_path", "0acca8727c0044d5bc7ef7542e2bc51384c1ca56865889cec8e389b559403130"),
            "D17": ("versioned_concern_table", "8f125e98166a1fa6edacc6ef2e29a1749eb13d5ab5d187d1aab63c38d5cac3a8"),
            "D18": ("coordinator_is_owner", "16fb38386dfedc25cbd4f7d3cc25aeeeb9512b3d0e3733fdb8591441eca3c8de"),
            "D19": ("owner_remains_live", "0fb4d7e749fb873ccb7691ff2a87c30f2792969534311903ce439a5ac86c2796"),
            "D27": ("clean_break_no_legacy", "f3ef27ad6318d4da20d4750cdde9387b66045f1708a909b57aba1c6e48ec2b0e"),
        }
        decisions = {item["id"]: item for item in register["decisions"]}
        for decision_id, expected in expected_resolutions.items():
            with self.subTest(decision_id=decision_id):
                resolution = decisions[decision_id]["resolution"]
                self.assertIsNotNone(resolution)
                self.assertEqual(expected[0], resolution["selected_option_id"])
                self.assertEqual("user", resolution["authority"])
                self.assertEqual(expected[1], resolution["resolution_sha256"])
        expected_order = list(QUESTION_ORDER)
        expected_order.insert(8, "D27")
        self.assertEqual(expected_order, register["question_order"])
        self.assertEqual(
            [*[item["id"] for item in REQUIRED_CATALOG], "D27"],
            [item["id"] for item in register["decisions"]],
        )
        self.assertEqual(["D4"], decisions["D27"]["supersedes"])

    def test_required_catalog_and_single_slice_ordinal_are_frozen(self) -> None:
        register = load_register(REGISTER_PATH)
        removed = copy.deepcopy(register)
        removed["decisions"] = removed["decisions"][: len(REQUIRED_CATALOG) - 1]
        with self.assertRaisesRegex(DecisionRegisterFormatError, "required_catalog"):
            validate_register(removed)

        changed = copy.deepcopy(register)
        changed["decisions"][0]["required_by_slice"] = "S3"
        with self.assertRaisesRegex(DecisionRegisterFormatError, "required_catalog"):
            validate_register(changed)

    def test_pending_decision_cannot_carry_resolution(self) -> None:
        register = _pending_d1_register()
        changed = copy.deepcopy(register)
        changed["decisions"][0]["resolution"] = _resolution(
            "D1", selected_option_id="pullwise_full_scan"
        )
        with self.assertRaisesRegex(
            DecisionRegisterFormatError, r"decisions\[0\]\.resolution:pending"
        ):
            validate_register(changed)

    def test_resolution_supports_option_or_explicit_custom_text(self) -> None:
        register = _pending_d1_register()
        option = _resolve(register, "D1", "pullwise_full_scan")
        option["active_decision_id"] = "D3"
        validate_register(option)

        custom = copy.deepcopy(register)
        custom["decisions"][0]["status"] = "resolved"
        custom["decisions"][0]["resolution"] = _resolution(
            "D1",
            selected_option_id="pullwise_full_scan",
            custom_text="Use a bounded hybrid scope that needs explicit branch review.",
        )
        custom["active_decision_id"] = "D2"
        validate_register(custom)
        self.assertEqual("D2", custom["active_decision_id"])

    def test_required_definition_freezes_questions_options_and_unit_mapping(self) -> None:
        register = load_register(REGISTER_PATH)
        mutations = []

        title = copy.deepcopy(register)
        title["decisions"][0]["title"] = "Rewritten before owner review"
        mutations.append(title)

        option = copy.deepcopy(register)
        option["decisions"][0]["options"][0]["summary"] = "Rewritten option"
        mutations.append(option)

        mapping = copy.deepcopy(register)
        mapping["decisions"][0]["affected_units"].remove("target-authority-scope")
        mapping["normative_units"][0]["decision_ids"].remove("D1")
        mutations.append(mapping)

        for changed in mutations:
            with self.subTest(change=changed["decisions"][0]["title"]):
                with self.assertRaisesRegex(
                    DecisionRegisterFormatError, "register:required_definition"
                ):
                    validate_register(changed)

        catalog = copy.deepcopy(NORMATIVE_UNIT_CATALOG)
        catalog[0]["path"] = "docs/retargeted.md"
        with mock.patch.object(
            decision_definition, "NORMATIVE_UNIT_CATALOG", tuple(catalog)
        ):
            with self.assertRaisesRegex(
                DecisionRegisterFormatError, "register:required_definition"
            ):
                validate_register(register)

    def test_resolution_date_must_be_a_real_canonical_date(self) -> None:
        register = _resolve(
            _pending_d1_register(), "D1", "pullwise_full_scan"
        )
        register["active_decision_id"] = "D3"
        resolution = register["decisions"][0]["resolution"]
        resolution["decided_at"] = "2026-99-99"
        resolution["resolution_sha256"] = canonical_resolution_sha256(
            "D1", resolution
        )
        with self.assertRaisesRegex(DecisionRegisterFormatError, "decided_at"):
            validate_register(register)

    def test_resolution_rejects_untrusted_authority_and_digest_tampering(self) -> None:
        register = _pending_d1_register()
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

    def test_resolution_rejects_blank_or_control_only_evidence(self) -> None:
        register = _resolve(
            _pending_d1_register(), "D1", "pullwise_full_scan"
        )
        register["active_decision_id"] = "D3"
        resolution = register["decisions"][0]["resolution"]
        for field, value in (
            ("decision_text", " "),
            ("evidence_refs", ["\t"]),
        ):
            with self.subTest(field=field):
                changed = copy.deepcopy(register)
                changed_resolution = changed["decisions"][0]["resolution"]
                changed_resolution[field] = value
                changed_resolution["resolution_sha256"] = (
                    canonical_resolution_sha256("D1", changed_resolution)
                )
                with self.assertRaisesRegex(
                    DecisionRegisterFormatError, field
                ):
                    validate_register(changed)

    def test_resolved_decision_requires_resolved_or_inactive_dependencies(self) -> None:
        register = _pending_d1_register()
        changed = _resolve(register, "D3", "mvp_r0_r1_reject_r2")
        with self.assertRaisesRegex(DecisionRegisterFormatError, "depends_on:unresolved"):
            validate_register(changed)

    def test_d2_activation_is_derived_from_d1_resolution(self) -> None:
        register = _pending_d1_register()
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
        register = _pending_d1_register()
        changed = copy.deepcopy(register)
        changed["active_decision_id"] = "D7"
        with self.assertRaisesRegex(
            DecisionRegisterFormatError, "active_decision_id:first_ready_pending"
        ):
            validate_register(changed)

    def test_pullwise_scope_resolution_unblocks_slice_two(self) -> None:
        register = load_register(REGISTER_PATH)
        report = verify_register(
            register, REPO_ROOT, require_slice="S2", check_document=False
        )

        self.assertEqual("valid_pending", report["status"])
        self.assertTrue(report["valid"])
        self.assertFalse(report["ready"])
        self.assertEqual([], report["failures"])
        self.assertEqual("D20", report["active_decision_id"])
        self.assertEqual(["D2"], report["inactive_decision_ids"])

    def test_machine_entrypoint_is_documented(self) -> None:
        command = "python scripts/agent_first_decision_register.py check --repo-root ."
        manifest = "contracts/agent-first/spec-decision-register.json"
        for relative in ("AGENTS.md", "docs/agent-first-worker-mvp-implementation-design.md"):
            text = (REPO_ROOT / relative).read_text(encoding="utf-8")
            with self.subTest(path=relative):
                self.assertIn(command, text)
                self.assertIn(manifest, text)

    def test_documented_cli_runs_and_is_bound_to_canonical_manifest(self) -> None:
        command = [
            sys.executable,
            "-B",
            "scripts/agent_first_decision_register.py",
            "check",
            "--repo-root",
            ".",
        ]
        result = subprocess.run(
            command,
            cwd=REPO_ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("valid_pending", json.loads(result.stdout)["status"])
        with self.assertRaisesRegex(
            DecisionRegisterFormatError, "manifest:canonical_path"
        ):
            load_repo_register(REPO_ROOT, "alternate.json")

    def test_generated_file_sync_is_bounded_and_deterministic(self) -> None:
        register = load_register(REGISTER_PATH)
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "docs").mkdir()
            target = sync_generated_file(register, root)
            expected = render_generated_file(register)
            self.assertIn(
                "Status: generated Agent-First decision packet.", expected
            )
            self.assertNotIn("generated S1 decision packet", expected)
            self.assertEqual(expected, target.read_text(encoding="utf-8"))
            target.write_text(
                expected.replace(
                    "Pending recommendations are non-normative",
                    "All recommendations are approved implementation authority",
                ),
                encoding="utf-8",
                newline="\n",
            )
            self.assertFalse(_generated_document_matches(register, root))
            target.write_text("drift", encoding="utf-8")
            sync_generated_file(register, root)
            self.assertEqual(expected, target.read_text(encoding="utf-8"))

    def test_missing_tracked_documents_are_deterministic_invalidity(self) -> None:
        register = load_register(REGISTER_PATH)
        with tempfile.TemporaryDirectory() as temp_dir:
            report = verify_register(
                register, Path(temp_dir), check_history=False
            )
        self.assertEqual("invalid", report["status"])
        self.assertIn(
            "tracked_file_missing",
            {item["code"] for item in report["failures"]},
        )
        self.assertEqual([], report["indeterminate_reasons"])


if __name__ == "__main__":
    unittest.main()
