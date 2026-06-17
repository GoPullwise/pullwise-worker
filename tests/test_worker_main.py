from __future__ import annotations

import json
import tempfile
import unittest
import importlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

worker_main = importlib.import_module("pullwise_worker.main")


def config_for(tmp: Path) -> SimpleNamespace:
    return SimpleNamespace(
        service_home=str(tmp / "home"),
        worker_token="secret-token",
        codex_command="codex",
        codex_model="gpt-5",
        codex_reasoning_effort="high",
        codex_doctor_timeout_seconds=60,
    )


class GraphVerifiedWorkerTest(unittest.TestCase):
    def test_graph_verified_review_is_the_only_review_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            cfg = config_for(Path(tmp_dir))

            self.assertTrue(worker_main.graph_verified_review_enabled(cfg, {"agentConfig": {}}))
            self.assertFalse(hasattr(worker_main, "run_codex_review"))
            self.assertFalse(hasattr(worker_main, "build_repository_graph_bundle"))
            self.assertFalse(hasattr(worker_main, "apply_review_calibration_decisions"))
            self.assertFalse(hasattr(worker_main, "apply_convergence_gate"))
            self.assertFalse(hasattr(worker_main, "convergence_context_for_job"))
            self.assertFalse(hasattr(worker_main, "reportability_rejection_reason"))

    def test_write_graph_verified_codereview_config_uses_plan_schema(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            cfg = config_for(root)

            worker_main.write_graph_verified_codereview_config(
                cfg,
                root,
                {
                    "codegraphCommand": "codegraph",
                    "syncBeforeRun": True,
                    "forceIndexOnFailure": True,
                    "finderMaxParallel": 7,
                    "finderTimeoutSeconds": 240,
                    "reproMaxParallel": 3,
                    "reproTimeoutSeconds": 600,
                    "maxRepro": 20,
                    "requireRedGreen": True,
                    "minScoreForRepro": 9,
                },
                "deep",
            )

            payload = json.loads((root / ".codereview" / "config.json").read_text(encoding="utf-8"))

        self.assertEqual(payload["mode"], "deep")
        self.assertEqual(payload["codegraph"]["command"], "codegraph")
        self.assertTrue(payload["codegraph"]["optional_sync"])
        self.assertTrue(payload["codegraph"]["reindex"])
        self.assertEqual(payload["finders"]["max_workers"], 7)
        self.assertEqual(payload["finders"]["timeout_seconds"], 240)
        self.assertEqual(payload["repro"]["max_workers"], 3)
        self.assertEqual(payload["repro"]["timeout_seconds"], 600)
        self.assertEqual(payload["repro"]["max_repro"], 20)
        self.assertTrue(payload["repro"]["require_red_green"])
        self.assertEqual(payload["scoring"]["min_score_for_repro"], 9)
        self.assertEqual(payload["scoring"]["always_repro_severities"], ["critical", "high"])

    def test_upsert_graph_verified_codex_mcp_config_replaces_existing_block(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            codex_home = Path(tmp_dir) / ".codex"
            config_path = codex_home / "config.toml"
            codex_home.mkdir(parents=True)
            config_path.write_text(
                "[mcp_servers.codegraph]\ncommand = \"old\"\nargs = [\"old\"]\n\n[agents]\nmax_threads = 6\n",
                encoding="utf-8",
            )

            worker_main.upsert_graph_verified_codex_mcp_config({"CODEX_HOME": str(codex_home)}, "codegraph")
            content = config_path.read_text(encoding="utf-8")

        self.assertIn("[mcp_servers.codegraph]\ncommand = \"codegraph\"\nargs = [\"serve\", \"--mcp\"]", content)
        self.assertEqual(content.count("[mcp_servers.codegraph]"), 1)
        self.assertIn("[agents]\nmax_threads = 6", content)

    def test_run_graph_verified_review_payload_reads_confirmed_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            cfg = config_for(root)
            reports = root / ".codereview" / "runs" / "run_1" / "reports"
            reports.mkdir(parents=True)
            final_md = reports / "final.md"
            final_md.write_text("# Graph-Verified Code Review Report\n", encoding="utf-8")
            (reports / "debug.md").write_text("# Debug Report\n", encoding="utf-8")
            (reports / "confirmed.json").write_text(json.dumps([{"candidate": {"candidate_id": "c1"}}]), encoding="utf-8")
            (reports / "rejected.json").write_text(json.dumps([{"candidate_id": "r1"}]), encoding="utf-8")
            (reports / "final.json").write_text(json.dumps({"confirmed": [{"candidate": {"candidate_id": "c1"}}]}), encoding="utf-8")
            (reports / "summary.json").write_text(json.dumps({"reports": {"blocked": 2}}), encoding="utf-8")
            codereview_main = importlib.import_module("codereview.main")

            with patch.object(worker_main, "ensure_graph_verified_codegraph_codex_mcp"), patch.object(
                codereview_main,
                "run_review",
                return_value=final_md,
            ):
                payload = worker_main.run_graph_verified_review_payload(
                    cfg,
                    {"base_commit": "origin/main", "agentConfig": {"graphVerified": {"mode": "fast"}}},
                    root,
                    "HEAD",
                )

        self.assertEqual(payload["version"], "graph-verified-code-review/1")
        self.assertEqual(payload["runId"], "run_1")
        self.assertEqual(payload["mode"], "fast")
        self.assertEqual(payload["base"], "origin/main")
        self.assertEqual(payload["confirmedCount"], 1)
        self.assertEqual(payload["rejectedCount"], 1)
        self.assertEqual(payload["blockedCount"], 2)
        self.assertEqual(payload["finalJson"]["confirmed"][0]["candidate"]["candidate_id"], "c1")

    def test_run_graph_verified_review_payload_blocks_on_preflight_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            cfg = config_for(root)
            with patch.object(
                worker_main,
                "ensure_graph_verified_codegraph_codex_mcp",
                side_effect=RuntimeError("failed with secret-token"),
            ):
                payload = worker_main.run_graph_verified_review_payload(
                    cfg,
                    {"agentConfig": {"graphVerified": {"mode": "invalid"}}},
                    root,
                    "abc123",
                )

        self.assertEqual(payload["version"], "graph-verified-code-review/1")
        self.assertEqual(payload["mode"], "standard")
        self.assertEqual(payload["base"], "abc123^")
        self.assertEqual(payload["confirmedCount"], 0)
        self.assertEqual(payload["blockedCount"], 1)
        self.assertEqual(payload["finalJson"], {"confirmed": []})
        self.assertNotIn("secret-token", payload["debugMarkdown"])
        self.assertIn("[redacted]", payload["debugMarkdown"])


if __name__ == "__main__":
    unittest.main()
