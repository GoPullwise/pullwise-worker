from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codereview.utils import jsonl


class JsonlUtilsTest(unittest.TestCase):
    def test_read_text_rejects_oversized_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "large.json"
            path.write_bytes(b"x" * (jsonl.READ_TEXT_MAX_BYTES + 1))

            with self.assertRaisesRegex(OSError, "oversized JSON file"):
                jsonl.read_text(path)

    def test_read_text_rejects_post_open_non_regular_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "not_regular.json"
            path.write_text("{}", encoding="utf-8")
            real_fstat = os.fstat

            def fake_fstat(fd: int) -> os.stat_result:
                result = real_fstat(fd)
                values = list(result)
                values[0] = stat.S_IFDIR | 0o700
                return os.stat_result(values)

            with patch.object(jsonl.os, "fstat", fake_fstat):
                with self.assertRaisesRegex(OSError, "non-regular JSON file"):
                    jsonl.read_text(path)

    def test_read_json_returns_default_for_oversized_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "large.json"
            path.write_bytes(b"x" * (jsonl.READ_TEXT_MAX_BYTES + 1))

            value = jsonl.read_json(path, default={"ok": True})

        self.assertEqual(value, {"ok": True})

    def test_read_json_strict_rejects_oversized_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "large.json"
            path.write_bytes(b"x" * (jsonl.READ_TEXT_MAX_BYTES + 1))

            with self.assertRaisesRegex(OSError, "oversized JSON file"):
                jsonl.read_json_strict(path)

    def test_read_jsonl_returns_empty_for_oversized_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "large.jsonl"
            path.write_bytes(b"x" * (jsonl.READ_TEXT_MAX_BYTES + 1))

            value = jsonl.read_jsonl(path)

        self.assertEqual(value, [])


if __name__ == "__main__":
    unittest.main()
