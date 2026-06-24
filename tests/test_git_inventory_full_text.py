from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codereview.inventory.git_inventory import build_git_inventory


class GitInventoryFullTextTest(unittest.TestCase):
    def test_all_non_binary_non_generated_text_files_are_analyzable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            checkout = Path(tmp)
            (checkout / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
            (checkout / "service.custom").write_text("enabled=true\n", encoding="utf-8")
            (checkout / "requirements.lock").write_text("package==1\n", encoding="utf-8")
            (checkout / "artifact.min.js").write_text("minified()\n", encoding="utf-8")
            (checkout / "binary.bin").write_bytes(b"abc\x00def")
            vendor = checkout / "node_modules"
            vendor.mkdir()
            (vendor / "ignored.js").write_text("ignored()\n", encoding="utf-8")

            inventory = build_git_inventory(checkout, include_untracked=True)
            entries = {
                str(item.get("path") or ""): item
                for item in inventory.get("files", [])
                if isinstance(item, dict)
            }

            self.assertEqual(entries["Dockerfile"]["scope"], "analyze")
            self.assertEqual(entries["service.custom"]["scope"], "analyze")
            self.assertEqual(entries["requirements.lock"]["scope"], "analyze")
            self.assertEqual(entries["artifact.min.js"]["scope"], "excluded")
            self.assertEqual(entries["binary.bin"]["scope"], "excluded")
            self.assertNotIn("node_modules/ignored.js", entries)


if __name__ == "__main__":
    unittest.main()
