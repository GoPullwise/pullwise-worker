from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pullwise_worker.review_worker_v1 import (
    ActiveJob,
    ReviewWorkerV1,
    immutable_inventory_baseline_path,
    intent_validation_workspace_integrity_payload,
    inventory,
    materialize_artifacts,
    materialize_generated_intent_test_sources,
    prepare_validation_workspace,
    read_json,
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

    def test_declaring_validation_repo_source_does_not_allow_added_source_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            _repo, run_dir, validation_repo = self._prepared_workspace(Path(tmp_dir))
            added = validation_repo / "src" / "backdoor.py"
            added.parent.mkdir(parents=True)
            added.write_text("ENABLED = True\n", encoding="utf-8")
            write_json(
                run_dir / "intent" / "intent-test-source.json",
                {
                    "schema_version": "intent-test-source/v1",
                    "generated_tests": [
                        {"test_id": "ITV-001", "path": "src/backdoor.py"}
                    ],
                },
            )

            integrity = intent_validation_workspace_integrity_payload(run_dir)

        self.assertEqual(integrity["status"], "violation")
        self.assertTrue(
            any(
                violation.get("path") == "src/backdoor.py"
                and "added" in violation.get("reason", "")
                for violation in integrity["violations"]
            ),
            integrity,
        )

    def test_tampered_source_repo_root_cannot_materialize_external_generated_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            _repo, run_dir, validation_repo = self._prepared_workspace(root)
            external_root = root / "external-source"
            external_source = external_root / ".codex-review" / "generated-tests" / "secret.py"
            external_source.parent.mkdir(parents=True)
            external_source.write_text("SECRET = 'outside canonical source'\n", encoding="utf-8")
            validation = read_json(run_dir / "intent" / "validation-workspace.json", {})
            validation["source_repo_root"] = str(external_root)
            write_json(run_dir / "intent" / "validation-workspace.json", validation)
            source = {
                "schema_version": "intent-test-source/v1",
                "generated_tests": [
                    {"test_id": "ITV-001", "path": str(external_source)}
                ],
            }
            write_json(run_dir / "intent" / "intent-test-source.json", source)

            errors = materialize_generated_intent_test_sources(
                run_dir,
                validation_repo,
                validation,
                source,
            )
            integrity = intent_validation_workspace_integrity_payload(run_dir)
            copied = validation_repo / ".codex-review" / "generated-tests" / "secret.py"

        self.assertIn("ITV-001", errors)
        self.assertFalse(copied.exists())
        self.assertEqual(integrity["status"], "violation")
        self.assertTrue(
            any("source" in violation.get("reason", "") for violation in integrity["violations"]),
            integrity,
        )

    def test_tampered_validation_repo_root_cannot_materialize_to_external_destination(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            repo, run_dir, _validation_repo = self._prepared_workspace(root)
            generated_source = repo / ".codex-review" / "generated-tests" / "secret.py"
            generated_source.parent.mkdir(parents=True)
            generated_source.write_text("SECRET = 'canonical source'\n", encoding="utf-8")
            external_validation = root / "external-validation"
            external_validation.mkdir()
            validation = read_json(run_dir / "intent" / "validation-workspace.json", {})
            validation["validation_repo_root"] = str(external_validation)
            write_json(run_dir / "intent" / "validation-workspace.json", validation)
            source = {
                "schema_version": "intent-test-source/v1",
                "generated_tests": [
                    {"test_id": "ITV-001", "path": str(generated_source)}
                ],
            }

            errors = materialize_generated_intent_test_sources(
                run_dir,
                external_validation,
                validation,
                source,
            )
            copied = external_validation / ".codex-review" / "generated-tests" / "secret.py"

        self.assertIn("ITV-001", errors)
        self.assertFalse(copied.exists())

    def test_missing_worker_baseline_fails_closed_without_rebaselining_mutated_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo, run_dir, validation_repo = self._prepared_workspace(Path(tmp_dir))
            baseline = immutable_inventory_baseline_path(run_dir)
            baseline.unlink()
            (repo / "app.py").write_text("VALUE = 'mutated'\n", encoding="utf-8")
            (validation_repo / "app.py").write_text("VALUE = 'mutated'\n", encoding="utf-8")

            integrity = intent_validation_workspace_integrity_payload(run_dir)

        self.assertEqual(integrity["status"], "violation")
        self.assertFalse(baseline.exists())
        self.assertTrue(
            any("baseline" in violation.get("reason", "") for violation in integrity["violations"]),
            integrity,
        )


class CodexThreadLifecycleBoundaryTests(unittest.TestCase):
    def test_reviewer_threads_are_released_after_retry_and_submit_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            repo = root / "repo"
            run_dir = repo / ".codex-review" / "runs" / "run_1"
            (run_dir / "bundles").mkdir(parents=True)
            prompts_dir = repo / ".codex-review" / "prompts" / "reviewers"
            prompts_dir.mkdir(parents=True)
            write_json(run_dir / "run-state.json", {"thread_id": "root-thread"})
            write_json(
                run_dir / "bundle-plan.json",
                {
                    "schema_version": "bundle-plan/v1",
                    "bundles": [
                        {
                            "bundle_id": "p0-bundle-001",
                            "tier": "P0",
                            "reviewers": ["security"],
                        }
                    ],
                },
            )
            (run_dir / "bundles" / "p0-bundle-001.md").write_text("# bundle\n", encoding="utf-8")
            (prompts_dir / "security.md").write_text("Security reviewer\n", encoding="utf-8")

            class CodexClient:
                def __init__(self) -> None:
                    self.threads = {"root-thread": object()}
                    self.released: list[str] = []
                    self.turns = 0
                    self.next_id = 0

                def start_thread(self, *_args: object, **_kwargs: object) -> str:
                    self.next_id += 1
                    thread_id = f"reviewer-{self.next_id}"
                    self.threads[thread_id] = object()
                    return thread_id

                def release_thread(self, thread_id: str) -> None:
                    self.released.append(thread_id)
                    self.threads.pop(thread_id, None)

                def run_turn(self, **kwargs: object) -> SimpleNamespace:
                    self.turns += 1
                    if self.turns == 1:
                        raise RuntimeError("429 server busy")
                    prompt = str(kwargs["prompt"])
                    output_path = Path(
                        next(
                            line.removeprefix("Exact output path: ")
                            for line in prompt.splitlines()
                            if line.startswith("Exact output path: ")
                        )
                    )
                    write_json(
                        output_path,
                        {
                            "schema_version": "codex-reviewer-output/v1",
                            "bundle_id": "p0-bundle-001",
                            "reviewer": "security",
                            "reviewed_paths": ["src/app.py"],
                            "review_summary": "Reviewed the assigned bundle.",
                            "uncertainties": [],
                            "findings": [],
                        },
                    )
                    return SimpleNamespace(duration_ms=1)

            worker = ReviewWorkerV1(
                SimpleNamespace(worker_id="wk_1", service_home=str(root)),
                client=object(),
            )
            worker.progress_phase = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
            job = {
                "model_profile": {
                    "default_model": "gpt-5.5",
                    "core_effort": "high",
                    "non_core_effort": "medium",
                },
                "review_request": {
                    "policy": {
                        "allow_source_modification": False,
                        "allow_dependency_install": False,
                        "allow_network": False,
                        "helper_scripts_standard_library_only": True,
                        "turn_timeout_seconds": 30,
                        "reviewer_concurrency": 1,
                    },
                    "budget": {"max_wall_time_seconds": 60},
                },
                "repositoryLimits": {
                    "maxFiles": 1000,
                    "maxBytes": 10 * 1024 * 1024,
                },
            }
            codex = CodexClient()
            worker.run_reviewer_fanout_phase(
                codex,
                repo,
                run_dir,
                job,
                active=ActiveJob("job_1", "run_1", "lease_1", "wk_1-1"),
                progress=70,
            )

            submit_failure_codex = CodexClient()
            with patch(
                "pullwise_worker.review_worker_v1.ThreadPoolExecutor.submit",
                side_effect=RuntimeError("executor unavailable"),
            ):
                with self.assertRaisesRegex(RuntimeError, "executor unavailable"):
                    worker.run_reviewer_fanout_phase(
                        submit_failure_codex,
                        repo,
                        run_dir,
                        job,
                        active=ActiveJob("job_2", "run_1", "lease_2", "wk_1-2"),
                        progress=70,
                    )

        self.assertEqual(codex.turns, 2)
        self.assertEqual(set(codex.threads), {"root-thread"})
        self.assertEqual(codex.released, ["reviewer-1", "reviewer-2"])
        self.assertEqual(set(submit_failure_codex.threads), {"root-thread"})
        self.assertEqual(submit_failure_codex.released, ["reviewer-1"])


if __name__ == "__main__":
    unittest.main()
