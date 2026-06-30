from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codereview.inventory.file_hashes import sha256_file
from codereview.snapshot import create_immutable_snapshot, source_state_from_inventory


class ImmutableSnapshotTest(unittest.TestCase):
    def test_source_state_from_inventory_uses_analyzable_hashes(self) -> None:
        inventory = {
            "files": [
                {"path": "app.py", "content_hash": "sha256:aaa", "scope": "analyze"},
                {"path": "dist/app.js", "content_hash": "sha256:bbb", "scope": "excluded"},
            ],
            "summary": {"files": 2, "analyzable_files": 1},
        }

        state = source_state_from_inventory(inventory)

        self.assertTrue(str(state["manifest_hash"]).startswith("sha256:"))
        self.assertEqual(state["files"], [{"path": "app.py", "content_hash": "sha256:aaa", "scope": "analyze"}])
        self.assertEqual(state["summary"], inventory["summary"])

    def test_snapshot_rejects_inventory_paths_outside_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            checkout = root / "repo"
            checkout.mkdir()
            outside = root / "outside.py"
            outside.write_text("print('outside')\n", encoding="utf-8")
            run = checkout / ".codereview" / "runs" / "run_1"

            with self.assertRaisesRegex(RuntimeError, "immutable snapshot missing analyzable inventory files"):
                create_immutable_snapshot(
                    checkout,
                    {"files": [{"path": "../outside.py", "content_hash": sha256_file(outside), "scope": "analyze"}]},
                    run,
                )

            snapshot_repo = run / "workers" / "coordinator" / "snapshot" / "repo"
            self.assertFalse((snapshot_repo / "outside.py").exists())
            self.assertEqual(outside.read_text(encoding="utf-8"), "print('outside')\n")

    def test_snapshot_rejects_ancestor_symlink_escape(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            checkout = root / "repo"
            checkout.mkdir()
            outside = root / "outside"
            outside.mkdir()
            outside_file = outside / "secret.py"
            outside_file.write_text("print('secret')\n", encoding="utf-8")
            (checkout / "linked").symlink_to(outside, target_is_directory=True)
            run = checkout / ".codereview" / "runs" / "run_1"

            with self.assertRaisesRegex(RuntimeError, "immutable snapshot missing analyzable inventory files"):
                create_immutable_snapshot(
                    checkout,
                    {"files": [{"path": "linked/secret.py", "content_hash": sha256_file(outside_file), "scope": "analyze"}]},
                    run,
                )

            snapshot_repo = run / "workers" / "coordinator" / "snapshot" / "repo"
            self.assertFalse((snapshot_repo / "linked" / "secret.py").exists())
            self.assertEqual(outside_file.read_text(encoding="utf-8"), "print('secret')\n")

    def test_snapshot_rejects_inventory_hash_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            checkout = Path(tmp_dir) / "repo"
            checkout.mkdir()
            source = checkout / "app.py"
            source.write_text("print('ok')\n", encoding="utf-8")
            run = checkout / ".codereview" / "runs" / "run_1"

            with self.assertRaisesRegex(RuntimeError, "immutable snapshot inventory hash mismatch"):
                create_immutable_snapshot(
                    checkout,
                    {"files": [{"path": "app.py", "content_hash": "sha256:wrong", "scope": "analyze"}]},
                    run,
                )

    def test_snapshot_manifest_records_inventory_hash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            checkout = Path(tmp_dir) / "repo"
            checkout.mkdir()
            source = checkout / "app.py"
            source.write_text("print('ok')\n", encoding="utf-8")
            inventory = {"files": [{"path": "app.py", "content_hash": sha256_file(source), "scope": "analyze"}]}
            run = checkout / ".codereview" / "runs" / "run_1"

            manifest = create_immutable_snapshot(checkout, inventory, run)

            self.assertTrue(str(manifest["inventory_manifest_hash"]).startswith("sha256:"))
            self.assertEqual(manifest["copied_file_hashes"], {"app.py": sha256_file(source)})

    def test_snapshot_codereview_asset_copy_does_not_follow_internal_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            checkout = root / "repo"
            prompts = checkout / ".codereview" / "prompts"
            schemas = checkout / ".codereview" / "schemas"
            prompts.mkdir(parents=True)
            schemas.mkdir()
            source = checkout / "app.py"
            source.write_text("print('ok')\n", encoding="utf-8")
            outside = root / "outside.md"
            outside.write_text("secret\n", encoding="utf-8")
            (prompts / "finder.md").symlink_to(outside)
            (prompts / "safe.md").write_text("safe\n", encoding="utf-8")
            run = checkout / ".codereview" / "runs" / "run_1"

            create_immutable_snapshot(
                checkout,
                {"files": [{"path": "app.py", "content_hash": sha256_file(source), "scope": "analyze"}]},
                run,
            )

            snapshot_prompts = run / "workers" / "coordinator" / "snapshot" / "repo" / ".codereview" / "prompts"
            self.assertFalse((snapshot_prompts / "finder.md").exists())
            self.assertEqual((snapshot_prompts / "safe.md").read_text(encoding="utf-8"), "safe\n")
            self.assertEqual(outside.read_text(encoding="utf-8"), "secret\n")


if __name__ == "__main__":
    unittest.main()
