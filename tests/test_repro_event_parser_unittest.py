from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codereview.repro.event_parser import MAX_EVENT_STREAM_BYTES, event_stream_text
from codereview.repro.runner import validate_repro_result


class ReproEventParserTest(unittest.TestCase):
    def test_repro_runner_imports_without_legacy_judge_modules(self) -> None:
        self.assertEqual(
            validate_repro_result({"candidate_id": "cand", "status": "blocked"}, expected_candidate_id="cand"),
            [],
        )

    def test_event_stream_text_does_not_follow_symlinked_event_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            outside = root / "outside.jsonl"
            outside.write_text("secret\n", encoding="utf-8")
            event_path = root / "events.jsonl"
            event_path.symlink_to(outside)

            self.assertEqual(event_stream_text(event_path), "")
            self.assertEqual(outside.read_text(encoding="utf-8"), "secret\n")

    def test_event_stream_text_rejects_symlinked_ancestor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            outside = root / "outside"
            outside.mkdir()
            (outside / "events.jsonl").write_text("secret\n", encoding="utf-8")
            linked = root / "linked"
            linked.symlink_to(outside, target_is_directory=True)

            self.assertEqual(event_stream_text(linked / "events.jsonl"), "")
            self.assertEqual((outside / "events.jsonl").read_text(encoding="utf-8"), "secret\n")
    def test_event_stream_text_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            event_path = Path(tmp_dir) / "events.jsonl"
            event_path.write_bytes(b"a" * (MAX_EVENT_STREAM_BYTES + 1024))

            text = event_stream_text(event_path)

            self.assertEqual(len(text), MAX_EVENT_STREAM_BYTES)


if __name__ == "__main__":
    unittest.main()
