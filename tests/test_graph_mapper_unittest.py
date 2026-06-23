from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codereview.graph import mapper


class GraphMapperTest(unittest.TestCase):
    def test_read_mapper_file_lines_rejects_oversized_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "large.py"
            path.write_bytes(b"x" * 2048)

            with self.assertRaisesRegex(OSError, "file too large"):
                mapper.read_mapper_file_lines(path, max_bytes=1024)

    def test_read_mapper_file_lines_does_not_follow_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            outside = root / "outside.py"
            outside.write_text("def secret():\n    pass\n", encoding="utf-8")
            link = root / "app.py"
            link.symlink_to(outside)

            with self.assertRaises(OSError):
                mapper.read_mapper_file_lines(link)

    def test_map_graph_task_falls_back_for_oversized_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            checkout = Path(tmp_dir)
            path = checkout / "large.py"
            path.write_bytes(b"x" * (mapper.DETERMINISTIC_MAPPER_FILE_MAX_BYTES + 1))
            inventory = {
                "large.py": {
                    "path": "large.py",
                    "scope": "analyze",
                    "line_count": 1,
                    "size_bytes": path.stat().st_size,
                    "content_hash": "hash-large",
                    "extension": ".py",
                }
            }

            result = mapper.map_graph_task(
                checkout,
                {"task_id": "graph-map-0001", "shard_id": "shard-0001", "files": ["large.py"]},
                inventory,
            )

        self.assertEqual(result["coverage"]["mapped_files"], ["large.py"])
        self.assertTrue(result["nodes"])
        self.assertIn("file too large", result["warnings"][0])


if __name__ == "__main__":
    unittest.main()
