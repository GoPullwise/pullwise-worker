from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest


REPO_ROOT = Path(__file__).resolve().parents[3]
MODULE_PATH = REPO_ROOT / "pullwise_worker/reviewer/qualification.py"


def load_qualification():
    if not MODULE_PATH.is_file():
        raise AssertionError("R3Q-02 qualification module is absent")
    spec = importlib.util.spec_from_file_location("reviewer_qualification_interrupt", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


class InterruptTimeoutQualificationTest(unittest.TestCase):
    def test_timeout_reaps_real_descendant_process_group(self) -> None:
        qualification = load_qualification()
        fixture = json.loads((Path(__file__).parent / "fixtures/close-hang.json").read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as directory:
            result = qualification.ProcessSupervisor().run(
                [sys.executable, "-c", fixture["child_code"]],
                cwd=Path(directory),
                env={"PATH": str(Path(sys.executable).parent)},
                timeout_seconds=fixture["timeout_seconds"],
            )
        self.assertTrue(result.timed_out)
        self.assertTrue(result.process_group_reaped)

    def test_publication_fence_rejects_late_output(self) -> None:
        qualification = load_qualification()
        fence = qualification.PublicationFence("attempt-1")
        self.assertEqual(b"first", fence.publish("attempt-1", b"first"))
        fence.close()
        with self.assertRaises(qualification.QualificationError):
            fence.publish("attempt-1", b"late")


if __name__ == "__main__":
    unittest.main()
