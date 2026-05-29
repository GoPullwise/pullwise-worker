from __future__ import annotations

import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import Mock, patch

from pullwise_worker.main import Worker, WorkerConfig, clone_repository, parse_findings, result_checksum, run_codex_review, summarize


def config() -> WorkerConfig:
    namespace = Namespace(
        server_url="http://server.test",
        worker_token="worker-token",
        worker_id="wk_1",
        max_concurrent_jobs=2,
        poll_seconds=1,
        work_dir=tempfile.mkdtemp(),
        codex_command="codex",
        codex_timeout_seconds=60,
    )
    return WorkerConfig(namespace)


class WorkerMainTest(unittest.TestCase):
    def test_parse_findings_accepts_object_payload(self) -> None:
        findings = parse_findings('{"findings":[{"title":"Bug","severity":"high"}]}')

        self.assertEqual(findings, [{"title": "Bug", "severity": "high"}])
        self.assertEqual(summarize(findings)["high"], 1)

    def test_run_job_uploads_progress_result_and_cleans_checkout(self) -> None:
        worker = Worker(config())
        worker.client = Mock()
        checkout_dir = Path(worker.config.work_dir) / "job_1"

        with (
            patch("pullwise_worker.main.clone_repository") as clone_repository,
            patch("pullwise_worker.main.run_codex_review") as run_codex_review,
            patch("pullwise_worker.main.shutil.rmtree") as rmtree,
        ):
            run_codex_review.return_value = (
                [{"title": "Bug", "severity": "high"}],
                {"critical": 0, "high": 1, "medium": 0, "low": 0, "info": 0},
                "review ok",
            )

            worker.run_job({"job_id": "job_1", "attempt": 2, "repo": "acme/api"})

        clone_repository.assert_called_once()
        self.assertEqual(clone_repository.call_args.args[1], checkout_dir)
        run_codex_review.assert_called_once()
        worker.client.result.assert_called_once()
        result_payload = worker.client.result.call_args.args[1]
        self.assertEqual(result_payload["status"], "done")
        self.assertEqual(result_payload["attempt_id"], "wk_1-2")
        self.assertEqual(result_payload["summary"]["high"], 1)
        self.assertEqual(result_payload["result_checksum"], result_checksum({k: v for k, v in result_payload.items() if k != "result_checksum"}))
        self.assertGreaterEqual(worker.client.progress.call_count, 3)
        rmtree.assert_called_with(checkout_dir, ignore_errors=True)

    def test_clone_repository_uses_short_lived_token(self) -> None:
        with patch("pullwise_worker.main.subprocess.run") as run:
            clone_repository(
                {
                    "repo": "acme/api",
                    "branch": "main",
                    "commit": "pending",
                    "clone_url": "https://github.com/acme/api.git",
                    "clone_token": {"token": "short-token"},
                },
                Path("checkout"),
            )

        clone_command = run.call_args_list[0].args[0]
        self.assertEqual(clone_command[:4], ["git", "clone", "--depth", "1"])
        self.assertIn("x-access-token:short-token@github.com", clone_command[-2])

    def test_run_codex_review_invokes_codex_exec_and_parses_findings(self) -> None:
        completed = Mock(returncode=0, stdout='{"findings":[{"title":"Bug","severity":"medium"}]}', stderr="")

        with patch("pullwise_worker.main.subprocess.run", return_value=completed) as run:
            findings, summary, _logs = run_codex_review(config(), {"repo": "acme/api"}, Path("checkout"))

        command = run.call_args.args[0]
        self.assertEqual(command[:3], ["codex", "exec", "--json"])
        self.assertEqual(findings[0]["title"], "Bug")
        self.assertEqual(summary["medium"], 1)


if __name__ == "__main__":
    unittest.main()
