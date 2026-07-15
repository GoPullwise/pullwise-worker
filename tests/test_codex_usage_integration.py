from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from pullwise_worker.codex_sdk_runtime import CodexTokenUsage
from pullwise_worker.review_worker_v1 import CodexSdkClient, ReviewWorkerV1, write_json


class CodexUsageIntegrationTests(unittest.TestCase):
    def test_run_turn_attributes_usage_to_phase_and_returns_turn_usage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            notifications = iter(
                (
                    SimpleNamespace(
                        method="thread/tokenUsage/updated",
                        payload={
                            "threadId": "thread-1",
                            "turnId": "turn-1",
                            "tokenUsage": {
                                "total": {
                                    "inputTokens": 90,
                                    "cachedInputTokens": 20,
                                    "outputTokens": 10,
                                    "reasoningOutputTokens": 3,
                                    "totalTokens": 100,
                                },
                                "last": {
                                    "inputTokens": 90,
                                    "cachedInputTokens": 20,
                                    "outputTokens": 10,
                                    "reasoningOutputTokens": 3,
                                    "totalTokens": 100,
                                },
                            },
                        },
                    ),
                    SimpleNamespace(
                        method="turn/completed",
                        payload={
                            "turn": {
                                "id": "turn-1",
                                "status": "completed",
                                "durationMs": 123,
                            }
                        },
                    ),
                )
            )

            class Client:
                def turn_start(self, *_args: object, **_kwargs: object) -> SimpleNamespace:
                    return SimpleNamespace(turn=SimpleNamespace(id="turn-1"))

                def next_turn_notification(self, _turn_id: str) -> SimpleNamespace:
                    return next(notifications)

                def unregister_turn_notifications(self, _turn_id: str) -> None:
                    return None

            server = CodexSdkClient("", {}, root, root / "events.jsonl")
            server._client = Client()

            metrics = server.run_turn(
                thread_id="thread-1",
                repo_dir=root,
                prompt="review",
                effort="medium",
                read_only=True,
                timeout_seconds=2,
                metrics_phase="repo_map",
            )
            snapshot = server.usage_snapshot()

        self.assertEqual(metrics.duration_ms, 123)
        self.assertEqual(
            metrics.token_usage,
            CodexTokenUsage(
                input_tokens=90,
                cached_input_tokens=20,
                output_tokens=10,
                reasoning_output_tokens=3,
                total_tokens=100,
            ),
        )
        self.assertEqual(snapshot["by_phase"]["repo_map"]["tokens"]["total_tokens"], 100)

    def test_completed_envelope_contains_only_current_run_usage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            run_dir = root / "repo" / ".codex-review" / "runs" / "run-1"
            artifact_dir = root / "artifacts" / "run-1"
            run_dir.mkdir(parents=True)
            artifact_dir.mkdir(parents=True)
            write_json(
                artifact_dir / "artifact-manifest.json",
                {"schema_version": "artifact-manifest/v1", "items": []},
            )

            class UsageClient:
                events_path = run_dir / "codex-events.jsonl"

                @staticmethod
                def usage_snapshot() -> dict[str, object]:
                    return {
                        "schema_version": "codex-usage/v1",
                        "observed": True,
                        "turns_started": 10,
                        "turns_with_usage": 10,
                        "threads_observed": 3,
                        "tokens": {"total_tokens": 1_099_277},
                        "by_phase": {},
                    }

            worker = ReviewWorkerV1(
                SimpleNamespace(worker_id="worker-1", service_home=str(root)),
                client=object(),
            )
            worker.codex_client = UsageClient()  # type: ignore[assignment]
            envelope = worker.build_envelope(
                {
                    "job_id": "job-1",
                    "run_id": "run-1",
                    "lease_id": "lease-1",
                    "repo": "acme/repo",
                },
                "run-1",
                "completed",
                1.0,
                artifact_dir,
                run_dir,
            )

            UsageClient.events_path = root / "other-run" / "codex-events.jsonl"
            unrelated_envelope = worker.build_envelope(
                {
                    "job_id": "job-2",
                    "run_id": "run-2",
                    "lease_id": "lease-2",
                    "repo": "acme/repo",
                },
                "run-2",
                "failed",
                1.0,
                root / "artifacts" / "run-2",
                root / "repo" / ".codex-review" / "runs" / "run-2",
                error="failed before Codex startup",
            )

        self.assertEqual(envelope["usage"]["tokens"]["total_tokens"], 1_099_277)
        self.assertNotIn("usage", unrelated_envelope)


if __name__ == "__main__":
    unittest.main()
