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
    payload = json.loads(
        (CONTRACT_ROOT / "fixtures" / "schema-golden.json").read_text(
            encoding="utf-8"
        )
    )
    return copy.deepcopy(
        next(case["valid"] for case in payload["cases"] if case["schema_id"] == schema_id)
    )


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


if __name__ == "__main__":
    unittest.main()
