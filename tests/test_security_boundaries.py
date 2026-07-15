from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path

from pullwise_worker.review_worker_v1 import (
    intent_validation_workspace_integrity_payload,
    inventory,
    materialize_artifacts,
    prepare_validation_workspace,
    write_debug_bundle,
    write_json,
)


def _write_completed_artifact_inputs(run_dir: Path) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "report.md").write_text("# report\n", encoding="utf-8")
    for name, payload in (
        (
            "report.agent.json",
            {"schema_id": "codex-full-repo-review", "schema_version": "v1", "findings": []},
        ),
        ("coverage.json", {"schema_version": "coverage/v1"}),
        ("qa.json", {"schema_version": "qa/v1", "status": "pass"}),
        ("token-budget.json", {"schema_version": "token-budget/v1"}),
    ):
        write_json(run_dir / name, payload)


class ArtifactBoundaryTests(unittest.TestCase):
    def test_materialize_artifacts_rejects_symlinked_source_outside_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            run_dir = root / "repo" / ".codex-review" / "runs" / "run_1"
            artifact_dir = root / "artifacts" / "run_1"
            _write_completed_artifact_inputs(run_dir)
            outside = root / "outside-report.md"
            outside.write_text("outside secret\n", encoding="utf-8")
            (run_dir / "report.md").unlink()
            try:
                os.symlink(outside, run_dir / "report.md")
            except OSError as exc:
                self.skipTest(f"symlink creation is unavailable: {exc}")

            with self.assertRaisesRegex(RuntimeError, "artifact source.*run directory"):
                materialize_artifacts(run_dir, artifact_dir)

    def test_debug_bundle_excludes_packed_repository_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            run_dir = root / "repo" / ".codex-review" / "runs" / "run_1"
            artifact_dir = root / "artifacts" / "run_1"
            bundle_file = run_dir / "bundles" / "p0-bundle-001.md"
            bundle_file.parent.mkdir(parents=True)
            bundle_file.write_text("UNIQUE_REPOSITORY_SOURCE_SECRET\n", encoding="utf-8")

            debug_bundle = write_debug_bundle(run_dir, artifact_dir, status="completed")

            with zipfile.ZipFile(debug_bundle) as archive:
                names = archive.namelist()
                contents = b"\n".join(archive.read(name) for name in names)

        self.assertNotIn("run/bundles/p0-bundle-001.md", names)
        self.assertNotIn(b"UNIQUE_REPOSITORY_SOURCE_SECRET", contents)


class ValidationWorkspaceIntegrityBoundaryTests(unittest.TestCase):
    def _prepared_workspace(self, root: Path) -> tuple[Path, Path, Path]:
        repo = root / "repo"
        repo.mkdir()
        (repo / "app.py").write_text("VALUE = 'original'\n", encoding="utf-8")
        run_dir = repo / ".codex-review" / "runs" / "run_1"
        write_json(run_dir / "inventory.json", inventory(repo))
        prepare_validation_workspace(repo, run_dir)
        return repo, run_dir, root / "validation-repo"

    def test_integrity_ignores_tampered_mutable_inventory_hash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            _repo, run_dir, validation_repo = self._prepared_workspace(Path(tmp_dir))
            validation_source = validation_repo / "app.py"
            validation_source.write_text("VALUE = 'tampered'\n", encoding="utf-8")

            mutable_inventory = json.loads((run_dir / "inventory.json").read_text(encoding="utf-8"))
            mutable_inventory["files"][0]["sha256"] = hashlib.sha256(validation_source.read_bytes()).hexdigest()
            write_json(run_dir / "inventory.json", mutable_inventory)

            integrity = intent_validation_workspace_integrity_payload(run_dir)

        self.assertEqual(integrity["status"], "violation")
        self.assertTrue(
            any(
                violation.get("path") == "app.py" and "immutable" in violation.get("reason", "")
                for violation in integrity["violations"]
            )
        )

    def test_integrity_detects_undeclared_added_source_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            _repo, run_dir, validation_repo = self._prepared_workspace(Path(tmp_dir))
            (validation_repo / "backdoor.py").write_text("ENABLED = True\n", encoding="utf-8")

            integrity = intent_validation_workspace_integrity_payload(run_dir)

        self.assertEqual(integrity["status"], "violation")
        self.assertTrue(
            any(
                violation.get("path") == "backdoor.py" and "added" in violation.get("reason", "")
                for violation in integrity["violations"]
            )
        )


if __name__ == "__main__":
    unittest.main()
