from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest


REPO_ROOT = Path(__file__).resolve().parents[3]
MODULE_PATH = REPO_ROOT / "pullwise_worker/reviewer/qualification.py"


def load_qualification():
    if not MODULE_PATH.is_file():
        raise AssertionError("R3Q-02 qualification module is absent")
    spec = importlib.util.spec_from_file_location("reviewer_qualification_replay", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


class ReplayQualificationTest(unittest.TestCase):
    def test_report_digest_omits_only_self_reference(self) -> None:
        qualification = load_qualification()
        report = {"schema_id": "pullwise-runtime-capability-report/v1", "result": "PASS", "qualification_report_sha256": None}
        first = qualification.qualification_report_sha256(report)
        report["qualification_report_sha256"] = first
        self.assertEqual(first, qualification.qualification_report_sha256(report))

    def test_replay_bytes_are_identical_and_detached(self) -> None:
        qualification = load_qualification()
        original = {"fixtures": [{"id": "REPLAY", "status": "PASS"}]}
        first = qualification.replay_bytes(original)
        original["fixtures"][0]["status"] = "FAIL"
        self.assertEqual(first, qualification.replay_bytes({"fixtures": [{"id": "REPLAY", "status": "PASS"}]}))


if __name__ == "__main__":
    unittest.main()
