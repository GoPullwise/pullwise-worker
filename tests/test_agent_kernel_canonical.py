from __future__ import annotations

import json
from pathlib import Path
import unittest

from pullwise_worker.agent_kernel_canonical import (
    CanonicalizationError,
    canonical_bytes,
    canonical_sha256,
    load_strict_json,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PATH = (
    REPO_ROOT / "contracts" / "agent-task" / "v1" / "fixtures" / "canonical-json.json"
)


class AgentKernelCanonicalTest(unittest.TestCase):
    def test_golden_fixture_is_byte_exact_across_key_order(self) -> None:
        fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        case = fixture["cases"][0]

        self.assertEqual(
            case["canonical_utf8"].encode("utf-8"),
            canonical_bytes(case["input"]),
        )
        self.assertEqual(case["sha256"], canonical_sha256(case["input"]))
        reordered = {key: case["input"][key] for key in reversed(case["input"])}
        self.assertEqual(canonical_bytes(case["input"]), canonical_bytes(reordered))

    def test_declared_top_level_digest_field_is_excluded_without_mutation(self) -> None:
        payload = {"schema_id": "example/v1", "value": 7, "digest": "old"}

        digest = canonical_sha256(payload, digest_field="digest")

        self.assertEqual(
            canonical_sha256({"schema_id": "example/v1", "value": 7}),
            digest,
        )
        self.assertEqual("old", payload["digest"])

    def test_profile_rejects_float_unsafe_integer_and_non_string_key(self) -> None:
        cases = (
            ({"value": 1.5}, "float_not_supported"),
            ({"value": 2**53}, "integer_out_of_range"),
            ({1: "value"}, "object_key_not_string"),
        )
        for payload, reason in cases:
            with self.subTest(reason=reason), self.assertRaisesRegex(
                CanonicalizationError, reason
            ):
                canonical_bytes(payload)

    def test_profile_rejects_non_ascii_keys_non_nfc_and_invalid_unicode(self) -> None:
        cases = (
            ({"é": "value"}, "object_key_not_ascii"),
            ({"value": "e\u0301"}, "string_not_nfc"),
            ({"value": "\ud800"}, "string_not_utf8"),
        )
        for payload, reason in cases:
            with self.subTest(reason=reason), self.assertRaisesRegex(
                CanonicalizationError, reason
            ):
                canonical_bytes(payload)

    def test_strict_loader_rejects_duplicate_keys_floats_and_non_utf8(self) -> None:
        cases = (
            (b'{"a":1,"a":2}', "duplicate_object_key"),
            (b'{"value":1.5}', "float_not_supported"),
            (b'{"value":NaN}', "non_finite_number"),
            (b'\xff', "json_not_utf8"),
        )
        for payload, reason in cases:
            with self.subTest(reason=reason), self.assertRaisesRegex(
                CanonicalizationError, reason
            ):
                load_strict_json(payload)

    def test_strict_loader_returns_profile_validated_value(self) -> None:
        payload = b'{"z":2,"a":[true,null,-3]}'

        value = load_strict_json(payload)

        self.assertEqual(b'{"a":[true,null,-3],"z":2}', canonical_bytes(value))


if __name__ == "__main__":
    unittest.main()
