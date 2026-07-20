from __future__ import annotations

import copy
import json
from pathlib import Path
import unittest

from pullwise_worker.agent_kernel_canonical import canonical_sha256
from pullwise_worker.agent_kernel_schema_registry import (
    SchemaRegistry,
    SchemaValidationError,
)


CONTRACT_ROOT = (
    Path(__file__).resolve().parents[1] / "contracts" / "agent-task" / "v1"
)


def _fixture(schema_id: str) -> dict[str, object]:
    for path in sorted((CONTRACT_ROOT / "fixtures").glob("schema-golden*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        for case in payload["cases"]:
            if case["schema_id"] == schema_id:
                return copy.deepcopy(case["valid"])
    raise AssertionError(f"missing fixture for {schema_id}")


def _redigest(instance: dict[str, object]) -> dict[str, object]:
    instance["digest"] = canonical_sha256(instance, digest_field="digest")
    return instance


class AgentKernelContractSemanticsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = SchemaRegistry(CONTRACT_ROOT)

    def test_policy_digest_and_fail_closed_profile_invariants_are_bound(self) -> None:
        valid = _fixture("effective-execution-policy/v1")
        self.registry.validate("effective-execution-policy/v1", valid)
        cases = []
        wrong_digest = copy.deepcopy(valid)
        wrong_digest["digest"] = "0" * 64
        cases.append(wrong_digest)
        write_root = copy.deepcopy(valid)
        write_root["allowed_write_roots"] = ["repository"]
        cases.append(_redigest(write_root))
        deny_with_origin = copy.deepcopy(valid)
        deny_with_origin["agent_tool_network"]["origins"] = ["https://example.test"]
        cases.append(_redigest(deny_with_origin))
        overlap = copy.deepcopy(valid)
        overlap["granted_capabilities"].append("repository.write")
        overlap["granted_capabilities"].sort()
        cases.append(_redigest(overlap))

        for instance in cases:
            with self.subTest(instance=instance), self.assertRaises(
                SchemaValidationError
            ):
                self.registry.validate("effective-execution-policy/v1", instance)

    def test_policy_rejects_capability_risk_above_the_mvp_ceiling(self) -> None:
        valid = _fixture("effective-execution-policy/v1")
        for ceiling in ("R2", "R3", "R4"):
            policy = copy.deepcopy(valid)
            policy["capability_risk_ceiling"] = ceiling

            with self.subTest(ceiling=ceiling), self.assertRaisesRegex(
                SchemaValidationError, "capability_risk_ceiling_exceeds_mvp"
            ):
                self.registry.validate(
                    "effective-execution-policy/v1", _redigest(policy)
                )

    def test_policy_roots_are_strict_relative_paths(self) -> None:
        valid = _fixture("effective-execution-policy/v1")
        invalid_paths = (
            "/absolute",
            "segment//child",
            "segment/./child",
            "segment/../child",
            "segment\\child",
            "segment\0child",
            "segment/",
        )
        for field in ("allowed_read_roots", "allowed_write_roots"):
            for path in invalid_paths:
                policy = copy.deepcopy(valid)
                if field == "allowed_write_roots":
                    policy["source_write_mode"] = "isolated_reversible"
                policy[field] = [path]

                with self.subTest(field=field, path=path), self.assertRaisesRegex(
                    SchemaValidationError, "relative_path_invalid"
                ):
                    self.registry.validate(
                        "effective-execution-policy/v1", _redigest(policy)
                    )

        valid_nested = copy.deepcopy(valid)
        valid_nested["allowed_read_roots"] = ["repository", "task/runtime"]
        valid_nested["allowed_write_roots"] = ["task/runtime"]
        valid_nested["source_write_mode"] = "isolated_reversible"
        self.registry.validate(
            "effective-execution-policy/v1", _redigest(valid_nested)
        )

    def test_policy_root_sets_reject_casefold_collisions(self) -> None:
        valid = _fixture("effective-execution-policy/v1")
        for field in ("allowed_read_roots", "allowed_write_roots"):
            policy = copy.deepcopy(valid)
            if field == "allowed_write_roots":
                policy["source_write_mode"] = "isolated_reversible"
            policy[field] = ["Root", "root"]

            with self.subTest(field=field), self.assertRaisesRegex(
                SchemaValidationError, "relative_path_casefold_collision"
            ):
                self.registry.validate(
                    "effective-execution-policy/v1", _redigest(policy)
                )

    def test_task_record_refs_pointers_and_terminal_state_are_coherent(self) -> None:
        valid = _fixture("task-record/v1")
        self.registry.validate("task-record/v1", valid)
        cases = []
        request_mismatch = copy.deepcopy(valid)
        request_mismatch["request_digest"] = "f" * 64
        cases.append(request_mismatch)
        charter_mismatch = copy.deepcopy(valid)
        charter_mismatch["charter_ref"] = copy.deepcopy(valid["request_ref"])
        cases.append(charter_mismatch)
        checkpoint_mismatch = copy.deepcopy(valid)
        checkpoint_mismatch["current_checkpoint_generation"] = 1
        cases.append(checkpoint_mismatch)
        false_terminal = copy.deepcopy(valid)
        false_terminal["lifecycle"] = "TERMINAL"
        cases.append(false_terminal)

        for instance in cases:
            with self.subTest(instance=instance), self.assertRaises(
                SchemaValidationError
            ):
                self.registry.validate("task-record/v1", instance)

    def test_task_request_source_ids_are_unique_across_explicit_inputs(self) -> None:
        valid = _fixture("task-request/v1")
        self.registry.validate("task-request/v1", valid)
        duplicate = copy.deepcopy(valid)
        duplicate["constraints"][0]["source_id"] = duplicate["acceptance_criteria"][0][
            "source_id"
        ]

        with self.assertRaisesRegex(SchemaValidationError, "source_id_duplicate"):
            self.registry.validate("task-request/v1", duplicate)

    def test_requirement_identity_and_mandatory_derivation_rules_are_strict(self) -> None:
        valid = _fixture("requirement-entry/v1")
        self.registry.validate("requirement-entry/v1", valid)
        cases = []
        wrong_prefix = copy.deepcopy(valid)
        wrong_prefix["source_kind"] = "delivery"
        cases.append(wrong_prefix)
        weakened_explicit = copy.deepcopy(valid)
        weakened_explicit["mandatory"] = False
        cases.append(weakened_explicit)
        derived_without_rationale = copy.deepcopy(valid)
        derived_without_rationale.update(
            {
                "requirement_id": "req_derived_" + "b" * 64,
                "source_kind": "derived",
                "necessity": "mechanically_necessary",
                "parent_requirement_ids": [valid["requirement_id"]],
                "rationale": "",
            }
        )
        cases.append(derived_without_rationale)
        for instance in cases:
            with self.subTest(instance=instance), self.assertRaises(
                SchemaValidationError
            ):
                self.registry.validate("requirement-entry/v1", instance)

        derived = copy.deepcopy(derived_without_rationale)
        derived["rationale"] = "Required to mechanically demonstrate the objective."
        self.registry.validate("requirement-entry/v1", derived)

    def test_legacy_mapping_schema_checks_the_domain_separated_identity(self) -> None:
        valid = _fixture("legacy-v1-task-mapping/v1")
        self.registry.validate("legacy-v1-task-mapping/v1", valid)
        forged = copy.deepcopy(valid)
        forged["task_id"] = "task_" + "f" * 32

        with self.assertRaisesRegex(SchemaValidationError, "task_id_digest_mismatch"):
            self.registry.validate("legacy-v1-task-mapping/v1", forged)

    def test_policy_fixture_carries_its_declared_canonical_digest(self) -> None:
        policy = _fixture("effective-execution-policy/v1")
        self.assertEqual(
            policy["digest"], canonical_sha256(policy, digest_field="digest")
        )

    def test_charter_digest_and_revision_predecessor_are_coherent(self) -> None:
        valid = _fixture("task-charter/v1")
        self.registry.validate("task-charter/v1", valid)
        wrong_digest = copy.deepcopy(valid)
        wrong_digest["digest"] = "0" * 64
        missing_predecessor = copy.deepcopy(valid)
        missing_predecessor["charter_version"] = 2
        missing_predecessor["digest"] = canonical_sha256(
            missing_predecessor, digest_field="digest"
        )
        for instance in (wrong_digest, missing_predecessor):
            with self.subTest(instance=instance), self.assertRaises(
                SchemaValidationError
            ):
                self.registry.validate("task-charter/v1", instance)

    def test_interaction_kind_capability_and_timestamps_are_coherent(self) -> None:
        valid = _fixture("interaction-request/v1")
        self.registry.validate("interaction-request/v1", valid)
        approval_without_capability = copy.deepcopy(valid)
        approval_without_capability["kind"] = "approval"
        input_with_capability = copy.deepcopy(valid)
        input_with_capability["requested_capability"] = "repository.write"
        deadline_before_creation = copy.deepcopy(valid)
        deadline_before_creation["deadline_at"] = "2026-07-17T11:59:59.999Z"
        for instance in (
            approval_without_capability,
            input_with_capability,
            deadline_before_creation,
        ):
            with self.subTest(instance=instance), self.assertRaises(
                SchemaValidationError
            ):
                self.registry.validate("interaction-request/v1", instance)

    def test_waiver_expiry_must_follow_issue_time(self) -> None:
        valid = _fixture("waiver-event/v1")
        self.registry.validate("waiver-event/v1", valid)
        invalid = copy.deepcopy(valid)
        invalid["expires_at"] = invalid["issued_at"]
        with self.assertRaisesRegex(SchemaValidationError, "waiver_time_window_invalid"):
            self.registry.validate("waiver-event/v1", invalid)


if __name__ == "__main__":
    unittest.main()
