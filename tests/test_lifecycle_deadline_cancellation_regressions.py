from __future__ import annotations

import json
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pullwise_worker import review_worker_v1 as worker_module
from pullwise_worker.review_worker_v1 import (
    ActiveJob,
    JobCancelled,
    ReviewWorkerV1,
    clone_repository_checkout,
    copy_tree,
    prepare_validation_workspace,
    run_git,
    run_intent_tests,
    start_codex_thread_with_lifecycle,
    write_json,
)


def worker_config(root: Path) -> SimpleNamespace:
    return SimpleNamespace(
        worker_id="wk_1",
        service_home=str(root),
        worker_root=str(root),
        work_dir=str(root),
    )


def review_job(*, checkout_dir: Path | None = None, wall_time_seconds: int = 30) -> dict:
    job = {
        "job_id": "job_1",
        "run_id": "run_1",
        "lease_id": "lease_1",
        "attempt": 1,
        "repo": "org/repo",
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
                "turn_timeout_seconds": 1800,
                "reviewer_concurrency": 1,
            },
            "budget": {"max_wall_time_seconds": wall_time_seconds},
        },
        "repositoryLimits": {"maxFiles": 2000, "maxBytes": 50 * 1024 * 1024},
    }
    if checkout_dir is not None:
        job["checkout_dir"] = str(checkout_dir)
    return job


def prepare_intent_run(root: Path) -> Path:
    repo = root / "repo"
    run_dir = repo / ".codex-review" / "runs" / "run_1"
    intent_dir = run_dir / "intent"
    intent_dir.mkdir(parents=True)
    validation = prepare_validation_workspace(repo, run_dir)
    validation_repo = root / "validation-repo"
    generated_test = repo / ".codex-review" / "generated-tests" / "test_delegated.py"
    generated_test.parent.mkdir(parents=True)
    generated_test.write_text("import unittest\n", encoding="utf-8")
    write_json(
        intent_dir / "validation-workspace.json",
        validation,
    )
    write_json(
        intent_dir / "intent-test-validation.json",
        {
            "schema_version": "intent-test-validation/v1",
            "enabled": True,
            "max_total_test_run_seconds": 60,
            "max_test_run_seconds_per_test": 60,
        },
    )
    write_json(
        intent_dir / "intent-test-source.json",
        {
            "schema_version": "intent-test-source/v1",
            "execution": {
                "ran": False,
                "reason": "Execution is delegated to the intent_test_running phase.",
            },
            "generated_tests": [
                {
                    "test_id": "ITV-001",
                    "path": ".codex-review/generated-tests/test_delegated.py",
                    "command": [
                        "python3",
                        "-m",
                        "unittest",
                        ".codex-review/generated-tests/test_delegated.py",
                    ],
                    "test_framework": "unittest",
                    "artifact_refs": ["art_intent_test_source"],
                }
            ],
        },
    )
    return run_dir


class _JoinedThread:
    def join(self) -> None:
        return None


