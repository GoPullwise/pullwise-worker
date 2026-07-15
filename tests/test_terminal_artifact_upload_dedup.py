from __future__ import annotations

import copy
import hashlib
import tempfile
import unittest
from pathlib import Path
from typing import Any

from pullwise_worker.review_worker_v1 import (
    DEBUG_BUNDLE_ARTIFACT_ID,
    upload_log_artifacts,
    write_json,
    write_uploaded_artifact_manifest,
)


FINAL_ARTIFACTS = {
    "codex-events.jsonl": ("art_codex_events", "codex_event_log", "application/x-ndjson"),
    "worker.log.jsonl": ("art_worker_log", "worker_log", "application/x-ndjson"),
    "progress.log.jsonl": ("art_progress_log", "progress_log", "application/x-ndjson"),
    "debug-bundle.zip": (DEBUG_BUNDLE_ARTIFACT_ID, "debug_bundle", "application/zip"),
}


class RecordingClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def artifact(self, job_id: str, artifact_id: str, payload: dict[str, Any]) -> dict[str, bool]:
        self.calls.append((job_id, artifact_id, payload))
        return {"accepted": True}


def write_terminal_artifact_fixture(root: Path) -> tuple[Path, Path, dict[str, Any]]:
    run_dir = root / "repo" / ".codex-review" / "runs" / "run_1"
    artifact_dir = root / "artifacts" / "run_1"
    run_dir.mkdir(parents=True)
    artifact_dir.mkdir(parents=True)
    contents = {
        "codex-events.jsonl": b'{"event":"stable"}\n',
        "worker.log.jsonl": b'{"event":"before"}\n',
        "progress.log.jsonl": b'{"event_type":"before"}\n',
        "debug-bundle.zip": b"initial debug bundle",
    }
    items: list[dict[str, Any]] = []
    for name, data in contents.items():
        if name != "debug-bundle.zip":
            (run_dir / name).write_bytes(data)
        (artifact_dir / name).write_bytes(data)
        artifact_id, kind, media_type = FINAL_ARTIFACTS[name]
        items.append(
            {
                "artifact_id": artifact_id,
                "kind": kind,
                "name": name,
                "media_type": media_type,
                "schema_id": None,
                "schema_version": "v1",
                "encoding": "utf-8" if media_type != "application/zip" else "binary",
                "compression": "none",
                "required": False,
                "storage": {
                    "type": "server_artifact",
                    "url": f"/v1/review-runs/run_1/artifacts/{artifact_id}",
                },
                "sha256": hashlib.sha256(data).hexdigest(),
                "size_bytes": len(data),
            }
        )
    manifest = {
        "schema_version": "artifact-manifest/v1",
        "run_id": "run_1",
        "items": items,
    }
    write_json(artifact_dir / "artifact-manifest.json", manifest)
    write_json(run_dir / "artifact-manifest.json", manifest)
    write_uploaded_artifact_manifest(
        artifact_dir,
        manifest,
        items,
        source_run_dir=run_dir,
    )
    return run_dir, artifact_dir, manifest


class TerminalArtifactUploadDedupTests(unittest.TestCase):
    def test_skips_only_previously_accepted_artifact_whose_content_and_metadata_are_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            run_dir, artifact_dir, _manifest = write_terminal_artifact_fixture(Path(tmp_dir))
            accepted_snapshot = (artifact_dir / "uploaded-artifact-manifest.json").read_bytes()
            (run_dir / "worker.log.jsonl").write_text('{"event":"after"}\n', encoding="utf-8")
            (run_dir / "progress.log.jsonl").write_text(
                '{"event_type":"run_completed"}\n',
                encoding="utf-8",
            )
            client = RecordingClient()

            upload_log_artifacts(client, "job_1", "wk_1-1", run_dir, artifact_dir)

            uploaded_names = {payload["artifact"]["name"] for _job_id, _artifact_id, payload in client.calls}
            self.assertEqual(
                uploaded_names,
                {"worker.log.jsonl", "progress.log.jsonl", "debug-bundle.zip"},
            )
            self.assertEqual(
                (artifact_dir / "uploaded-artifact-manifest.json").read_bytes(),
                accepted_snapshot,
            )
            self.assertTrue(all(payload["final_log_upload"] for _job_id, _artifact_id, payload in client.calls))

    def test_missing_acceptance_snapshot_fails_safe_by_uploading_every_final_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            run_dir, artifact_dir, _manifest = write_terminal_artifact_fixture(Path(tmp_dir))
            (artifact_dir / "uploaded-artifact-manifest.json").unlink()
            (run_dir / "uploaded-artifact-manifest.json").unlink()
            client = RecordingClient()

            upload_log_artifacts(client, "job_1", "wk_1-1", run_dir, artifact_dir)

            self.assertEqual(
                {payload["artifact"]["name"] for _job_id, _artifact_id, payload in client.calls},
                set(FINAL_ARTIFACTS),
            )

    def test_same_content_with_changed_manifest_metadata_is_reuploaded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            run_dir, artifact_dir, manifest = write_terminal_artifact_fixture(Path(tmp_dir))
            current_manifest = copy.deepcopy(manifest)
            codex_item = next(item for item in current_manifest["items"] if item["name"] == "codex-events.jsonl")
            codex_item["media_type"] = "application/octet-stream"
            write_json(artifact_dir / "artifact-manifest.json", current_manifest)
            write_json(run_dir / "artifact-manifest.json", current_manifest)
            client = RecordingClient()

            upload_log_artifacts(client, "job_1", "wk_1-1", run_dir, artifact_dir)

            uploaded_names = {payload["artifact"]["name"] for _job_id, _artifact_id, payload in client.calls}
            self.assertIn("codex-events.jsonl", uploaded_names)
            self.assertNotIn("worker.log.jsonl", uploaded_names)
            self.assertNotIn("progress.log.jsonl", uploaded_names)


if __name__ == "__main__":
    unittest.main()
