from __future__ import annotations

import unittest

from pullwise_worker.agent_kernel_identity import (
    AgentKernelIdentityError,
    assert_same_legacy_identity,
    legacy_v1_task_mapping,
)
from pullwise_worker.agent_kernel_schema_registry import SchemaRegistry


class AgentKernelLegacyMappingTest(unittest.TestCase):
    def test_unicode_mapping_is_deterministic_and_schema_valid(self) -> None:
        first = legacy_v1_task_mapping("扫描-α", 3)
        second = legacy_v1_task_mapping("扫描-α", 3)

        self.assertEqual(first, second)
        self.assertEqual("task_6101dbd3b5a7cc902d90e40185c6e158", first["task_id"])
        self.assertEqual(3, first["transport_epoch"])
        SchemaRegistry().validate("legacy-v1-task-mapping/v1", first)

    def test_mapping_rejects_empty_non_nfc_and_invalid_attempt(self) -> None:
        cases = (
            (("", 1), "scan_id_invalid"),
            (("e\u0301", 1), "scan_id_not_nfc"),
            (("scan", 0), "transport_epoch_invalid"),
            (("scan", True), "transport_epoch_invalid"),
            (("scan", 2**53), "transport_epoch_invalid"),
        )
        for arguments, reason in cases:
            with self.subTest(arguments=arguments), self.assertRaisesRegex(
                AgentKernelIdentityError, reason
            ):
                legacy_v1_task_mapping(*arguments)

    def test_existing_task_requires_byte_identical_original_scan_id(self) -> None:
        mapping = legacy_v1_task_mapping("scan-a", 1)
        assert_same_legacy_identity(
            existing_task_id=mapping["task_id"],
            existing_scan_id="scan-a",
            incoming=mapping,
        )

        with self.assertRaisesRegex(AgentKernelIdentityError, "TASK_ID_COLLISION"):
            assert_same_legacy_identity(
                existing_task_id=mapping["task_id"],
                existing_scan_id="scan-b",
                incoming=mapping,
            )

    def test_injected_hash_collision_never_reuses_another_scan_task(self) -> None:
        constant_digest = lambda _: b"\x00" * 32
        first = legacy_v1_task_mapping("scan-a", 1, digest=constant_digest)
        second = legacy_v1_task_mapping("scan-b", 1, digest=constant_digest)
        self.assertEqual(first["task_id"], second["task_id"])

        with self.assertRaisesRegex(AgentKernelIdentityError, "TASK_ID_COLLISION"):
            assert_same_legacy_identity(
                existing_task_id=first["task_id"],
                existing_scan_id=first["scan_id"],
                incoming=second,
            )


if __name__ == "__main__":
    unittest.main()
