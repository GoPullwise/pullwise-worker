from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Iterator
from unittest.mock import patch

from pullwise_worker.current_run_eta import CurrentRunEstimator
from pullwise_worker.review_worker_v1 import (
    ActiveJob,
    JobCancelled,
    PIPELINE_PHASES,
    ReviewWorkerV1,
    SEMANTIC_PHASES,
    current_run_estimator_for_job,
    write_json,
)


def prompt_assignment(prompt: object) -> tuple[str, str, Path]:
    lines = str(prompt).splitlines()
    bundle_id = next(
        line.removeprefix("Bundle assignment: ")
        for line in lines
        if line.startswith("Bundle assignment: ")
    )
    reviewer_id = next(
        line.removeprefix("Reviewer assignment: ")
        for line in lines
        if line.startswith("Reviewer assignment: ")
    )
    output_path = Path(
        next(
            line.removeprefix("Exact output path: ")
            for line in lines
            if line.startswith("Exact output path: ")
        )
    )
    return bundle_id, reviewer_id, output_path


def write_reviewer_output(prompt: object) -> None:
    bundle_id, reviewer_id, output_path = prompt_assignment(prompt)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    write_json(
        output_path,
        {
            "schema_version": "codex-reviewer-output/v1",
            "bundle_id": bundle_id,
            "reviewer": reviewer_id,
            "reviewed_paths": ["src/app.py"],
            "findings": [],
            "uncertainties": [],
        },
    )


@contextmanager
def fanout_fixture(
    reviewers: list[str],
) -> Iterator[tuple[ReviewWorkerV1, Path, Path, dict, ActiveJob]]:
    with tempfile.TemporaryDirectory() as tmp_dir:
        root = Path(tmp_dir)
        repo = root / "repo"
        run_dir = repo / ".codex-review" / "runs" / "run_1"
        bundles_dir = run_dir / "bundles"
        prompts_dir = repo / ".codex-review" / "prompts" / "reviewers"
        bundles_dir.mkdir(parents=True)
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
                        "reviewers": reviewers,
                    }
                ],
            },
        )
        (bundles_dir / "p0-bundle-001.md").write_text(
            "# p0-bundle-001\n",
            encoding="utf-8",
        )
        for reviewer in reviewers:
            (prompts_dir / f"{reviewer}.md").write_text(
                f"{reviewer} reviewer\n",
                encoding="utf-8",
            )
        job = {
            "job_id": "job_1",
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
                    "reviewer_concurrency": 2,
                    "max_bundles": 24,
                    "max_reviewer_assignments": 48,
                },
                "budget": {"max_wall_time_seconds": 14400},
            },
            "repositoryLimits": {
                "maxFiles": 2000,
                "maxBytes": 50 * 1024 * 1024,
            },
        }
        worker = ReviewWorkerV1(
            SimpleNamespace(worker_id="wk_1", service_home=str(root)),
            client=object(),
        )
        active = ActiveJob(
            "job_1",
            "run_1",
            "lease_1",
            "attempt_1",
            thread_id="root-thread",
        )
        yield worker, repo, run_dir, job, active


