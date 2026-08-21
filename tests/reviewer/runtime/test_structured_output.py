from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import unittest


REPO_ROOT = Path(__file__).resolve().parents[3]
MODULE_PATH = REPO_ROOT / "pullwise_worker/reviewer/qualification.py"


def load_qualification():
    if not MODULE_PATH.is_file():
        raise AssertionError("R3Q-02 qualification module is absent")
    spec = importlib.util.spec_from_file_location("reviewer_qualification", MODULE_PATH)
    if spec is None or spec.loader is None:
        raise AssertionError("R3Q-02 qualification module cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class StructuredOutputQualificationTest(unittest.TestCase):
    def test_fixture_registry_is_closed_and_complete(self) -> None:
        registry = json.loads((REPO_ROOT / "runtime/qualification-fixtures.json").read_text(encoding="utf-8"))
        self.assertEqual("pullwise-runtime-qualification-fixtures/v1", registry["schema_id"])
        self.assertEqual(
            list(load_qualification().FIXTURE_IDS),
            [item["id"] for item in registry["fixtures"]],
        )
        self.assertTrue(all(item["real"] is True for item in registry["fixtures"]))

    def test_strict_structured_payload_rejects_malformed_or_open_objects(self) -> None:
        qualification = load_qualification()
        self.assertEqual(
            {"fixture": "STRUCTURED", "status": "PASS"},
            qualification.parse_structured_payload('{"fixture":"STRUCTURED","status":"PASS"}'),
        )
        for malformed in ('{"fixture":"STRUCTURED"}', '{"fixture":"STRUCTURED","status":"PASS","extra":1}', '{'):
            with self.subTest(malformed=malformed), self.assertRaises(qualification.QualificationError):
                qualification.parse_structured_payload(malformed)


if __name__ == "__main__":
    unittest.main()
