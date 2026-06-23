from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codereview.judge.logs import MAX_REPRO_LOG_BYTES, read_worker_log_text
from codereview.judge.validate import validate_log


class JudgeLogReadTest(unittest.TestCase):
    def test_validate_log_rejects_symlinked_log_outside_worker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            worker = root / "worker"
            logs = worker / "logs"
            logs.mkdir(parents=True)
            outside = root / "outside.log"
            outside.write_text("secret observable\n", encoding="utf-8")
            (logs / "repro.log").symlink_to(outside)

            error = validate_log(worker, "logs/repro.log", "secret observable")

            self.assertIn("outside worker directory", error)
            self.assertEqual(outside.read_text(encoding="utf-8"), "secret observable\n")

    def test_worker_log_text_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            worker = Path(tmp_dir) / "worker"
            logs = worker / "logs"
            logs.mkdir(parents=True)
            (logs / "repro.log").write_bytes(b"a" * (MAX_REPRO_LOG_BYTES + 1024))

            text, error = read_worker_log_text(worker, "logs/repro.log")

            self.assertEqual(error, "")
            self.assertEqual(len(text), MAX_REPRO_LOG_BYTES)


if __name__ == "__main__":
    unittest.main()