class ReviewerFanoutConcurrencyTest(unittest.TestCase):
    def test_over_limit_plan_is_rejected_before_starting_any_thread(self) -> None:
        with fanout_fixture(["security", "correctness", "test_gap"]) as (
            worker,
            repo,
            run_dir,
            job,
            active,
        ):
            job["review_request"]["policy"]["max_reviewer_assignments"] = 2
            starts: list[str] = []

            class FakeCodexClient:
                def start_thread(self, *_args: object, **_kwargs: object) -> str:
                    starts.append("started")
                    raise AssertionError("fanout started before enforcing its assignment cap")

            with self.assertRaisesRegex(
                RuntimeError,
                "REVIEW_PLAN_LIMIT_EXCEEDED.*assignments.*2",
            ):
                worker.run_reviewer_fanout_phase(
                    FakeCodexClient(),
                    repo,
                    run_dir,
                    job,
                    active=active,
                    progress=70,
                )

        self.assertEqual(starts, [])

    def test_low_memory_host_reduces_effective_concurrency_to_one(self) -> None:
        with fanout_fixture(["security", "correctness", "test_gap"]) as (
            worker,
            repo,
            run_dir,
            job,
            active,
        ):
            active_turns = 0
            max_active_turns = 0
            state_lock = threading.Lock()

            class FakeCodexClient:
                def start_thread(self, _repo_dir: Path, _model: str) -> str:
                    return f"reviewer-thread-{time.monotonic_ns()}"

                def run_turn(self, **kwargs: object) -> SimpleNamespace:
                    nonlocal active_turns, max_active_turns
                    with state_lock:
                        active_turns += 1
                        max_active_turns = max(max_active_turns, active_turns)
                    try:
                        time.sleep(0.02)
                        write_reviewer_output(kwargs["prompt"])
                    finally:
                        with state_lock:
                            active_turns -= 1
                    return SimpleNamespace(duration_ms=20)

            with patch(
                "pullwise_worker.review_worker_v1.worker_memory_payload",
                return_value={
                    "totalBytes": 4 * 1024**3,
                    "availableBytes": 3 * 1024**3,
                },
            ):
                worker.run_reviewer_fanout_phase(
                    FakeCodexClient(),
                    repo,
                    run_dir,
                    job,
                    active=active,
                    progress=70,
                )

            execution = json.loads(
                (run_dir / "reviewer-execution.json").read_text(encoding="utf-8")
            )

        self.assertEqual(max_active_turns, 1)
        self.assertEqual(execution["max_concurrency"], 2)
        self.assertEqual(execution["effective_concurrency"], 1)
        self.assertEqual(execution["concurrency_limit_reason"], "low_memory_host")

    def test_estimate_includes_downstream_pipeline_critical_path(self) -> None:
        with fanout_fixture(['security', 'correctness', 'test_gap']) as (
            worker,
            repo,
            run_dir,
            job,
            active,
        ):
            monotonic_now = [100.0]
            active.current_run_estimator = current_run_estimator_for_job(
                job,
                monotonic_clock=lambda: monotonic_now[0],
                wall_clock=lambda: 1000.0,
            )
            for phase, phase_progress in PIPELINE_PHASES:
                if phase == 'reviewer_fanout':
                    break
                worker.start_phase(active, run_dir, phase, phase_progress)
                monotonic_now[0] += 5.0 if phase in SEMANTIC_PHASES else 2.0
                worker.complete_phase(active, run_dir, phase, phase_progress)

            class FakeCodexClient:
                def start_thread(self, _repo_dir: Path, _model: str) -> str:
                    return f'reviewer-thread-{time.monotonic_ns()}'

                def run_turn(self, **kwargs: object) -> SimpleNamespace:
                    _bundle, reviewer, _output = prompt_assignment(kwargs['prompt'])
                    time.sleep(0.01 if reviewer == 'security' else 0.05)
                    write_reviewer_output(kwargs['prompt'])
                    return SimpleNamespace(duration_ms=10_000)

            worker.start_phase(active, run_dir, 'reviewer_fanout', 70)
            worker.run_reviewer_fanout_phase(
                FakeCodexClient(),
                repo,
                run_dir,
                job,
                active=active,
                progress=70,
            )

            progress_events = [
                json.loads(line)
                for line in (run_dir / 'progress.log.jsonl').read_text(encoding='utf-8').splitlines()
                if line.strip()
            ]

        available_remaining = [
            event['progress']['estimate']['remainingSeconds']
            for event in progress_events
            if event.get('progress', {}).get('estimate', {}).get('state') == 'available'
            and event['progress']['estimate'].get('remainingSeconds', 0) > 0
        ]
        self.assertTrue(
            available_remaining,
            [event.get('progress', {}).get('estimate') for event in progress_events],
        )
        self.assertGreater(available_remaining[0], 10)

    def test_progress_estimate_uses_completed_turn_duration_and_live_scheduler_state(self) -> None:
        with fanout_fixture(['security', 'correctness', 'test_gap']) as (
            worker,
            repo,
            run_dir,
            job,
            active,
        ):
            active.current_run_estimator = CurrentRunEstimator(wall_clock=lambda: 1000.0)

            class FakeCodexClient:
                def start_thread(self, _repo_dir: Path, _model: str) -> str:
                    return f'reviewer-thread-{time.monotonic_ns()}'

                def run_turn(self, **kwargs: object) -> SimpleNamespace:
                    _bundle, reviewer, _output = prompt_assignment(kwargs['prompt'])
                    time.sleep(0.01 if reviewer == 'security' else 0.05)
                    write_reviewer_output(kwargs['prompt'])
                    return SimpleNamespace(duration_ms=10_000)

            worker.run_reviewer_fanout_phase(
                FakeCodexClient(),
                repo,
                run_dir,
                job,
                active=active,
                progress=70,
            )

            progress_events = [
                json.loads(line)
                for line in (run_dir / 'progress.log.jsonl').read_text(encoding='utf-8').splitlines()
                if line.strip()
            ]
            execution = json.loads(
                (run_dir / 'reviewer-execution.json').read_text(encoding='utf-8')
            )

        available = [
            event['progress']['estimate']
            for event in progress_events
            if event.get('progress', {}).get('estimate', {}).get('state') == 'available'
            and event['progress']['estimate'].get('remainingSeconds', 0) > 0
        ]
        self.assertTrue(available)
        self.assertEqual(available[0]['basis'], 'current_run_work_graph')
        self.assertEqual(available[0]['parallel']['configuredConcurrency'], 2)
        self.assertEqual(available[0]['parallel']['effectiveConcurrency'], 2)
        self.assertNotIn('thread_id', available[0])
        self.assertEqual(execution['assignments'][0]['attempts'][0]['duration_ms'], 10_000)

    def test_fatal_assignment_cancels_active_sibling_and_never_starts_pending_work(self) -> None:
        with fanout_fixture(["security", "correctness", "test_gap"]) as (
            worker,
            repo,
            run_dir,
            job,
            active,
        ):
            started_reviewers: list[str] = []
            sibling_cancelled = threading.Event()

            class FakeCodexClient:
                def start_thread(self, _repo_dir: Path, _model: str) -> str:
                    return f"reviewer-thread-{len(started_reviewers) + 1}"

                def run_turn(self, **kwargs: object) -> None:
                    _bundle, reviewer, _output = prompt_assignment(kwargs["prompt"])
                    started_reviewers.append(reviewer)
                    if reviewer == "security":
                        time.sleep(0.05)
                        raise RuntimeError("reviewer exploded")
                    cancel_requested = kwargs["cancel_requested"]
                    deadline = time.monotonic() + 2
                    while time.monotonic() < deadline and not cancel_requested():
                        time.sleep(0.01)
                    if cancel_requested():
                        sibling_cancelled.set()
                        raise JobCancelled("cancel requested")
                    raise AssertionError("active sibling was not cancelled")

            with self.assertRaisesRegex(RuntimeError, "reviewer exploded"):
                worker.run_reviewer_fanout_phase(
                    FakeCodexClient(),
                    repo,
                    run_dir,
                    job,
                    active=active,
                    progress=70,
                )

            execution = json.loads(
                (run_dir / "reviewer-execution.json").read_text(encoding="utf-8")
            )
            self.assertTrue(sibling_cancelled.is_set())
            self.assertCountEqual(started_reviewers, ["security", "correctness"])
            self.assertEqual(
                [record["status"] for record in execution["assignments"]],
                ["failed", "cancelled", "cancelled"],
            )
            self.assertEqual(execution["assignments"][2]["attempts"], [])

    def test_thread_archive_failure_closes_runtime_and_stops_pending_work(self) -> None:
        with fanout_fixture(["security", "correctness", "test_gap"]) as (
            worker,
            repo,
            run_dir,
            job,
            active,
        ):
            started_reviewers: list[str] = []
            archived_threads: list[str] = []
            runtime_closed = threading.Event()

            class FakeCodexClient:
                def start_thread(self, _repo_dir: Path, _model: str) -> str:
                    return f"reviewer-thread-{len(started_reviewers) + 1}"

                def run_turn(self, **kwargs: object) -> SimpleNamespace:
                    _bundle, reviewer, _output = prompt_assignment(kwargs["prompt"])
                    started_reviewers.append(reviewer)
                    if reviewer == "security":
                        write_reviewer_output(kwargs["prompt"])
                        return SimpleNamespace(duration_ms=10)
                    cancel_requested = kwargs["cancel_requested"]
                    deadline = time.monotonic() + 2
                    while time.monotonic() < deadline and not cancel_requested():
                        time.sleep(0.01)
                    raise JobCancelled("cancel requested")

                def release_thread(self, thread_id: str) -> None:
                    archived_threads.append(thread_id)
                    if thread_id == "reviewer-thread-1":
                        raise RuntimeError("archive rpc failed")

                def close(self) -> None:
                    runtime_closed.set()

            with self.assertRaisesRegex(
                RuntimeError,
                "Codex reviewer thread archive failed.*archive rpc failed",
            ):
                worker.run_reviewer_fanout_phase(
                    FakeCodexClient(),
                    repo,
                    run_dir,
                    job,
                    active=active,
                    progress=70,
                )

            execution = json.loads(
                (run_dir / "reviewer-execution.json").read_text(encoding="utf-8")
            )
            self.assertTrue(runtime_closed.is_set())
            self.assertCountEqual(started_reviewers, ["security", "correctness"])
            self.assertIn("reviewer-thread-1", archived_threads)
            self.assertEqual(execution["assignments"][2]["attempts"], [])
            self.assertEqual(execution["assignments"][2]["status"], "cancelled")

    def test_transient_capacity_error_retries_once_and_reduces_concurrency_to_one(self) -> None:
        with fanout_fixture(["security", "correctness", "test_gap"]) as (
            worker,
            repo,
            run_dir,
            job,
            active,
        ):
            attempt_counts: dict[str, int] = {}
            started_threads: list[str] = []
            active_turns = 0
            max_active_after_limit = 0
            capacity_error_seen = threading.Event()
            state_lock = threading.Lock()
            active.current_run_estimator = CurrentRunEstimator(wall_clock=lambda: 1000.0)

            class FakeCodexClient:
                def start_thread(self, _repo_dir: Path, _model: str) -> str:
                    thread_id = f"reviewer-thread-{len(started_threads) + 1}"
                    started_threads.append(thread_id)
                    return thread_id

                def run_turn(self, **kwargs: object) -> None:
                    nonlocal active_turns, max_active_after_limit
                    _bundle, reviewer, _output = prompt_assignment(kwargs["prompt"])
                    with state_lock:
                        attempt_counts[reviewer] = attempt_counts.get(reviewer, 0) + 1
                        attempt = attempt_counts[reviewer]
                        active_turns += 1
                        if capacity_error_seen.is_set():
                            max_active_after_limit = max(
                                max_active_after_limit,
                                active_turns,
                            )
                    try:
                        if reviewer == "security" and attempt == 1:
                            raise RuntimeError("429 rate limit")
                        time.sleep(0.03)
                        write_reviewer_output(kwargs["prompt"])
                    finally:
                        with state_lock:
                            active_turns -= 1
                        if reviewer == "security" and attempt == 1:
                            capacity_error_seen.set()

            worker.run_reviewer_fanout_phase(
                FakeCodexClient(),
                repo,
                run_dir,
                job,
                active=active,
                progress=70,
            )

            execution = json.loads(
                (run_dir / "reviewer-execution.json").read_text(encoding="utf-8")
            )
            log_events = [
                json.loads(line)
                for line in (run_dir / "worker.log.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
                if line.strip()
            ]
            progress_events = [
                json.loads(line)
                for line in (run_dir / "progress.log.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
                if line.strip()
            ]
            self.assertEqual(attempt_counts["security"], 2)
            self.assertEqual(len(started_threads), 4)
            self.assertEqual(max_active_after_limit, 1)
            self.assertEqual(execution["max_concurrency"], 2)
            self.assertEqual(execution["effective_concurrency"], 1)
            self.assertEqual(execution["assignments_completed"], 3)
            self.assertEqual(len(execution["assignments"][0]["attempts"]), 2)
            self.assertIn(
                "reviewer_concurrency_reduced",
                {event.get("event") for event in log_events},
            )
            self.assertTrue(
                any(
                    event.get("progress", {})
                    .get("estimate", {})
                    .get("parallel", {})
                    .get("effectiveConcurrency")
                    == 1
                    for event in progress_events
                )
            )


if __name__ == "__main__":
    unittest.main()
