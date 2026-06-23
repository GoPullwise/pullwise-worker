from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codereview import context_adapter
from codereview.utils import jsonl


class ContextAdapterTest(unittest.TestCase):
    def test_static_seed_reads_requested_window_from_large_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            checkout = Path(tmp_dir)
            path = checkout / "app.py"
            path.write_text("".join(f"value_{index}\n" for index in range(1, 10001)), encoding="utf-8")

            seed = context_adapter.build_static_seed(checkout, symbol="target", file_path="app.py", line=9000)

        self.assertEqual(seed["snippet_start"], 8960)
        self.assertEqual(seed["snippet_end"], 9040)
        self.assertIn("9000: value_9000", seed["snippet"])

    def test_static_seed_does_not_follow_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            checkout = Path(tmp_dir) / "checkout"
            outside = Path(tmp_dir) / "outside.py"
            checkout.mkdir()
            outside.write_text("secret = True\n", encoding="utf-8")
            (checkout / "app.py").symlink_to(outside)

            seed = context_adapter.build_static_seed(checkout, symbol="target", file_path="app.py", line=1)

        self.assertEqual(seed["snippet"], [])
        self.assertEqual(seed["snippet_start"], 1)
        self.assertEqual(seed["snippet_end"], 1)

    def test_read_context_output_rejects_oversized_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "context.json"
            path.write_text('{"ok": true}\n' + ("x" * 32), encoding="utf-8")

            with patch.object(jsonl, "READ_TEXT_MAX_BYTES", 16):
                value, error = context_adapter.read_context_output(path)

        self.assertIsNone(value)
        self.assertIn("oversized JSON file", error)


if __name__ == "__main__":
    unittest.main()