class LifecycleDeadlineCancellationRegressionsTest(unittest.TestCase):
    def test_thread_start_adapter_keeps_missing_identifier_empty(self) -> None:
        class CodexClient:
            def start_thread(self, *_args: object, **_kwargs: object) -> None:
                return None

        thread_id = start_codex_thread_with_lifecycle(
            CodexClient(),
            Path("."),
            "gpt-5.5",
            timeout_seconds=30,
            cancel_requested=lambda: False,
        )

        self.assertEqual(thread_id, "")

    def test_run_job_persists_active_marker_before_checkout_and_keeps_it_when_result_is_unconfirmed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            marker_path = root / "runtime" / "active-run.json"
            marker_seen: list[dict] = []

            class Worker(ReviewWorkerV1):
                def start_active_job_supervisor(self, _active: ActiveJob) -> tuple[threading.Event, _JoinedThread]:
                    return threading.Event(), _JoinedThread()

                def prepare_workspace(self, _job: dict, _run_id: str, **_kwargs: object) -> tuple[Path, Path, Path]:
                    marker_seen.append(json.loads(marker_path.read_text(encoding="utf-8")))
                    raise RuntimeError("checkout failed")

                def emit_event(self, *_args: object, **_kwargs: object) -> None:
                    return None

                def heartbeat(self) -> None:
                    return None

                def build_envelope(self, *_args: object, **_kwargs: object) -> dict:
                    return {
                        "protocol_version": "review-worker-protocol/v1",
                        "execution": {"status": "failed"},
                    }

                def submit_result_or_record_failure(self, *_args: object, **_kwargs: object) -> bool:
                    return False

            Worker(worker_config(root), client=object()).run_job(review_job())

            self.assertEqual(len(marker_seen), 1)
            self.assertEqual(marker_seen[0]["job_id"], "job_1")
            self.assertEqual(marker_seen[0]["run_id"], "run_1")
            self.assertEqual(marker_seen[0]["current_phase"], "prepare_workspace")
            self.assertTrue(marker_path.is_file())

    def test_run_job_clears_active_marker_only_after_terminal_result_is_confirmed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            marker_path = root / "runtime" / "active-run.json"
            marker_was_present: list[bool] = []

            class Worker(ReviewWorkerV1):
                def start_active_job_supervisor(self, _active: ActiveJob) -> tuple[threading.Event, _JoinedThread]:
                    return threading.Event(), _JoinedThread()

                def prepare_workspace(self, _job: dict, _run_id: str, **_kwargs: object) -> tuple[Path, Path, Path]:
                    marker_was_present.append(marker_path.is_file())
                    raise RuntimeError("checkout failed")

                def emit_event(self, *_args: object, **_kwargs: object) -> None:
                    return None

                def heartbeat(self) -> None:
                    return None

                def build_envelope(self, *_args: object, **_kwargs: object) -> dict:
                    return {
                        "protocol_version": "review-worker-protocol/v1",
                        "execution": {"status": "failed"},
                    }

                def submit_result_or_record_failure(self, *_args: object, **_kwargs: object) -> bool:
                    return True

            Worker(worker_config(root), client=object()).run_job(review_job())

            self.assertEqual(marker_was_present, [True])
            self.assertFalse(marker_path.exists())

    def test_completed_result_honors_cancellation_that_won_before_commit(self) -> None:
        class Client:
            def __init__(self) -> None:
                self.results: list[tuple[str, dict]] = []

            def result(self, job_id: str, payload: dict) -> None:
                self.results.append((job_id, payload))

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            artifact_dir = root / "artifacts" / "run_1"
            client = Client()
            worker = ReviewWorkerV1(worker_config(root), client=client)
            active = ActiveJob("job_1", "run_1", "lease_1", "wk_1-1")
            active.cancel_requested = True
            active.cancel_reason = "control plane cancellation"

            with self.assertRaisesRegex(JobCancelled, "control plane cancellation"):
                worker.submit_result_or_record_failure(
                    active,
                    "job_1",
                    {"status": "done"},
                    artifact_dir,
                    {
                        "protocol_version": "review-worker-protocol/v1",
                        "execution": {"status": "completed"},
                    },
                )

            self.assertEqual(client.results, [])
            self.assertFalse(active.terminal_result_in_flight)
            self.assertFalse((artifact_dir / "result-submit-failed.json").exists())

    def test_terminal_result_commit_atomically_rejects_stale_heartbeat_cancellation(self) -> None:
        cancel_call_entered = threading.Event()
        allow_cancel_call = threading.Event()
        result_call_entered = threading.Event()
        allow_result_call = threading.Event()

        class Client:
            def heartbeat(self, **_kwargs: object) -> dict:
                return {"cancelled_job_ids": ["job_1"]}

            def result(self, _job_id: str, _payload: dict) -> None:
                result_call_entered.set()
                if not allow_result_call.wait(5):
                    raise TimeoutError("test did not release result commit")

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            artifact_dir = root / "artifacts" / "run_1"
            run_dir = root / "workspaces" / "run_1" / "repo" / ".codex-review" / "runs" / "run_1"
            run_dir.mkdir(parents=True)
            worker = ReviewWorkerV1(worker_config(root), client=Client())
            worker.codex_client = SimpleNamespace(is_running=lambda: True)  # type: ignore[assignment]
            worker.quota_monitor.snapshot_if_due = lambda **_kwargs: {  # type: ignore[method-assign]
                "provider": "codex",
                "status": "ready",
                "ready": True,
            }
            worker.machine_metrics_payload = lambda: None  # type: ignore[method-assign]
            persisted_states: list[str] = []
            emitted_cancellations: list[str] = []
            worker.persist_active_run_marker = (  # type: ignore[method-assign]
                lambda active: persisted_states.append(active.state)
            )
            worker.emit_cancel_requested = (  # type: ignore[method-assign]
                lambda active, _run_dir: emitted_cancellations.append(active.cancel_reason)
            )
            active = ActiveJob("job_1", "run_1", "lease_1", "wk_1-1")
            active.run_dir = run_dir
            worker.state.set_active(active)
            initial_state = active.state
            original_request_cancel = worker.request_cancel
            cancellation_results: list[object] = []

            def delayed_request_cancel(active_job: ActiveJob, *, reason: str) -> object:
                cancel_call_entered.set()
                if not allow_cancel_call.wait(5):
                    raise TimeoutError("test did not release cancellation attempt")
                accepted = original_request_cancel(active_job, reason=reason)
                cancellation_results.append(accepted)
                return accepted

            worker.request_cancel = delayed_request_cancel  # type: ignore[method-assign]
            heartbeat_errors: list[BaseException] = []
            result_errors: list[BaseException] = []
            submitted: list[bool] = []

            def heartbeat_call() -> None:
                try:
                    worker._heartbeat_once(process_worker_command=False)
                except BaseException as exc:  # noqa: BLE001 - asserted below.
                    heartbeat_errors.append(exc)

            def result_call() -> None:
                try:
                    submitted.append(
                        worker.submit_result_or_record_failure(
                            active,
                            "job_1",
                            {"status": "done"},
                            artifact_dir,
                            {
                                "protocol_version": "review-worker-protocol/v1",
                                "execution": {"status": "completed"},
                            },
                        )
                    )
                except BaseException as exc:  # noqa: BLE001 - asserted below.
                    result_errors.append(exc)

            heartbeat_thread = threading.Thread(target=heartbeat_call, daemon=True)
            result_thread = threading.Thread(target=result_call, daemon=True)
            try:
                heartbeat_thread.start()
                self.assertTrue(
                    cancel_call_entered.wait(2),
                    "heartbeat never reached its stale cancellation decision",
                )
                result_thread.start()
                self.assertTrue(
                    result_call_entered.wait(2),
                    "completed result never entered its in-flight commit",
                )
                self.assertTrue(active.terminal_result_in_flight)
                allow_cancel_call.set()
                heartbeat_thread.join(2)
                self.assertFalse(heartbeat_thread.is_alive())
                self.assertFalse(active.cancel_requested)
                self.assertEqual(active.state, initial_state)
                self.assertEqual(persisted_states, [])
                self.assertEqual(emitted_cancellations, [])
                self.assertEqual(cancellation_results, [False])
            finally:
                allow_cancel_call.set()
                allow_result_call.set()
                heartbeat_thread.join(2)
                result_thread.join(2)

            self.assertFalse(result_thread.is_alive())
            self.assertEqual(heartbeat_errors, [])
            self.assertEqual(result_errors, [])
            self.assertEqual(submitted, [True])
            self.assertTrue(active.terminal_result_submitted)

    def test_cancelled_result_can_commit_after_cancellation_was_recorded(self) -> None:
        class Client:
            def __init__(self) -> None:
                self.results: list[tuple[str, dict]] = []

            def result(self, job_id: str, payload: dict) -> None:
                self.results.append((job_id, payload))

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            client = Client()
            worker = ReviewWorkerV1(worker_config(root), client=client)
            active = ActiveJob("job_1", "run_1", "lease_1", "wk_1-1")
            active.cancel_requested = True

            submitted = worker.submit_result_or_record_failure(
                active,
                "job_1",
                {"status": "cancelled"},
                root / "artifacts" / "run_1",
                {
                    "protocol_version": "review-worker-protocol/v1",
                    "execution": {"status": "cancelled"},
                },
            )

            self.assertTrue(submitted)
            self.assertEqual(len(client.results), 1)

    def test_copy_tree_polls_cancellation_during_checkout_copy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            source = root / "source"
            destination = root / "destination"
            source.mkdir()
            for index in range(5):
                (source / f"file-{index}.txt").write_text("source", encoding="utf-8")
            checks = 0

            def cancel_requested() -> bool:
                nonlocal checks
                checks += 1
                return checks >= 2

            with self.assertRaises(JobCancelled):
                copy_tree(source, destination, cancel_requested=cancel_requested)

            self.assertGreaterEqual(checks, 2)
            self.assertLess(len(list(destination.rglob("*.txt"))), 5)

    def test_clone_propagates_cancellation_without_retrying_git(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            cancel_requested = lambda: False
            with patch(
                "pullwise_worker.review_worker_v1.run_git",
                side_effect=JobCancelled("cancel requested"),
            ) as git:
                with self.assertRaises(JobCancelled):
                    clone_repository_checkout(
                        {
                            "repo": "org/repo",
                            "branch": "main",
                            "repository": {"clone_url": "https://example.invalid/org/repo.git"},
                        },
                        root / "repo",
                        mirror_cache_root=root / "mirrors",
                        deadline_monotonic=time.monotonic() + 60,
                        cancel_requested=cancel_requested,
                    )

            self.assertEqual(git.call_count, 1)
            self.assertIs(git.call_args.kwargs["cancel_requested"], cancel_requested)

    def test_git_process_polls_cancellation_while_clone_command_is_running(self) -> None:
        class HangingProcess:
            def __init__(self) -> None:
                self.args = ["git", "fetch"]
                self.returncode: int | None = None
                self.terminated = False

            def communicate(self, timeout: float | None = None) -> tuple[str, str]:
                if self.returncode is not None:
                    return "", ""
                raise subprocess.TimeoutExpired(self.args, timeout or 0)

            def poll(self) -> int | None:
                return self.returncode

            def terminate(self) -> None:
                self.terminated = True
                self.returncode = -15

            def kill(self) -> None:
                self.returncode = -9

            def wait(self, timeout: float | None = None) -> int:
                del timeout
                return int(self.returncode or 0)

        process = HangingProcess()
        checks = 0

        def cancel_requested() -> bool:
            nonlocal checks
            checks += 1
            return checks >= 2

        with patch("pullwise_worker.review_worker_v1.subprocess.Popen", return_value=process):
            with self.assertRaises(JobCancelled):
                run_git(
                    ["git", "fetch"],
                    env={},
                    deadline_monotonic=time.monotonic() + 60,
                    cancel_requested=cancel_requested,
                )

        self.assertTrue(process.terminated)
        self.assertGreaterEqual(checks, 2)

    def test_run_job_passes_one_absolute_deadline_from_checkout_into_phases(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            observed: list[tuple[str, float]] = []

            class Worker(ReviewWorkerV1):
                def start_active_job_supervisor(self, _active: ActiveJob) -> tuple[threading.Event, _JoinedThread]:
                    return threading.Event(), _JoinedThread()

                def prepare_workspace(
                    self,
                    _job: dict,
                    run_id: str,
                    *,
                    deadline_monotonic: float,
                    cancel_requested: object,
                ) -> tuple[Path, Path, Path]:
                    self.assert_cancel_callback(cancel_requested)
                    observed.append(("checkout", deadline_monotonic))
                    repo_dir = root / "workspaces" / run_id / "repo"
                    run_dir = repo_dir / ".codex-review" / "runs" / run_id
                    artifact_dir = root / "artifacts" / run_id
                    run_dir.mkdir(parents=True)
                    artifact_dir.mkdir(parents=True)
                    return repo_dir, run_dir, artifact_dir

                @staticmethod
                def assert_cancel_callback(callback: object) -> None:
                    if not callable(callback):
                        raise AssertionError("cancel callback was not propagated")

                def run_mechanical_phase(
                    self,
                    _repo_dir: Path,
                    _run_dir: Path,
                    _job: dict,
                    phase: str,
                    *,
                    deadline_monotonic: float,
                    cancel_requested: object,
                    **_kwargs: object,
                ) -> None:
                    self.assert_cancel_callback(cancel_requested)
                    observed.append((phase, deadline_monotonic))

                def emit_event(self, *_args: object, **_kwargs: object) -> None:
                    return None

                def heartbeat(self) -> None:
                    return None

                def build_envelope(self, *_args: object, **_kwargs: object) -> dict:
                    return {
                        "protocol_version": "review-worker-protocol/v1",
                        "execution": {"status": "failed", "duration_ms": 0},
                    }

                def submit_result_or_record_failure(self, *_args: object, **_kwargs: object) -> bool:
                    return True

            with patch.object(
                worker_module,
                "PIPELINE_PHASES",
                (("prepare_workspace", 0), ("inventory_repository", 10)),
            ), patch.object(worker_module, "validate_phase_outputs", return_value=None), patch.object(
                worker_module,
                "phase_completion_data",
                return_value={},
            ):
                Worker(worker_config(root), client=object()).run_job(
                    review_job(wall_time_seconds=30)
                )

            self.assertEqual([name for name, _deadline in observed], ["checkout", "inventory_repository"])
            self.assertEqual(observed[0][1], observed[1][1])

    def test_semantic_turn_timeout_is_clamped_to_remaining_global_deadline(self) -> None:
        calls: list[dict] = []

        class CodexClient:
            def run_turn(self, **kwargs: object) -> None:
                calls.append(dict(kwargs))

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            repo_dir = root / "repo"
            run_dir = repo_dir / ".codex-review" / "runs" / "run_1"
            run_dir.mkdir(parents=True)
            write_json(run_dir / "run-state.json", {"thread_id": "thread_1"})
            worker = ReviewWorkerV1(worker_config(root), client=object())

            with patch("pullwise_worker.review_worker_v1.time.monotonic", return_value=100.0):
                worker.run_semantic_phase(
                    CodexClient(),
                    repo_dir,
                    run_dir,
                    review_job(),
                    "repo_map",
                    deadline_monotonic=112.9,
                )

        self.assertEqual(calls[0]["timeout_seconds"], 12)

    def test_intent_process_polls_cancellation_while_test_is_running(self) -> None:
        class HangingProcess:
            def __init__(self) -> None:
                self.args = ["python3", "-m", "unittest", "test_delegated.py"]
                self.returncode: int | None = None
                self.terminated = False

            def communicate(self, timeout: float | None = None) -> tuple[str, str]:
                if self.returncode is not None:
                    return "", ""
                raise subprocess.TimeoutExpired(self.args, timeout or 0)

            def poll(self) -> int | None:
                return self.returncode

            def terminate(self) -> None:
                self.terminated = True
                self.returncode = -15

            def kill(self) -> None:
                self.returncode = -9

            def wait(self, timeout: float | None = None) -> int:
                del timeout
                return int(self.returncode or 0)

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            run_dir = prepare_intent_run(root)
            process = HangingProcess()
            checks = 0
            process_started = False

            def cancel_requested() -> bool:
                nonlocal checks
                if not process_started:
                    return False
                checks += 1
                return checks >= 2

            def start_process(*_args: object, **_kwargs: object) -> HangingProcess:
                nonlocal process_started
                process_started = True
                return process

            with patch("pullwise_worker.review_worker_v1.sys.platform", "win32"), patch(
                "pullwise_worker.review_worker_v1.shutil.which",
                return_value="/usr/bin/python3",
            ), patch(
                "pullwise_worker.review_worker_v1.subprocess.Popen",
                side_effect=start_process,
            ):
                with self.assertRaises(JobCancelled):
                    run_intent_tests(
                        run_dir,
                        deadline_monotonic=time.monotonic() + 60,
                        cancel_requested=cancel_requested,
                    )

            self.assertTrue(process.terminated)
            self.assertGreaterEqual(checks, 2)

    def test_reviewer_retry_uses_attempt_unique_staging_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            repo_dir = root / "repo"
            run_dir = repo_dir / ".codex-review" / "runs" / "run_1"
            (run_dir / "bundles").mkdir(parents=True)
            prompts_dir = repo_dir / ".codex-review" / "prompts" / "reviewers"
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
            (run_dir / "bundles" / "p0-bundle-001.md").write_text(
                "# bundle\n",
                encoding="utf-8",
            )
            (prompts_dir / "security.md").write_text("Security reviewer\n", encoding="utf-8")
            attempt_two_started = threading.Event()
            late_write_finished = threading.Event()
            output_paths: list[Path] = []
            start_calls: list[dict] = []

            class CodexClient:
                def __init__(self) -> None:
                    self.thread_count = 0

                def start_thread(self, *_args: object, **kwargs: object) -> str:
                    start_calls.append(dict(kwargs))
                    self.thread_count += 1
                    return f"thread-{self.thread_count}"

                def run_turn(self, **kwargs: object) -> None:
                    prompt_text = str(kwargs["prompt"])
                    output_path = Path(
                        next(
                            line.removeprefix("Exact output path: ")
                            for line in prompt_text.splitlines()
                            if line.startswith("Exact output path: ")
                        )
                    )
                    output_paths.append(output_path)
                    if len(output_paths) == 1:
                        def late_writer() -> None:
                            attempt_two_started.wait(2)
                            output_path.parent.mkdir(parents=True, exist_ok=True)
                            write_json(
                                output_path,
                                {
                                    "schema_version": "codex-reviewer-output/v1",
                                    "bundle_id": "p0-bundle-001",
                                    "reviewer": "security",
                                    "reviewed_paths": ["src/app.py"],
                                    "findings": [],
                                    "uncertainties": [],
                                },
                            )
                            late_write_finished.set()

                        threading.Thread(target=late_writer, daemon=True).start()
                        raise RuntimeError("429 server busy")
                    attempt_two_started.set()
                    if not late_write_finished.wait(2):
                        raise AssertionError("late attempt-1 writer did not run")

            class Worker(ReviewWorkerV1):
                def progress_phase(self, *_args: object, **_kwargs: object) -> None:
                    return None

            worker = Worker(worker_config(root), client=object())
            active = ActiveJob("job_1", "run_1", "lease_1", "wk_1-1", thread_id="root-thread")
            worker.run_reviewer_fanout_phase(
                CodexClient(),
                repo_dir,
                run_dir,
                review_job(),
                active=active,
                progress=70,
                deadline_monotonic=time.monotonic() + 60,
            )
            execution = json.loads(
                (run_dir / "reviewer-execution.json").read_text(encoding="utf-8")
            )

            self.assertEqual(len(output_paths), 2)
            self.assertEqual(len(start_calls), 2)
            self.assertTrue(all(callable(call["cancel_requested"]) for call in start_calls))
            self.assertTrue(all(1 <= int(call["timeout_seconds"]) <= 60 for call in start_calls))
            self.assertNotEqual(output_paths[0], output_paths[1])
            self.assertFalse(
                (run_dir / "raw-reviewers" / "p0-bundle-001.security.json").exists()
            )
            self.assertEqual(execution["assignments_completed"], 0)
            self.assertEqual(execution["assignments"][0]["status"], "invalid_output")


if __name__ == "__main__":
    unittest.main()
