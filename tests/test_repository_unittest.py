from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codereview.repository.snapshot import analyze_repository_snapshot, count_text_lines_no_follow
from codereview.repository.symbols import map_repository_symbols


class RepositoryAnalysisTest(unittest.TestCase):
    def test_repository_snapshot_counts_large_file_lines_without_full_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            checkout = Path(tmp_dir)
            path = checkout / "app.py"
            path.write_text(("x = 1\n" * 10000) + "def handler():\n", encoding="utf-8")
            inventory = {"files": [{"path": "app.py", "scope": "analyze"}]}

            snapshot = analyze_repository_snapshot(checkout, inventory)

        self.assertEqual(snapshot.spans[0]["lines"], 10001)
        self.assertEqual(snapshot.spans[0]["end"], 10001)

    def test_repository_symbols_stream_large_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            checkout = Path(tmp_dir)
            path = checkout / "app.py"
            path.write_text(("x = 1\n" * 10000) + "def handler():\n", encoding="utf-8")
            inventory = {"files": [{"path": "app.py", "scope": "analyze"}]}
            snapshot = analyze_repository_snapshot(checkout, inventory)

            symbols = map_repository_symbols(checkout, snapshot)

        self.assertIn(("handler", 10001), [(item["symbol"], item["line"]) for item in symbols])

    def test_repository_line_count_does_not_follow_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            outside = root / "outside.py"
            outside.write_text("def secret():\n", encoding="utf-8")
            link = root / "app.py"
            link.symlink_to(outside)

            with self.assertRaises(OSError):
                count_text_lines_no_follow(link)

    def test_repository_symbols_ignore_symlink_race(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            outside = root / "outside.py"
            outside.write_text("def secret():\n", encoding="utf-8")
            link = root / "app.py"
            link.symlink_to(outside)
            snapshot = type(
                "Snapshot",
                (),
                {"spans": [{"file": "app.py", "start": 1, "lines": 1, "end": 1, "kind": "repository"}]},
            )()

            symbols = map_repository_symbols(root, snapshot)

        self.assertEqual(symbols, [])


if __name__ == "__main__":
    unittest.main()
