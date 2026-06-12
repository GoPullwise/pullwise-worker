from __future__ import annotations

import concurrent.futures
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import Mock, patch

import pullwise_worker.main as worker_main
from pullwise_worker import __version__
from pullwise_worker.main import (
    CODEX_LOGIN_COMMAND,
    OPENCODE_AUTH_COMMAND,
    PullwiseClient,
    PullwiseHTTPError,
    PullwiseRequestError,
    PullwiseResponse,
    Worker,
    WorkerConfig,
    audit_swarm_findings_from_payload,
    audit_swarm_output_schema,
    audit_swarm_payload_from_findings,
    audit_swarm_scan_artifacts,
    build_repository_graph_bundle,
    checkout_dir_for_job,
    cleanup_checkouts,
    cleanup_worker_resources,
    clone_repository,
    collect_preflight_metadata,
    completion_audit_payload,
    codex_ready_check,
    default_worker_package,
    execute_lifecycle_command,
    filter_audit_swarm_payload_by_findings,
    filter_reportable_findings,
    normalize_audit_swarm_files_for_checkout,
    node_version_check,
    package_install_command,
    parse_audit_swarm_payload,
    redact_secrets,
    result_checksum,
    run_codex_provider_review,
    run_codex_review,
    run_deterministic_repository_checks,
    run_doctor,
    run_git_command,
    run_verifier_commands,
    safe_job_id,
    safe_rmtree,
    service_action,
    summarize,
    verification_audit_payload,
    verifier_command_env,
    uninstall_worker,
    update_worker,
    worker_config_for_job,
    worker_readiness_checks,
    write_scan_summary,
)


def audit_payload(issue_cards: list[dict] | None = None, verification_results: list[dict] | None = None) -> dict:
    return {
        "audit_protocol": "audit-swarm/0.1",
        "issue_cards": issue_cards or [],
        "verification_results": verification_results or [],
    }


def worker_job(**overrides: object) -> dict:
    payload = {
        "job_id": "job_1",
        "attempt": 1,
        "repo": "acme/api",
        "commit": "pending",
        "agentConfig": {
            "providerChain": ["codex"],
            "codex": {
                "command": "codex",
                "model": "gpt-5.5",
                "reasoningEffort": "medium",
            },
        },
        "repositoryLimits": {"maxFiles": 2000, "maxBytes": 50 * 1024 * 1024},
    }
    payload.update(overrides)
    return payload


def issue_card(
    title: str,
    *,
    issue_id: str = "issue-1",
    severity: str = "P2",
    file: str = "src/app.py",
    line: int = 12,
    evidence: list | None = None,
) -> dict:
    return {
        "issue_id": issue_id,
        "shard_id": "app",
        "agent_role": "correctness-reviewer",
        "title": title,
        "category": "correctness",
        "severity": severity,
        "confidence": 0.8,
        "locations": [{"file": file, "startLine": line, "endLine": line}] if file else [],
        "claim": f"{title} claim.",
        "evidence": evidence if evidence is not None else ["Concrete evidence."],
        "reproduction_idea": f"Reproduce {title.lower()} in a focused checkout.",
        "suggested_test": f"Add a regression test for {title.lower()}.",
        "false_positive_checks": ["Check for upstream guard."],
        "violated_invariants": ["The behavior should remain deterministic."],
        "limitations": [],
    }


def config() -> WorkerConfig:
    namespace = Namespace(
        server_url="https://server.test",
        worker_token="worker-token",
        worker_id="wk_1",
        max_concurrent_jobs=2,
        poll_seconds=1,
        work_dir=tempfile.mkdtemp(),
        checkout_root=None,
        log_dir=tempfile.mkdtemp(),
        provider="codex",
        codex_command="codex",
        codex_timeout_seconds=60,
    )
    return WorkerConfig(namespace)


def mark_checkout_root_owned(cfg: WorkerConfig) -> None:
    checkout_root = Path(cfg.work_dir)
    checkout_root.mkdir(parents=True, exist_ok=True)
    (checkout_root / ".pullwise-checkout-root").write_text(
        "pullwise-worker checkout root\n",
        encoding="utf-8",
    )


class WorkerMainTest(unittest.TestCase):
    def setUp(self) -> None:
        with worker_main._CODEX_AUTH_FAILURE_LOCK:
            worker_main._codex_auth_failure_until = 0.0
            worker_main._codex_auth_failure_detail = ""

    def test_worker_config_defaults_repository_limits(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            worker_config = config()

        self.assertEqual(worker_config.max_repo_files, 2000)
        self.assertEqual(worker_config.max_repo_bytes, 50 * 1024 * 1024)
        self.assertEqual(worker_config.max_claim_jobs, 2)

    def test_worker_config_for_job_applies_admin_agent_policy_but_keeps_local_commands(self) -> None:
        worker_config = config()
        worker_config.provider = "codex"
        worker_config.provider_chain = ["codex"]
        worker_config.codex_command = "/opt/server-pullwise/ops/codex-node22"
        worker_config.codex_model = "gpt-env"
        worker_config.codex_reasoning_effort = "medium"
        worker_config.opencode_command = "/opt/server-pullwise/ops/opencode"
        worker_config.opencode_model = "opencode/env"
        worker_config.opencode_variant = "medium"

        job_config = worker_config_for_job(
            worker_config,
            {
                "agentConfig": {
                    "providerChain": ["codex", "opencode"],
                    "codex": {
                        "command": "codex-nightly",
                        "model": "gpt-5.5-codex",
                        "reasoningEffort": "xhigh",
                    },
                    "opencode": {
                        "command": "opencode-cli",
                        "model": "openai/gpt-5",
                        "variant": "high",
                    },
                },
                "repositoryLimits": {"maxFiles": 321, "maxBytes": 654321},
            },
        )

        self.assertIsNot(job_config, worker_config)
        self.assertEqual(job_config.provider, "codex")
        self.assertEqual(job_config.provider_chain, ["codex", "opencode"])
        self.assertEqual(job_config.codex_command, "/opt/server-pullwise/ops/codex-node22")
        self.assertEqual(job_config.codex_model, "gpt-5.5-codex")
        self.assertEqual(job_config.codex_reasoning_effort, "xhigh")
        self.assertEqual(job_config.opencode_command, "/opt/server-pullwise/ops/opencode")
        self.assertEqual(job_config.opencode_model, "openai/gpt-5")
        self.assertEqual(job_config.opencode_variant, "high")
        self.assertEqual(job_config.max_repo_files, 321)
        self.assertEqual(job_config.max_repo_bytes, 654321)
        self.assertEqual(worker_config.provider_chain, ["codex"])
        self.assertEqual(worker_config.codex_command, "/opt/server-pullwise/ops/codex-node22")
        self.assertEqual(worker_config.codex_model, "gpt-env")
        self.assertEqual(worker_config.codex_reasoning_effort, "medium")
        self.assertEqual(worker_config.opencode_command, "/opt/server-pullwise/ops/opencode")
        self.assertEqual(worker_config.opencode_model, "opencode/env")
        self.assertEqual(worker_config.opencode_variant, "medium")

    def test_worker_config_for_job_requires_canonical_server_payload(self) -> None:
        worker_config = config()

        with self.assertRaisesRegex(RuntimeError, "agentConfig"):
            worker_config_for_job(worker_config, {"agent_config": {"providerChain": ["codex"]}})
        with self.assertRaisesRegex(RuntimeError, "repositoryLimits"):
            worker_config_for_job(worker_config, {"agentConfig": {"providerChain": ["codex"]}})

    def test_worker_config_for_job_rejects_unknown_agent_policy(self) -> None:
        worker_config = config()

        with self.assertRaisesRegex(RuntimeError, "providerChain"):
            worker_config_for_job(
                worker_config,
                {
                    "agentConfig": {
                        "providerChain": ["codex --unsafe"],
                        "codex": {
                            "command": "codex --unsafe",
                            "model": "gpt-5.5\nbad",
                            "reasoningEffort": 'xhigh" --unsafe',
                        },
                        "opencode": {
                            "command": "opencode --flag",
                            "model": "openai/gpt-5\rbad",
                            "variant": "turbo",
                        },
                    },
                    "repositoryLimits": {"maxFiles": 1, "maxBytes": 1},
                },
            )

    def test_repository_resource_stats_excludes_git_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            checkout_dir = Path(tmp)
            (checkout_dir / "src").mkdir()
            (checkout_dir / "src" / "app.py").write_bytes(b"print(1)\n")
            (checkout_dir / "README.md").write_bytes(b"hello\n")
            (checkout_dir / ".git" / "objects").mkdir(parents=True)
            (checkout_dir / ".git" / "objects" / "ignored").write_bytes(b"x" * 1000)

            stats = worker_main.repository_resource_stats(checkout_dir)

        self.assertEqual(stats["fileCount"], 2)
        self.assertEqual(stats["totalBytes"], len(b"print(1)\n") + len(b"hello\n"))

    def test_repository_resource_stats_stops_after_limit_exceeded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            checkout_dir = Path(tmp)
            for index in range(3):
                (checkout_dir / f"{index}.txt").write_bytes(b"x")

            stats = worker_main.repository_resource_stats(
                checkout_dir,
                limits={"maxFiles": 1, "maxBytes": 1024},
            )

        self.assertEqual(stats["fileCount"], 2)
        self.assertEqual(stats["totalBytes"], 2)
        self.assertTrue(stats["scanStoppedEarly"])

    def test_build_repository_graph_detects_project_structure(self) -> None:
        cfg = config()
        with tempfile.TemporaryDirectory() as tmp:
            checkout_dir = Path(tmp)
            (checkout_dir / "src" / "screens").mkdir(parents=True)
            (checkout_dir / "src" / "lib").mkdir(parents=True)
            (checkout_dir / "tests").mkdir()
            (checkout_dir / ".github" / "workflows").mkdir(parents=True)
            (checkout_dir / "src" / "App.jsx").write_text(
                'import Flow from "./screens/flow.jsx";\nexport default Flow;\n',
                encoding="utf-8",
            )
            (checkout_dir / "src" / "screens" / "flow.jsx").write_text(
                'import { api } from "../lib/api.js";\nexport function Flow() { return api; }\n',
                encoding="utf-8",
            )
            (checkout_dir / "src" / "lib" / "api.js").write_text("export const api = {};\n", encoding="utf-8")
            (checkout_dir / "tests" / "flow.test.jsx").write_text("test('flow', () => {});\n", encoding="utf-8")
            (checkout_dir / "package.json").write_text(
                '{"scripts":{"dev":"vite","test":"vitest"},"dependencies":{"@vitejs/plugin-react":"latest"}}',
                encoding="utf-8",
            )
            (checkout_dir / ".github" / "workflows" / "ci.yml").write_text("name: ci\n", encoding="utf-8")

            graph, semantic_graph = build_repository_graph_bundle(
                cfg,
                {"repo": "octocat/app", "branch": "main", "commit": "abc123"},
                checkout_dir,
                {"languages": ["JavaScript/TypeScript"], "packageManagers": ["npm"]},
            )

        self.assertEqual(graph["version"], "repository-graph/0.2")
        self.assertEqual(graph["repo"], "octocat/app")
        self.assertIn("JavaScript", graph["stats"]["languages"])
        node_ids = {node["id"] for node in graph["nodes"]}
        self.assertIn("file:src/App.jsx", node_ids)
        self.assertIn("dir:src/screens", node_ids)
        self.assertIn("file:tests/flow.test.jsx", node_ids)
        self.assertIn("file:.github/workflows/ci.yml", node_ids)
        edge_pairs = {(edge["source"], edge["target"], edge["type"]) for edge in graph["edges"]}
        self.assertIn(("file:src/App.jsx", "file:src/screens/flow.jsx", "imports"), edge_pairs)
        self.assertIn("src/App.jsx", graph["architectureSummary"]["entrypoints"])
        self.assertIn("Repository architecture:", graph["architectureSummary"]["promptText"])
        self.assertNotIn("semanticGraph", graph)
        semantic_node_labels = {node["label"] for node in semantic_graph["nodes"]}
        self.assertIn("Flow", semantic_node_labels)
        self.assertIn("Code semantics:", graph["architectureSummary"]["promptText"])
        self.assertNotIn(str(checkout_dir), json.dumps(graph))
        self.assertNotIn(str(checkout_dir), json.dumps(semantic_graph))

    def test_build_repository_graph_emits_traceability_and_impact_graph(self) -> None:
        cfg = config()
        with tempfile.TemporaryDirectory() as tmp:
            checkout_dir = Path(tmp)
            (checkout_dir / "src" / "auth").mkdir(parents=True)
            (checkout_dir / "tests" / "auth").mkdir(parents=True)
            (checkout_dir / "docs").mkdir()
            (checkout_dir / "config").mkdir()
            (checkout_dir / ".github" / "workflows").mkdir(parents=True)
            (checkout_dir / "src" / "app.py").write_text(
                "from src.auth.session import create_session\n\ncreate_session()\n",
                encoding="utf-8",
            )
            (checkout_dir / "src" / "auth" / "session.py").write_text(
                "def create_session():\n    return 'ok'\n",
                encoding="utf-8",
            )
            (checkout_dir / "tests" / "auth" / "test_session.py").write_text(
                "from src.auth import session\n\n\ndef test_create_session():\n    assert session.create_session()\n",
                encoding="utf-8",
            )
            (checkout_dir / "docs" / "auth.md").write_text(
                f"Auth flow documentation references src/auth/session.py from {checkout_dir / 'private-notes.md'}.\n",
                encoding="utf-8",
            )
            (checkout_dir / "README.md").write_text("Run the app from src/app.py.\n", encoding="utf-8")
            (checkout_dir / "config" / "settings.yaml").write_text("src: src\n", encoding="utf-8")
            (checkout_dir / "package.json").write_text(
                '{"scripts":{"test":"pytest tests","build":"python src/app.py"}}',
                encoding="utf-8",
            )
            (checkout_dir / "pyproject.toml").write_text("[tool.pytest.ini_options]\ntestpaths=['tests']\n", encoding="utf-8")
            (checkout_dir / "tsconfig.json").write_text('{"include":["src/**/*.ts","tests/**/*.ts"]}', encoding="utf-8")
            (checkout_dir / ".github" / "workflows" / "ci.yml").write_text(
                "name: ci\njobs:\n  test:\n    steps:\n      - run: npm test\n      - run: npm run build\n",
                encoding="utf-8",
            )
            (checkout_dir / "Dockerfile").write_text("FROM python:3.12\nCOPY src /app/src\n", encoding="utf-8")

            graph, _semantic_graph = build_repository_graph_bundle(
                cfg,
                {"repo": "octocat/auth", "branch": "main", "commit": "abc123"},
                checkout_dir,
                {"languages": ["Python"], "packageManagers": ["npm"]},
            )

        node_types = {node["id"]: node["type"] for node in graph["nodes"]}
        self.assertEqual(node_types["file:README.md"], "doc")
        self.assertEqual(node_types["file:docs/auth.md"], "doc")
        self.assertEqual(node_types["file:config/settings.yaml"], "config")
        edge_pairs = {(edge["source"], edge["target"], edge["type"]) for edge in graph["edges"]}
        self.assertIn(("file:tests/auth/test_session.py", "file:src/auth/session.py", "tests"), edge_pairs)
        self.assertIn(("file:docs/auth.md", "file:src/auth/session.py", "documents"), edge_pairs)
        self.assertIn(("file:docs/auth.md", "dir:src/auth", "documents"), edge_pairs)
        self.assertTrue(any(edge[0] == "file:package.json" and edge[2] == "configures" for edge in edge_pairs))
        self.assertTrue(any(edge[0] == "file:.github/workflows/ci.yml" and edge[2] == "configures" for edge in edge_pairs))
        trace_edges = [edge for edge in graph["edges"] if edge["type"] in {"tests", "documents", "configures"}]
        self.assertTrue(trace_edges)
        for edge in trace_edges:
            self.assertIn("confidence", edge)
            self.assertLessEqual(len(edge.get("evidence", [])), 4)
            for evidence in edge.get("evidence", []):
                self.assertLessEqual(len(evidence["kind"]), 40)
                self.assertFalse(Path(evidence["file"]).is_absolute())
                self.assertGreater(evidence["line"], 0)
                self.assertLessEqual(len(evidence.get("text", "")), 180)
                self.assertNotIn("\n", evidence.get("text", ""))
                self.assertNotIn("\x00", evidence.get("text", ""))

        impact_graph = graph["impactGraph"]
        self.assertEqual(impact_graph["version"], "impact-graph/0.1")
        session_target = next(target for target in impact_graph["targets"] if target["path"] == "src/auth/session.py")
        self.assertTrue(session_target["relations"]["tests"])
        self.assertTrue(session_target["relations"]["documents"])
        self.assertTrue(session_target["relations"]["configures"])
        self.assertIn("Impact context:", impact_graph["promptText"])
        self.assertIn("src/auth/session.py", impact_graph["promptText"])
        self.assertIn("Impact context:", graph["architectureSummary"]["promptText"])
        self.assertNotIn(str(checkout_dir), json.dumps(graph))

    def test_build_repository_graph_caps_large_repositories_deterministically(self) -> None:
        cfg = config()
        with tempfile.TemporaryDirectory() as tmp:
            checkout_dir = Path(tmp)
            (checkout_dir / "src").mkdir()
            for index in range(180):
                (checkout_dir / "src" / f"module_{index}.py").write_text(
                    f"from src import module_{max(0, index - 1)}\n"
                    f"def run_module_{index}():\n"
                    f"    return module_{max(0, index - 1)}\n",
                    encoding="utf-8",
                )
            first, first_semantic = build_repository_graph_bundle(cfg, {"repo": "octocat/large"}, checkout_dir, {})
            second, second_semantic = build_repository_graph_bundle(cfg, {"repo": "octocat/large"}, checkout_dir, {})

        self.assertEqual(first, second)
        self.assertEqual(first_semantic, second_semantic)
        self.assertLessEqual(len(first["nodes"]), 120)
        self.assertLessEqual(len(first["edges"]), 240)
        self.assertLessEqual(len(first_semantic["nodes"]), 120)
        self.assertLessEqual(len(first_semantic["edges"]), 240)
        self.assertTrue(first["stats"]["truncated"])

    def test_build_repository_graph_extracts_generic_language_semantics(self) -> None:
        cfg = config()
        with tempfile.TemporaryDirectory() as tmp:
            checkout_dir = Path(tmp)
            (checkout_dir / "internal" / "api").mkdir(parents=True)
            (checkout_dir / "internal" / "api" / "server.go").write_text(
                """
package api

type Server struct {}

func (s *Server) HandleHealth() {
    writeHealth()
}

func writeHealth() {}
""".strip(),
                encoding="utf-8",
            )
            _graph, semantic = build_repository_graph_bundle(cfg, {"repo": "octocat/go"}, checkout_dir, {})

        labels = {node["label"] for node in semantic["nodes"]}
        self.assertIn("Server", labels)
        self.assertIn("HandleHealth", labels)
        self.assertIn("writeHealth", labels)
        edge_labels = {(edge["source"], edge["target"], edge["type"]) for edge in semantic["edges"]}
        node_by_label = {node["label"]: node for node in semantic["nodes"]}
        self.assertIn((node_by_label["HandleHealth"]["id"], node_by_label["writeHealth"]["id"], "calls"), edge_labels)

    def test_build_repository_graph_can_use_agent_semantic_fallback(self) -> None:
        cfg = config()
        cfg.semantic_graph_agent_fallback = True
        cfg.semantic_graph_agent_min_symbols = 8
        cfg.semantic_graph_agent_timeout_seconds = 30
        agent_payload = {
            "version": "semantic-code-graph/0.1",
            "summary": "Agent inferred a component graph.",
            "nodes": [
                {
                    "id": "symbol:index.html:Widget",
                    "label": "Widget",
                    "type": "component",
                    "path": "index.html",
                    "line": 4,
                    "signature": "Widget",
                    "importance": 0.8,
                }
            ],
            "edges": [],
            "reviewHints": ["Agent fallback covered template semantics."],
        }
        with tempfile.TemporaryDirectory() as tmp:
            checkout_dir = Path(tmp)
            (checkout_dir / "index.html").write_text("<x-widget></x-widget>\n", encoding="utf-8")
            with patch(
                "pullwise_worker.main.subprocess.run",
                return_value=Mock(returncode=0, stdout=json.dumps(agent_payload), stderr=""),
            ) as run:
                _graph, semantic_graph = build_repository_graph_bundle(cfg, {"repo": "octocat/templates"}, checkout_dir, {})

        run.assert_called_once()
        self.assertEqual(semantic_graph["stats"]["source"], "agent_fallback")
        self.assertEqual(semantic_graph["nodes"][0]["label"], "Widget")
        self.assertIn("Agent fallback covered template semantics.", semantic_graph["reviewHints"])

    def test_parse_audit_swarm_accepts_protocol_payload(self) -> None:
        payload = parse_audit_swarm_payload(json.dumps(audit_payload([issue_card("Bug", severity="P1")])))

        findings = audit_swarm_findings_from_payload(payload) or []
        self.assertEqual(payload["audit_protocol"], "audit-swarm/0.1")
        self.assertEqual(findings[0]["title"], "Bug")
        self.assertEqual(summarize(findings)["high"], 1)

    def test_parse_audit_swarm_skips_codex_json_event_stream(self) -> None:
        payload = parse_audit_swarm_payload(
            '{"event":"review_progress","issue_cards":[]}\n'
            + json.dumps(audit_payload([issue_card("Bug", severity="P1")]))
        )

        findings = audit_swarm_findings_from_payload(payload) or []
        self.assertEqual(findings[0]["title"], "Bug")

    def test_audit_swarm_confirmed_verdict_without_evidence_is_not_static_proof(self) -> None:
        payload = audit_payload(
            [issue_card("Unsupported verifier confirmation", issue_id="issue-unsupported")],
            [{"issue_id": "issue-unsupported", "verdict": "confirmed"}],
        )

        findings = audit_swarm_findings_from_payload(payload) or []

        self.assertEqual(findings[0]["verificationStatus"], "potential_risk")

    def test_audit_swarm_confirmed_verdict_with_only_proof_strength_is_not_static_proof(self) -> None:
        payload = audit_payload(
            [issue_card("Proof strength only confirmation", issue_id="issue-proof-strength")],
            [{"issue_id": "issue-proof-strength", "verdict": "confirmed", "proof_strength": 3}],
        )

        findings = audit_swarm_findings_from_payload(payload) or []

        self.assertEqual(findings[0]["verificationStatus"], "potential_risk")

    def test_audit_swarm_scan_artifacts_emit_stable_evidence_blocks(self) -> None:
        card = {
            **issue_card("Refresh token rotation may not be atomic", issue_id="issue-refresh", severity="P1"),
            "claim": "Token invalidation and issuance are not in one transaction.",
            "evidence": [{"summary": "createRefreshToken runs before old-token invalidation is confirmed."}],
            "suggested_test": "Mock a failure between issuance and invalidation.",
            "violated_invariants": ["Refresh tokens must be single-use."],
        }
        result = {
            "issue_id": "issue-refresh",
            "verifier_role": "prover",
            "verdict": "confirmed",
            "confidence": 0.91,
            "proof_type": "failing_test",
            "proof_strength": 3,
            "result_summary": "A mocked failure leaves both tokens valid.",
            "commands_run": ["pnpm test auth -- refresh-token-rotation"],
            "evidence": ["Focused test reproduced the token rotation gap."],
        }

        audit = audit_swarm_scan_artifacts(
            "report",
            config=config(),
            audit_payload=audit_payload([card], [result]),
            verification_audit=verification_audit_payload(
                candidate_count=2,
                reported_findings=[
                    {
                        "id": "issue-refresh",
                        "title": card["title"],
                        "file": "src/app.py",
                        "line": 12,
                        "verificationStatus": "verified",
                    }
                ],
                rejected_reasons={"missing_evidence": 1},
            ),
            summary="2 candidates evaluated; 1 reported.",
        )

        blocks = audit["evidenceBlocks"]
        by_kind = {block["kind"]: block for block in blocks}
        self.assertEqual(audit["protocol"], "audit-swarm/0.1")
        self.assertEqual(by_kind["claim"]["summary"], "Token invalidation and issuance are not in one transaction.")
        self.assertEqual(by_kind["code_location"]["file"], "src/app.py")
        self.assertEqual(by_kind["false_positive_check"]["summary"], "Check for upstream guard.")
        self.assertEqual(by_kind["invariant"]["summary"], "Refresh tokens must be single-use.")
        self.assertEqual(by_kind["verifier_verdict"]["verdict"], "confirmed")
        command_blocks = [block for block in blocks if block["kind"] == "command" and block.get("command")]
        self.assertEqual(command_blocks[0]["command"], "pnpm test auth -- refresh-token-rotation")

    def test_completion_audit_flags_missing_card_fields_and_unknown_verification_ids(self) -> None:
        audit = completion_audit_payload(
            result_status="done",
            audit_payload=audit_payload(
                [
                    {
                        **issue_card("Missing contract", issue_id="issue-missing", file="", evidence=[]),
                        "claim": "",
                        "reproduction_idea": "",
                        "suggested_test": "",
                    }
                ],
                [
                    {
                        "issue_id": "issue-other",
                        "verifier_role": "prover",
                        "verdict": "confirmed",
                        "confidence": 0.9,
                        "proof_type": "static_proof",
                        "proof_strength": 2,
                        "evidence": ["Verifier checked the wrong issue id."],
                        "commands_run": [],
                        "result_summary": "Verifier checked the wrong issue id.",
                    }
                ],
            ),
            preflight={"mode": "static", "verifier": {"enabled": False, "runs": []}},
            verification_audit={"candidateCount": 1, "reportedCount": 1},
            logs_summary="review ok",
            candidate_count=1,
            rejected_reasons={},
        )

        checks = {check["id"]: check for check in audit["checks"]}
        self.assertEqual(audit["status"], "failed")
        self.assertTrue(audit["blockers"])
        self.assertEqual(checks["issue_card_locations"]["status"], "failed")
        self.assertEqual(checks["issue_card_evidence"]["status"], "failed")
        self.assertEqual(checks["issue_card_reproduction_ideas"]["status"], "failed")
        self.assertEqual(checks["issue_card_suggested_tests"]["status"], "failed")
        self.assertEqual(checks["verification_issue_references"]["status"], "failed")

    def test_review_prompt_reuses_prior_issue_ids_for_convergence(self) -> None:
        prompt = worker_main.review_prompt(
            {
                "repo": "acme/api",
                "branch": "main",
                "commit": "b" * 40,
                "convergence_context": {
                    "previous_head_sha": "a" * 40,
                    "open_findings": [
                        {
                            "fingerprint": "fp-old",
                            "issue_id": "issue-old",
                            "title": "Old bug",
                            "file": "src/app.py",
                            "line": 12,
                            "status": "open",
                        }
                    ],
                },
            }
        )

        self.assertIn("reuse its issue_id exactly", prompt)
        self.assertIn("issue-old", prompt)

    def test_review_prompt_uses_requested_review_output_language(self) -> None:
        prompt = worker_main.review_prompt(
            {
                "repo": "acme/api",
                "branch": "main",
                "commit": "b" * 40,
                "review_output_language": "zh-CN",
                "review_output_language_label": "Chinese",
            }
        )

        self.assertIn("Write every human-facing review output field in Chinese.", prompt)
        self.assertIn("Keep JSON keys, enum values, file paths, commands, identifiers, and code excerpts unchanged.", prompt)

    def test_review_prompt_defaults_to_english_output_language(self) -> None:
        prompt = worker_main.review_prompt({"repo": "acme/api", "branch": "main", "commit": "pending"})

        self.assertIn("Write every human-facing review output field in English.", prompt)

    def test_review_prompt_encourages_subagents_without_changing_output_shape(self) -> None:
        prompt = worker_main.review_prompt({"repo": "acme/api", "branch": "main", "commit": "pending"})

        self.assertIn("If the agent CLI supports subagents", prompt)
        self.assertIn("preserve the required final JSON output structure exactly", prompt)
        self.assertIn("Return only JSON with top-level `audit_protocol`, `issue_cards`, and `verification_results`", prompt)

    def test_repository_semantic_agent_prompt_encourages_subagents_without_changing_output_shape(self) -> None:
        prompt = worker_main.repository_semantic_agent_prompt(
            {"repo": "acme/api", "branch": "main", "commit": "pending"},
            [],
            Path("checkout"),
            {"nodes": []},
        )

        self.assertIn("If the agent CLI supports subagents", prompt)
        self.assertIn("preserve the required final JSON output structure exactly", prompt)
        self.assertIn("Return only JSON with top-level `version`, `summary`, `nodes`, `edges`, and optional `reviewHints`", prompt)

    def test_review_prompt_includes_repository_graph_architecture_summary(self) -> None:
        prompt = worker_main.review_prompt(
            {
                "repo": "acme/api",
                "branch": "main",
                "commit": "abc123",
                "architecture_summary": {
                    "promptText": "Repository architecture: API routes call worker result reconciliation.",
                },
            }
        )

        self.assertIn("Repository architecture context:", prompt)
        self.assertIn("worker result reconciliation", prompt)

    def test_run_codex_review_normalizes_checkout_absolute_file_paths(self) -> None:
        worker_config = config()
        checkout_dir = Path(worker_config.work_dir) / "job_1"
        checkout_file = checkout_dir / "src" / "app.py"

        with patch(
            "pullwise_worker.main.run_codex_provider_review",
            return_value=(
                audit_payload(
                    [
                        issue_card("Inside checkout", severity="P1", file=str(checkout_file), issue_id="inside"),
                        issue_card("Outside checkout", severity="P2", file="/var/log/pullwise/server.log", issue_id="outside"),
                    ]
                ),
                {"critical": 0, "high": 1, "medium": 1, "low": 0, "info": 0},
                "review ok",
            ),
        ):
            payload, _summary, _logs = run_codex_review(
                worker_config,
                {"job_id": "job_1", "repo": "acme/api"},
                checkout_dir,
            )

        findings = audit_swarm_findings_from_payload(payload) or []
        self.assertEqual(findings[0]["file"], "src/app.py")
        self.assertEqual(findings[1]["file"], "")

    def test_audit_swarm_blank_issue_id_keeps_confirmed_verification(self) -> None:
        card = issue_card("Blank id keeps verifier", issue_id="")
        fallback_id = (audit_swarm_findings_from_payload(audit_payload([card])) or [])[0]["id"]
        payload = audit_payload(
            [card],
            [
                {
                    "issue_id": fallback_id,
                    "verifier_role": "prover",
                    "verdict": "confirmed",
                    "proof_type": "static_proof",
                    "proof_strength": 3,
                    "result_summary": "Verifier confirmed the fallback issue.",
                    "evidence": ["Static proof matched the fallback issue id."],
                }
            ],
        )

        findings = audit_swarm_findings_from_payload(payload) or []
        filtered = filter_audit_swarm_payload_by_findings(payload, findings)

        self.assertEqual(findings[0]["id"], fallback_id)
        self.assertEqual(findings[0]["verificationStatus"], "static_proof")
        self.assertEqual(filtered["issue_cards"][0]["issue_id"], fallback_id)
        self.assertEqual(len(filtered["verification_results"]), 1)

    def test_audit_swarm_filter_keeps_verification_result_id_aliases(self) -> None:
        aliases = ["issue_id", "issueId", "id", "candidate_id", "candidateId"]
        for alias in aliases:
            with self.subTest(alias=alias):
                result = {
                    alias: "alias-issue",
                    "verifier_role": "prover",
                    "verdict": "confirmed",
                    "proof_type": "static_proof",
                    "proof_strength": 3,
                    "result_summary": "Verifier confirmed the aliased issue.",
                    "evidence": ["Static proof matched the aliased issue id."],
                }
                payload = audit_payload([issue_card("Aliased verifier", issue_id="alias-issue")], [result])

                findings = audit_swarm_findings_from_payload(payload) or []
                filtered = filter_audit_swarm_payload_by_findings(payload, findings)

                self.assertEqual(findings[0]["verificationStatus"], "static_proof")
                self.assertEqual(len(filtered["verification_results"]), 1)
                self.assertEqual(filtered["verification_results"][0]["issue_id"], "alias-issue")

    def test_audit_swarm_single_blank_issue_id_matches_blank_verification_result(self) -> None:
        card = issue_card("Blank id verifier", issue_id="")
        payload = audit_payload(
            [card],
            [
                {
                    "issue_id": "",
                    "verifier_role": "prover",
                    "verdict": "confirmed",
                    "proof_type": "static_proof",
                    "proof_strength": 3,
                    "result_summary": "Verifier confirmed the only blank issue.",
                    "evidence": ["Static proof matched the only blank issue id."],
                }
            ],
        )

        findings = audit_swarm_findings_from_payload(payload) or []
        filtered = filter_audit_swarm_payload_by_findings(payload, findings)

        self.assertTrue(findings[0]["id"].startswith("audit_swarm_"))
        self.assertEqual(findings[0]["verificationStatus"], "static_proof")
        self.assertEqual(filtered["verification_results"][0]["issue_id"], findings[0]["id"])

    def test_audit_swarm_normalizes_nested_reproduction_and_verifier_log_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            checkout_dir = Path(tmp)
            absolute_source = checkout_dir / "src" / "app.py"
            absolute_test = checkout_dir / "tests" / "test_app.py"
            absolute_card_log = checkout_dir / ".pullwise" / "card.log"
            absolute_result_log = checkout_dir / ".pullwise" / "result.log"
            card = issue_card(
                "Nested paths are relative",
                issue_id="nested-paths",
                file=str(absolute_source),
                evidence=[{"summary": "Source proof.", "file": str(absolute_source), "logPath": str(absolute_card_log)}],
            )
            card["reproduction"] = {
                "commands": ["pytest tests/test_app.py"],
                "testFile": str(absolute_test),
                "logPath": str(absolute_card_log),
            }
            result = {
                "issue_id": "nested-paths",
                "verifier_role": "prover",
                "verdict": "confirmed",
                "proof_type": "failing_test",
                "commands_run": ["pytest tests/test_app.py"],
                "result_summary": "Test failed before the fix.",
                "evidence": ["Verifier log confirmed the failure."],
                "logPath": str(absolute_result_log),
            }

            normalized = normalize_audit_swarm_files_for_checkout(audit_payload([card], [result]), checkout_dir)

        findings = audit_swarm_findings_from_payload(normalized) or []
        reproduction = findings[0]["reproduction"]
        log_paths = [item["logPath"] for item in findings[0]["evidence"] if item.get("logPath")]
        self.assertEqual(findings[0]["file"], "src/app.py")
        self.assertEqual(reproduction["testFile"], "tests/test_app.py")
        self.assertEqual(reproduction["logPath"], ".pullwise/card.log")
        self.assertIn(".pullwise/card.log", log_paths)
        self.assertIn(".pullwise/result.log", log_paths)

    def test_deterministic_checks_report_readme_package_script_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            checkout_dir = Path(tmp)
            (checkout_dir / "package.json").write_text(
                '{\n  "scripts": {\n    "build": "vite build"\n  }\n}\n',
                encoding="utf-8",
            )
            (checkout_dir / "README.md").write_text(
                "# App\n\nRun `npm run dev` to start local development.\n",
                encoding="utf-8",
            )

            findings = run_deterministic_repository_checks(
                {"repo": "acme/app", "commit": "abc1234"},
                checkout_dir,
            )

        self.assertEqual(len(findings), 1)
        finding = findings[0]
        self.assertEqual(finding["verificationStatus"], "static_proof")
        self.assertEqual(finding["severity"], "medium")
        self.assertEqual(finding["category"], "Docs")
        self.assertEqual(finding["file"], "README.md")
        self.assertEqual(finding["line"], 3)
        self.assertEqual(finding["reproduction"]["commands"], ["npm run dev"])
        self.assertEqual(finding["affectedLocations"][0], {"file": "README.md", "startLine": 3, "endLine": 3})
        self.assertEqual(finding["affectedLocations"][1], {"file": "package.json", "startLine": 2, "endLine": 2})
        self.assertEqual([item["type"] for item in finding["evidence"]], ["documentation", "code"])
        self.assertIn("does not define `dev`", finding["evidence"][1]["summary"])
        self.assertIn("no project scripts were executed", finding["verificationSummary"])

    def test_deterministic_checks_report_ci_workflow_missing_package_script(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            checkout_dir = Path(tmp)
            (checkout_dir / "package.json").write_text(
                '{\n  "scripts": {\n    "build": "vite build"\n  }\n}\n',
                encoding="utf-8",
            )
            workflow_dir = checkout_dir / ".github" / "workflows"
            workflow_dir.mkdir(parents=True)
            (workflow_dir / "ci.yml").write_text(
                "name: CI\n"
                "on: [push]\n"
                "jobs:\n"
                "  test:\n"
                "    runs-on: ubuntu-latest\n"
                "    steps:\n"
                "      - run: npm run ci\n",
                encoding="utf-8",
            )

            findings = run_deterministic_repository_checks(
                {"repo": "acme/app", "commit": "abc1234"},
                checkout_dir,
            )

        self.assertEqual(len(findings), 1)
        finding = findings[0]
        self.assertEqual(finding["verificationStatus"], "static_proof")
        self.assertEqual(finding["severity"], "low")
        self.assertEqual(finding["category"], "CI")
        self.assertEqual(finding["file"], ".github/workflows/ci.yml")
        self.assertEqual(finding["line"], 7)
        self.assertEqual(finding["reproduction"]["commands"][0], "npm run ci")
        self.assertEqual(
            finding["affectedLocations"],
            [
                {"file": ".github/workflows/ci.yml", "startLine": 7, "endLine": 7},
                {"file": "package.json", "startLine": 2, "endLine": 2},
            ],
        )
        self.assertEqual([item["type"] for item in finding["evidence"]], ["tool", "code"])
        self.assertIn("does not define `ci`", finding["evidence"][1]["summary"])
        self.assertIn("workflow was not executed", finding["verificationSummary"])
        self.assertIn("working-directory", finding["limitations"][0])

    def test_deterministic_checks_report_dockerfile_missing_copy_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            checkout_dir = Path(tmp)
            (checkout_dir / "package.json").write_text("{}", encoding="utf-8")
            (checkout_dir / "Dockerfile").write_text(
                "FROM node:22\n"
                "COPY package.json ./\n"
                "COPY missing/config.json /app/config.json\n"
                "COPY --from=builder /app/dist ./dist\n"
                "ADD https://example.test/archive.tar.gz /tmp/archive.tar.gz\n"
                "COPY src/*.js /app/\n",
                encoding="utf-8",
            )

            findings = run_deterministic_repository_checks(
                {"repo": "acme/app", "commit": "abc1234"},
                checkout_dir,
            )

        self.assertEqual(len(findings), 1)
        finding = findings[0]
        self.assertEqual(finding["verificationStatus"], "static_proof")
        self.assertEqual(finding["severity"], "medium")
        self.assertEqual(finding["category"], "Build")
        self.assertEqual(finding["file"], "Dockerfile")
        self.assertEqual(finding["line"], 3)
        self.assertEqual(finding["affectedLocations"], [{"file": "Dockerfile", "startLine": 3, "endLine": 3}])
        self.assertEqual([item["type"] for item in finding["evidence"]], ["code", "tool"])
        self.assertIn("missing/config.json", finding["evidence"][0]["summary"])
        self.assertEqual(finding["reproduction"]["commands"], ["docker build -f 'Dockerfile' ."])
        self.assertIn("docker build was not executed", finding["verificationSummary"])
        self.assertIn("literal local path", finding["whyNotFalsePositive"][0])
        self.assertIn("repository root as build context", finding["limitations"][0])

    def test_deterministic_checks_report_redacted_committed_secret(self) -> None:
        secret = "ghp_a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"
        with tempfile.TemporaryDirectory() as tmp:
            checkout_dir = Path(tmp)
            (checkout_dir / "config").mkdir()
            (checkout_dir / "config" / "prod.env").write_text(
                f"API_URL=https://api.example.test\nGITHUB_TOKEN={secret}\n",
                encoding="utf-8",
            )
            (checkout_dir / ".env.example").write_text(
                f"GITHUB_TOKEN={secret}\n",
                encoding="utf-8",
            )
            (checkout_dir / "tests").mkdir()
            (checkout_dir / "tests" / "fixture.py").write_text(
                f"TOKEN = '{secret}'\n",
                encoding="utf-8",
            )

            findings = run_deterministic_repository_checks(
                {"repo": "acme/app", "commit": "abc1234"},
                checkout_dir,
            )

        self.assertEqual(len(findings), 1)
        finding = findings[0]
        serialized = json.dumps(finding)
        self.assertNotIn(secret, serialized)
        self.assertEqual(finding["verificationStatus"], "static_proof")
        self.assertEqual(finding["severity"], "high")
        self.assertEqual(finding["category"], "Security")
        self.assertEqual(finding["file"], "config/prod.env")
        self.assertEqual(finding["line"], 2)
        self.assertEqual(finding["affectedLocations"], [{"file": "config/prod.env", "startLine": 2, "endLine": 2}])
        self.assertEqual(finding["evidence"][0]["type"], "code")
        self.assertIn("full value redacted", finding["evidence"][0]["summary"])
        self.assertEqual(finding["reproduction"]["commands"], ['git grep -n "ghp_" -- \'config/prod.env\''])
        self.assertIn("provider API validation", finding["verificationSummary"])
        self.assertIn("excludes common docs", finding["whyNotFalsePositive"][2])
        self.assertIn("rotate", finding["fixRisks"])

    def test_collect_preflight_metadata_reports_static_repository_environment(self) -> None:
        worker_config = config()
        worker_config.provider_chain = ["codex", "opencode"]
        worker_config.opencode_command = "opencode"
        with tempfile.TemporaryDirectory() as tmp:
            checkout_dir = Path(tmp)
            (checkout_dir / "package.json").write_text(
                json.dumps(
                    {
                        "packageManager": "pnpm@9.1.0",
                        "scripts": {
                            "build": "vite build",
                            "test": "vitest run",
                            "lint": "eslint .",
                        },
                    }
                ),
                encoding="utf-8",
            )
            (checkout_dir / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n", encoding="utf-8")
            (checkout_dir / "pyproject.toml").write_text("[project]\nname = 'demo'\n", encoding="utf-8")
            (checkout_dir / "Dockerfile").write_text("FROM node:22\n", encoding="utf-8")
            (checkout_dir / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
            workflow_dir = checkout_dir / ".github" / "workflows"
            workflow_dir.mkdir(parents=True)
            (workflow_dir / "ci.yml").write_text("name: CI\n", encoding="utf-8")
            devcontainer_dir = checkout_dir / ".devcontainer"
            devcontainer_dir.mkdir()
            (devcontainer_dir / "devcontainer.json").write_text('{"name":"demo"}\n', encoding="utf-8")

            with patch(
                "pullwise_worker.main.safe_tool_version",
                side_effect=lambda name, command: {
                    "name": name,
                    "command": worker_main.public_tool_version_command(command),
                    "available": True,
                    "exitCode": 0,
                    "output": f"{name} ok",
                },
            ):
                preflight = collect_preflight_metadata(
                    worker_config,
                    {"repo": "acme/app", "branch": "main", "commit": "abc1234"},
                    checkout_dir,
                )

        self.assertEqual(preflight["mode"], "static")
        self.assertEqual(preflight["execution"], "no_project_scripts")
        self.assertIn("pnpm", preflight["packageManagers"])
        self.assertIn("JavaScript/TypeScript", preflight["languages"])
        self.assertIn("Python", preflight["languages"])
        self.assertEqual(preflight["availableScripts"], ["build", "lint", "test"])
        self.assertIn({"file": "package.json", "type": "node"}, preflight["manifests"])
        self.assertIn({"file": "pnpm-lock.yaml", "type": "pnpm-lock"}, preflight["manifests"])
        self.assertIn({"file": "Dockerfile", "type": "dockerfile"}, preflight["manifests"])
        self.assertIn({"file": "docker-compose.yml", "type": "docker-compose"}, preflight["manifests"])
        self.assertIn({"file": ".github/workflows/ci.yml", "type": "github-actions-workflow"}, preflight["manifests"])
        self.assertIn({"file": ".devcontainer/devcontainer.json", "type": "devcontainer"}, preflight["manifests"])
        self.assertEqual(
            len([item for item in preflight["manifests"] if item == {"file": "Dockerfile", "type": "dockerfile"}]),
            1,
        )
        self.assertEqual(
            [tool["name"] for tool in preflight["toolVersions"]],
            ["git", "node", "python", "pnpm", "codex", "opencode"],
        )
        self.assertIn("environment", preflight)
        self.assertIn("pythonVersion", preflight["environment"])
        self.assertEqual(preflight["environment"]["checkoutRoot"], "repository-root")
        self.assertEqual(preflight["environment"]["pythonExecutable"], Path(sys.executable).name)
        serialized_preflight = json.dumps(preflight)
        self.assertNotIn(str(checkout_dir), serialized_preflight)
        self.assertNotIn(sys.executable, serialized_preflight)
        self.assertGreaterEqual(preflight["repositoryStats"]["fileCount"], 7)
        self.assertGreater(preflight["repositoryStats"]["totalBytes"], 0)
        self.assertEqual(preflight["repositoryLimits"]["maxFiles"], worker_config.max_repo_files)
        self.assertEqual(preflight["repositoryLimits"]["maxBytes"], worker_config.max_repo_bytes)
        self.assertIn("worker environment", preflight["summary"])
        self.assertIn("no project scripts were executed", preflight["summary"])

    def test_collect_preflight_metadata_redacts_tool_version_executable_paths(self) -> None:
        worker_config = config()
        worker_config.provider_chain = []
        with tempfile.TemporaryDirectory() as tmp:
            checkout_dir = Path(tmp)

            with patch(
                "pullwise_worker.main.subprocess.run",
                return_value=subprocess.CompletedProcess([], 0, stdout="Python 3.12.0\n", stderr=""),
            ) as run:
                preflight = collect_preflight_metadata(
                    worker_config,
                    {"repo": "acme/app", "branch": "main", "commit": "abc1234"},
                    checkout_dir,
                )

        python_tool = next(tool for tool in preflight["toolVersions"] if tool["name"] == "python")
        self.assertEqual(python_tool["command"], f"{Path(sys.executable).name} --version")
        self.assertNotIn(sys.executable, python_tool["command"])
        self.assertNotIn(json.dumps(sys.executable)[1:-1], json.dumps(preflight))
        run.assert_any_call(
            [sys.executable, "--version"],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=5,
        )

    def test_declared_package_manager_stays_first_when_lockfiles_conflict(self) -> None:
        worker_config = config()
        worker_config.verifier_enabled = True
        worker_config.verifier_host_execution_allowed = True
        worker_config.verifier_scripts = ["test"]
        with tempfile.TemporaryDirectory() as tmp:
            checkout_dir = Path(tmp)
            (checkout_dir / "package.json").write_text(
                json.dumps(
                    {
                        "packageManager": "pnpm@9.1.0",
                        "scripts": {"test": "vitest run"},
                    }
                ),
                encoding="utf-8",
            )
            (checkout_dir / "package-lock.json").write_text('{"lockfileVersion": 3}\n', encoding="utf-8")
            (checkout_dir / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n", encoding="utf-8")

            with patch(
                "pullwise_worker.main.safe_tool_version",
                side_effect=lambda name, command: {
                    "name": name,
                    "command": " ".join(command),
                    "available": True,
                    "exitCode": 0,
                    "output": f"{name} ok",
                },
            ):
                preflight = collect_preflight_metadata(
                    worker_config,
                    {"repo": "acme/app", "branch": "main", "commit": "abc1234"},
                    checkout_dir,
                )

            self.assertEqual(preflight["packageManagers"][0], "pnpm")

            with patch("pullwise_worker.main.subprocess.run", return_value=Mock(returncode=0, stdout="", stderr="")) as run:
                verifier, findings, logs = run_verifier_commands(
                    worker_config,
                    {"job_id": "job_verify_pnpm", "repo": "acme/app", "commit": "abc1234"},
                    checkout_dir,
                    preflight,
                )

        self.assertEqual(run.call_args_list[0].args[0], ["pnpm", "install", "--frozen-lockfile", "--ignore-scripts"])
        self.assertEqual(run.call_args_list[1].args[0], ["pnpm", "run", "test"])
        self.assertEqual(findings, [])
        self.assertIn("2 allowlisted command", logs)
        self.assertTrue(verifier["enabled"])

    def test_verifier_is_disabled_by_default_and_does_not_run_scripts(self) -> None:
        worker_config = config()
        preflight = {"packageManagers": ["npm"], "availableScripts": ["test"]}

        with patch("pullwise_worker.main.subprocess.run") as run:
            verifier, findings, logs = run_verifier_commands(
                worker_config,
                {"job_id": "job_verify", "repo": "acme/app", "commit": "abc1234"},
                Path(worker_config.work_dir),
                preflight,
            )

        run.assert_not_called()
        self.assertFalse(verifier["enabled"])
        self.assertEqual(verifier["runs"], [])
        self.assertEqual(findings, [])
        self.assertEqual(logs, "verifier disabled")

    def test_verifier_enabled_without_host_execution_permission_does_not_run_scripts(self) -> None:
        worker_config = config()
        worker_config.verifier_enabled = True
        worker_config.verifier_scripts = ["test"]
        checkout_dir = Path(worker_config.work_dir) / "job_verify_untrusted"
        checkout_dir.mkdir(parents=True)
        preflight = {"packageManagers": ["npm"], "availableScripts": ["test"]}

        with patch("pullwise_worker.main.subprocess.run") as run:
            verifier, findings, logs = run_verifier_commands(
                worker_config,
                {"job_id": "job_verify_untrusted", "repo": "acme/app", "commit": "abc1234"},
                checkout_dir,
                preflight,
            )

        run.assert_not_called()
        self.assertTrue(verifier["enabled"])
        self.assertEqual(verifier["runs"], [])
        self.assertEqual(findings, [])
        self.assertIn("host execution is not allowed", verifier["summary"])
        self.assertEqual(logs, "verifier host execution disabled")

    def test_verifier_failed_script_becomes_verified_finding(self) -> None:
        worker_config = config()
        worker_config.verifier_enabled = True
        worker_config.verifier_host_execution_allowed = True
        worker_config.verifier_scripts = ["test"]
        checkout_dir = Path(worker_config.work_dir) / "job_verify"
        checkout_dir.mkdir(parents=True)
        (checkout_dir / "package.json").write_text(
            '{\n  "scripts": {\n    "test": "vitest run"\n  }\n}\n',
            encoding="utf-8",
        )
        (checkout_dir / "package-lock.json").write_text('{"lockfileVersion": 3}\n', encoding="utf-8")
        preflight = {"packageManagers": ["npm"], "availableScripts": ["test"]}

        install_completed = Mock(returncode=0, stdout="installed\n", stderr="")
        test_failed_first = Mock(returncode=1, stdout="FAIL tests/example.test.js\n", stderr="AssertionError\n")
        test_failed_second = Mock(returncode=1, stdout="FAIL tests/example.test.js\n", stderr="AssertionError again\n")
        with patch(
            "pullwise_worker.main.subprocess.run",
            side_effect=[install_completed, test_failed_first, test_failed_second],
        ) as run:
            verifier, findings, logs = run_verifier_commands(
                worker_config,
                {"job_id": "job_verify", "repo": "acme/app", "commit": "abc1234"},
                checkout_dir,
                preflight,
            )

        self.assertEqual(run.call_count, 3)
        self.assertEqual(run.call_args_list[0].args[0], ["npm", "ci", "--ignore-scripts"])
        self.assertEqual(run.call_args_list[1].args[0], ["npm", "run", "test"])
        self.assertEqual(run.call_args_list[2].args[0], ["npm", "run", "test"])
        self.assertTrue(verifier["enabled"])
        self.assertEqual(verifier["runs"][0]["script"], "install-deps")
        self.assertEqual(verifier["runs"][0]["status"], "passed")
        self.assertEqual(verifier["runs"][1]["status"], "failed")
        self.assertEqual(verifier["runs"][1]["exitCode"], 1)
        self.assertTrue(verifier["runs"][1]["confirmedFailure"])
        self.assertEqual([attempt["status"] for attempt in verifier["runs"][1]["attempts"]], ["failed", "failed"])
        self.assertIn("2 allowlisted command(s): 1 passed, 1 failed", logs)
        self.assertIn("1 failed", logs)
        self.assertEqual(len(findings), 1)
        finding = findings[0]
        self.assertEqual(finding["verificationStatus"], "verified")
        self.assertEqual(finding["category"], "Tests")
        self.assertEqual(finding["affectedLocations"], [{"file": "package.json", "startLine": 3, "endLine": 3}])
        self.assertEqual(finding["evidence"][0]["type"], "runtime_log")
        self.assertEqual(finding["evidence"][0]["command"], "npm run test")
        self.assertTrue(finding["evidence"][0]["outputRedacted"])
        self.assertNotIn("output", finding["evidence"][0])
        self.assertIn("withheld", finding["evidence"][0]["summary"])
        self.assertEqual(
            finding["reproduction"]["actual"],
            "Command exited 1; stdout/stderr is withheld from shared payloads.",
        )
        self.assertIn("two consecutive attempts", finding["whyNotFalsePositive"][0])
        log_path = Path(worker_config.log_dir) / verifier["runs"][1]["logPath"]
        self.assertIn("--- attempt 1 (failed exit 1) ---", log_path.read_text(encoding="utf-8"))
        self.assertIn("--- attempt 2 (failed exit 1) ---", log_path.read_text(encoding="utf-8"))
        self.assertIn("AssertionError", log_path.read_text(encoding="utf-8"))
        self.assertTrue(verifier["runs"][1]["outputRedacted"])
        self.assertNotIn("output", verifier["runs"][1])
        self.assertTrue(verifier["runs"][1]["attempts"][0]["outputRedacted"])
        self.assertNotIn("output", verifier["runs"][1]["attempts"][0])

    def test_verifier_command_env_does_not_inherit_host_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            checkout_dir = Path(tmp) / "checkout"
            checkout_dir.mkdir()
            with patch.dict(
                os.environ,
                {
                    "PATH": "/bin",
                    "AWS_SECRET_ACCESS_KEY": "secret",
                    "GITHUB_TOKEN": "token",
                    "OPENAI_API_KEY": "key",
                },
                clear=False,
            ):
                env = verifier_command_env(checkout_dir)

        self.assertEqual(env["PATH"], "/bin")
        self.assertEqual(env["CI"], "true")
        self.assertNotIn("AWS_SECRET_ACCESS_KEY", env)
        self.assertNotIn("GITHUB_TOKEN", env)
        self.assertNotIn("OPENAI_API_KEY", env)

    def test_verifier_dependency_install_failure_blocks_project_scripts(self) -> None:
        worker_config = config()
        worker_config.verifier_enabled = True
        worker_config.verifier_host_execution_allowed = True
        worker_config.verifier_scripts = ["test"]
        checkout_dir = Path(worker_config.work_dir) / "job_verify_install"
        checkout_dir.mkdir(parents=True)
        (checkout_dir / "package.json").write_text(
            '{\n  "scripts": {\n    "test": "vitest run"\n  }\n}\n',
            encoding="utf-8",
        )
        (checkout_dir / "package-lock.json").write_text('{"lockfileVersion": 3}\n', encoding="utf-8")
        preflight = {"packageManagers": ["npm"], "availableScripts": ["test"]}

        install_failed_first = Mock(returncode=1, stdout="", stderr="registry auth failed\n")
        install_failed_second = Mock(returncode=1, stdout="", stderr="registry auth still failed\n")
        with patch(
            "pullwise_worker.main.subprocess.run",
            side_effect=[install_failed_first, install_failed_second],
        ) as run:
            verifier, findings, logs = run_verifier_commands(
                worker_config,
                {"job_id": "job_verify_install", "repo": "acme/app", "commit": "abc1234"},
                checkout_dir,
                preflight,
            )

        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args_list[0].args[0], ["npm", "ci", "--ignore-scripts"])
        self.assertEqual(run.call_args_list[1].args[0], ["npm", "ci", "--ignore-scripts"])
        self.assertTrue(verifier["enabled"])
        self.assertEqual(verifier["runs"][0]["script"], "install-deps")
        self.assertEqual(verifier["runs"][0]["status"], "failed")
        self.assertEqual(verifier["runs"][0]["exitCode"], 1)
        self.assertTrue(verifier["runs"][0]["confirmedFailure"])
        self.assertEqual([attempt["status"] for attempt in verifier["runs"][0]["attempts"]], ["failed", "failed"])
        self.assertIn("1 allowlisted command(s): 0 passed, 1 failed", logs)
        self.assertEqual(len(findings), 1)
        finding = findings[0]
        self.assertEqual(finding["verificationStatus"], "verified")
        self.assertEqual(finding["category"], "Dependencies")
        self.assertEqual(finding["file"], "package-lock.json")
        self.assertEqual(finding["affectedLocations"], [{"file": "package-lock.json", "startLine": 1, "endLine": 1}])
        self.assertEqual(finding["evidence"][0]["type"], "runtime_log")
        self.assertEqual(finding["evidence"][0]["command"], "npm ci --ignore-scripts")
        self.assertIn("dependency installation", finding["title"])
        self.assertIn("build/test reproduction is blocked", finding["impact"])
        self.assertEqual(finding["reproduction"]["commands"], ["npm ci --ignore-scripts"])
        self.assertEqual(
            finding["reproduction"]["actual"],
            "Command exited 1; stdout/stderr is withheld from shared payloads.",
        )
        self.assertIn("install scripts disabled", " ".join(finding["limitations"]))
        log_path = Path(worker_config.log_dir) / verifier["runs"][0]["logPath"]
        self.assertIn("registry auth failed", log_path.read_text(encoding="utf-8"))
        self.assertIn("registry auth still failed", log_path.read_text(encoding="utf-8"))

    def test_verifier_flaky_dependency_install_continues_to_project_scripts(self) -> None:
        worker_config = config()
        worker_config.verifier_enabled = True
        worker_config.verifier_host_execution_allowed = True
        worker_config.verifier_scripts = ["test"]
        checkout_dir = Path(worker_config.work_dir) / "job_verify_install_flaky"
        checkout_dir.mkdir(parents=True)
        (checkout_dir / "package.json").write_text(
            '{\n  "scripts": {\n    "test": "vitest run"\n  }\n}\n',
            encoding="utf-8",
        )
        (checkout_dir / "package-lock.json").write_text('{"lockfileVersion": 3}\n', encoding="utf-8")
        preflight = {"packageManagers": ["npm"], "availableScripts": ["test"]}

        install_failed = Mock(returncode=1, stdout="", stderr="registry timeout\n")
        install_passed = Mock(returncode=0, stdout="installed\n", stderr="")
        test_passed = Mock(returncode=0, stdout="PASS tests/example.test.js\n", stderr="")
        with patch(
            "pullwise_worker.main.subprocess.run",
            side_effect=[install_failed, install_passed, test_passed],
        ) as run:
            verifier, findings, logs = run_verifier_commands(
                worker_config,
                {"job_id": "job_verify_install_flaky", "repo": "acme/app", "commit": "abc1234"},
                checkout_dir,
                preflight,
            )

        self.assertEqual(run.call_count, 3)
        self.assertEqual(run.call_args_list[0].args[0], ["npm", "ci", "--ignore-scripts"])
        self.assertEqual(run.call_args_list[1].args[0], ["npm", "ci", "--ignore-scripts"])
        self.assertEqual(run.call_args_list[2].args[0], ["npm", "run", "test"])
        self.assertTrue(verifier["enabled"])
        self.assertEqual(verifier["runs"][0]["script"], "install-deps")
        self.assertEqual(verifier["runs"][0]["status"], "flaky")
        self.assertFalse(verifier["runs"][0]["confirmedFailure"])
        self.assertEqual([attempt["status"] for attempt in verifier["runs"][0]["attempts"]], ["failed", "passed"])
        self.assertEqual(verifier["runs"][1]["script"], "test")
        self.assertEqual(verifier["runs"][1]["status"], "passed")
        self.assertIn("2 allowlisted command(s): 1 passed, 0 failed, 1 flaky", logs)
        self.assertEqual(findings, [])

    def test_verifier_flaky_failure_is_not_promoted_to_verified_finding(self) -> None:
        worker_config = config()
        worker_config.verifier_enabled = True
        worker_config.verifier_host_execution_allowed = True
        worker_config.verifier_install_deps = False
        worker_config.verifier_scripts = ["test"]
        checkout_dir = Path(worker_config.work_dir) / "job_verify_flaky"
        checkout_dir.mkdir(parents=True)
        (checkout_dir / "package.json").write_text(
            '{\n  "scripts": {\n    "test": "vitest run"\n  }\n}\n',
            encoding="utf-8",
        )
        preflight = {"packageManagers": ["npm"], "availableScripts": ["test"]}

        test_failed = Mock(returncode=1, stdout="FAIL flaky.test.js\n", stderr="AssertionError\n")
        test_passed = Mock(returncode=0, stdout="PASS flaky.test.js\n", stderr="")
        with patch("pullwise_worker.main.subprocess.run", side_effect=[test_failed, test_passed]) as run:
            verifier, findings, logs = run_verifier_commands(
                worker_config,
                {"job_id": "job_verify_flaky", "repo": "acme/app", "commit": "abc1234"},
                checkout_dir,
                preflight,
            )

        self.assertEqual(run.call_count, 2)
        self.assertEqual(verifier["runs"][0]["status"], "flaky")
        self.assertFalse(verifier["runs"][0]["confirmedFailure"])
        self.assertEqual([attempt["status"] for attempt in verifier["runs"][0]["attempts"]], ["failed", "passed"])
        self.assertIn("1 allowlisted command(s): 0 passed, 0 failed, 1 flaky", logs)
        self.assertEqual(findings, [])
        log_path = Path(worker_config.log_dir) / verifier["runs"][0]["logPath"]
        output = log_path.read_text(encoding="utf-8")
        self.assertIn("FAIL flaky.test.js", output)
        self.assertIn("PASS flaky.test.js", output)

    def test_package_install_command_uses_lockfile_aware_package_manager_commands(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            checkout_dir = Path(tmp)
            self.assertEqual(package_install_command("npm", checkout_dir), [])

            (checkout_dir / "package.json").write_text("{}", encoding="utf-8")
            self.assertEqual(package_install_command("npm", checkout_dir), ["npm", "install", "--ignore-scripts"])

            (checkout_dir / "package-lock.json").write_text("{}", encoding="utf-8")
            self.assertEqual(package_install_command("npm", checkout_dir), ["npm", "ci", "--ignore-scripts"])

        with tempfile.TemporaryDirectory() as tmp:
            checkout_dir = Path(tmp)
            (checkout_dir / "package.json").write_text("{}", encoding="utf-8")
            (checkout_dir / "pnpm-lock.yaml").write_text("lockfileVersion: 9\n", encoding="utf-8")
            self.assertEqual(package_install_command("pnpm", checkout_dir), ["pnpm", "install", "--frozen-lockfile", "--ignore-scripts"])

        with tempfile.TemporaryDirectory() as tmp:
            checkout_dir = Path(tmp)
            (checkout_dir / "package.json").write_text("{}", encoding="utf-8")
            (checkout_dir / "yarn.lock").write_text("", encoding="utf-8")
            self.assertEqual(package_install_command("yarn", checkout_dir), ["yarn", "install", "--frozen-lockfile", "--ignore-scripts"])

        with tempfile.TemporaryDirectory() as tmp:
            checkout_dir = Path(tmp)
            (checkout_dir / "package.json").write_text("{}", encoding="utf-8")
            (checkout_dir / "bun.lockb").write_text("", encoding="utf-8")
            self.assertEqual(package_install_command("bun", checkout_dir), ["bun", "install", "--frozen-lockfile", "--ignore-scripts"])

    def test_run_codex_review_prepends_deterministic_findings(self) -> None:
        worker_config = config()
        checkout_dir = Path(worker_config.work_dir) / "job_static"
        checkout_dir.mkdir(parents=True)
        (checkout_dir / "package.json").write_text(
            '{"scripts":{"build":"vite build"}}',
            encoding="utf-8",
        )
        (checkout_dir / "README.md").write_text("Run `npm run start`.\n", encoding="utf-8")

        with patch(
            "pullwise_worker.main.run_codex_provider_review",
            return_value=(
                audit_payload([issue_card("Provider finding", severity="low", issue_id="provider")]),
                {"critical": 0, "high": 0, "medium": 0, "low": 1, "info": 0},
                "review ok",
            ),
        ):
            payload, summary, _logs = run_codex_review(
                worker_config,
                {"job_id": "job_static", "repo": "acme/api", "commit": "abc1234"},
                checkout_dir,
            )

        findings = audit_swarm_findings_from_payload(payload) or []
        self.assertEqual(findings[0]["verificationStatus"], "static_proof")
        self.assertEqual(findings[0]["title"], "README references missing package script `start`")
        self.assertEqual(findings[1]["title"], "Provider finding")
        self.assertEqual(summary["medium"], 1)
        self.assertEqual(summary["low"], 1)

    def test_run_codex_review_continues_when_deterministic_checks_fail(self) -> None:
        with patch("pullwise_worker.main.run_deterministic_repository_checks", side_effect=RuntimeError("bad read")), \
            patch(
                "pullwise_worker.main.run_codex_provider_review",
                return_value=(
                    audit_payload([issue_card("Provider finding", severity="low", issue_id="provider")]),
                    {"critical": 0, "high": 0, "medium": 0, "low": 1, "info": 0},
                    "review ok",
                ),
            ):
            payload, summary, logs = run_codex_review(
                config(),
                {"job_id": "job_static", "repo": "acme/api"},
                Path("checkout"),
            )

        findings = audit_swarm_findings_from_payload(payload) or []
        self.assertEqual(findings[0]["title"], "Provider finding")
        self.assertEqual(summary["low"], 1)
        self.assertIn("deterministic: bad read", logs)

    def test_reportability_filter_rejects_candidates_without_evidence(self) -> None:
        findings, rejected_reasons, rejected_samples = filter_reportable_findings(
            [
                {
                    "title": "Precise code finding",
                    "file": "src/app.py",
                    "line": 12,
                    "evidence": [{"summary": "The code path has no guard.", "file": "src/app.py", "startLine": 12}],
                    "limitations": ["False-positive check: Confirm no upstream guard exists."],
                },
                {
                    "title": "Repro command finding",
                    "reproduction": {"commands": ["npm test"]},
                    "whyNotFalsePositive": ["The focused command reproduces the failure."],
                },
                {"title": "Only a vague model guess", "severity": "medium", "verificationStatus": "unverified"},
                {"severity": "low", "file": "src/untitled.py", "line": 1},
                "not a finding",
            ]
        )

        self.assertEqual([finding["title"] for finding in findings], ["Precise code finding", "Repro command finding"])
        self.assertEqual(rejected_reasons, {"missing_evidence": 1, "missing_title": 1, "invalid_candidate": 1})
        self.assertEqual(
            rejected_samples,
            [
                {
                    "reason": "missing_evidence",
                    "title": "Only a vague model guess",
                    "severity": "medium",
                    "verificationStatus": "unverified",
                },
                {"reason": "missing_title", "severity": "low", "file": "src/untitled.py", "line": 1},
                {"reason": "invalid_candidate"},
            ],
        )

        audit = verification_audit_payload(
            candidate_count=5,
            reported_findings=findings,
            rejected_reasons=rejected_reasons,
            rejected_samples=rejected_samples,
        )
        self.assertEqual(audit["candidateCount"], 5)
        self.assertEqual(audit["reportedCount"], 2)
        self.assertEqual(audit["rejectedCount"], 3)
        self.assertEqual(audit["potentialRiskCount"], 2)
        self.assertEqual(audit["rejectedSamples"][0]["title"], "Only a vague model guess")

    def test_reportability_filter_rejects_unverified_candidate_without_false_positive_check(self) -> None:
        findings, rejected_reasons, rejected_samples = filter_reportable_findings(
            [
                {
                    "title": "Precise but unchecked candidate",
                    "file": "src/app.py",
                    "line": 12,
                    "evidence": [{"summary": "The code path has no guard.", "file": "src/app.py", "startLine": 12}],
                    "verificationStatus": "potential_risk",
                }
            ]
        )

        self.assertEqual(findings, [])
        self.assertEqual(rejected_reasons, {"missing_false_positive_check": 1})
        self.assertEqual(rejected_samples[0]["title"], "Precise but unchecked candidate")

    def test_reportability_filter_rejects_unverified_candidate_with_only_location(self) -> None:
        findings, rejected_reasons, rejected_samples = filter_reportable_findings(
            [
                {
                    "title": "Location-only candidate",
                    "file": "src/app.py",
                    "line": 12,
                    "verificationStatus": "potential_risk",
                    "limitations": ["False-positive check: Confirm no upstream guard exists."],
                }
            ]
        )

        self.assertEqual(findings, [])
        self.assertEqual(rejected_reasons, {"missing_evidence": 1})
        self.assertEqual(rejected_samples[0]["title"], "Location-only candidate")

    def test_reportability_filter_rejects_verified_candidate_with_only_location(self) -> None:
        findings, rejected_reasons, rejected_samples = filter_reportable_findings(
            [
                {
                    "title": "Self verified location-only candidate",
                    "file": "src/app.py",
                    "line": 12,
                    "verificationStatus": "verified",
                }
            ]
        )

        self.assertEqual(findings, [])
        self.assertEqual(rejected_reasons, {"missing_evidence": 1})
        self.assertEqual(rejected_samples[0]["title"], "Self verified location-only candidate")

    def test_reportability_filter_rejects_self_verified_candidate_without_false_positive_check(self) -> None:
        findings, rejected_reasons, rejected_samples = filter_reportable_findings(
            [
                {
                    "title": "Self verified unchecked candidate",
                    "file": "src/app.py",
                    "line": 12,
                    "evidence": [
                        {
                            "summary": "The checkout handler calls charge() before validating idempotency_key.",
                            "file": "src/app.py",
                            "startLine": 12,
                        }
                    ],
                    "verificationStatus": "verified",
                }
            ]
        )

        self.assertEqual(findings, [])
        self.assertEqual(rejected_reasons, {"missing_false_positive_check": 1})
        self.assertEqual(rejected_samples[0]["title"], "Self verified unchecked candidate")

    def test_reportability_filter_rejects_verified_command_without_supporting_evidence(self) -> None:
        findings, rejected_reasons, rejected_samples = filter_reportable_findings(
            [
                {
                    "title": "Self verified command-only candidate",
                    "file": "src/app.py",
                    "line": 12,
                    "evidence": [{"command": "pytest tests/test_checkout.py"}],
                    "verificationStatus": "verified",
                }
            ]
        )

        self.assertEqual(findings, [])
        self.assertEqual(rejected_reasons, {"missing_evidence": 1})
        self.assertEqual(rejected_samples[0]["title"], "Self verified command-only candidate")

    def test_reportability_filter_rejects_verified_command_with_vacuous_output(self) -> None:
        findings, rejected_reasons, rejected_samples = filter_reportable_findings(
            [
                {
                    "title": "Self verified vacuous output candidate",
                    "file": "src/app.py",
                    "line": 12,
                    "evidence": [
                        {
                            "summary": "The focused checkout regression test covers the failing path.",
                            "command": "pytest tests/test_checkout.py",
                            "output": "OK",
                        }
                    ],
                    "verificationStatus": "verified",
                }
            ]
        )

        self.assertEqual(findings, [])
        self.assertEqual(rejected_reasons, {"missing_false_positive_check": 1})
        self.assertEqual(rejected_samples[0]["title"], "Self verified vacuous output candidate")

    def test_reportability_filter_rejects_verified_reproduction_command_without_false_positive_check(self) -> None:
        findings, rejected_reasons, rejected_samples = filter_reportable_findings(
            [
                {
                    "title": "Self verified reproduction-only candidate",
                    "reproduction": {"commands": ["pytest tests/test_checkout.py"]},
                    "verificationStatus": "verified",
                }
            ]
        )

        self.assertEqual(findings, [])
        self.assertEqual(rejected_reasons, {"missing_false_positive_check": 1})
        self.assertEqual(rejected_samples[0]["title"], "Self verified reproduction-only candidate")

    def test_reportability_filter_rejects_natural_language_reproduction_command(self) -> None:
        findings, rejected_reasons, rejected_samples = filter_reportable_findings(
            [
                {
                    "title": "Natural language reproduction",
                    "reproduction": {"commands": ["open the app and try the checkout flow"]},
                    "whyNotFalsePositive": ["The flow was manually inspected."],
                    "verificationStatus": "potential_risk",
                }
            ]
        )

        self.assertEqual(findings, [])
        self.assertEqual(rejected_reasons, {"missing_evidence": 1})
        self.assertEqual(rejected_samples[0]["title"], "Natural language reproduction")

    def test_reportability_filter_rejects_bare_reproduction_command(self) -> None:
        findings, rejected_reasons, rejected_samples = filter_reportable_findings(
            [
                {
                    "title": "Bare reproduction command",
                    "reproduction": {"commands": ["pytest"]},
                    "whyNotFalsePositive": ["The focused command reproduces the failure."],
                    "verificationStatus": "potential_risk",
                }
            ]
        )

        self.assertEqual(findings, [])
        self.assertEqual(rejected_reasons, {"missing_evidence": 1})
        self.assertEqual(rejected_samples[0]["title"], "Bare reproduction command")

    def test_reportability_filter_rejects_version_reproduction_command(self) -> None:
        findings, rejected_reasons, rejected_samples = filter_reportable_findings(
            [
                {
                    "title": "Version reproduction command",
                    "reproduction": {"commands": ["pytest --version"]},
                    "whyNotFalsePositive": ["The focused command reproduces the failure."],
                    "verificationStatus": "potential_risk",
                }
            ]
        )

        self.assertEqual(findings, [])
        self.assertEqual(rejected_reasons, {"missing_evidence": 1})
        self.assertEqual(rejected_samples[0]["title"], "Version reproduction command")

    def test_reportability_filter_rejects_natural_language_reproduction_path(self) -> None:
        findings, rejected_reasons, rejected_samples = filter_reportable_findings(
            [
                {
                    "title": "Natural language reproduction path",
                    "reproductionPath": "Open the app and try the checkout flow manually.",
                    "whyNotFalsePositive": ["The flow was manually inspected."],
                    "verificationStatus": "potential_risk",
                }
            ]
        )

        self.assertEqual(findings, [])
        self.assertEqual(rejected_reasons, {"missing_evidence": 1})
        self.assertEqual(rejected_samples[0]["title"], "Natural language reproduction path")

    def test_reportability_filter_rejects_natural_language_evidence_command(self) -> None:
        findings, rejected_reasons, rejected_samples = filter_reportable_findings(
            [
                {
                    "title": "Natural language evidence command",
                    "evidence": [
                        {
                            "summary": "The checkout flow was inspected manually.",
                            "command": "Open the app and click through the checkout flow.",
                        }
                    ],
                    "whyNotFalsePositive": ["The flow was manually inspected."],
                    "verificationStatus": "potential_risk",
                }
            ]
        )

        self.assertEqual(findings, [])
        self.assertEqual(rejected_reasons, {"missing_evidence": 1})
        self.assertEqual(rejected_samples[0]["title"], "Natural language evidence command")

    def test_reportability_filter_rejects_natural_language_evidence_log_path(self) -> None:
        findings, rejected_reasons, rejected_samples = filter_reportable_findings(
            [
                {
                    "title": "Natural language evidence log path",
                    "evidence": [
                        {
                            "summary": "The checkout flow was inspected manually.",
                            "logPath": "Open the worker logs and inspect the checkout flow.",
                        }
                    ],
                    "whyNotFalsePositive": ["The focused checkout flow was inspected."],
                    "verificationStatus": "potential_risk",
                }
            ]
        )

        self.assertEqual(findings, [])
        self.assertEqual(rejected_reasons, {"missing_evidence": 1})
        self.assertEqual(rejected_samples[0]["title"], "Natural language evidence log path")

    def test_reportability_filter_rejects_source_file_as_evidence_log_path(self) -> None:
        findings, rejected_reasons, rejected_samples = filter_reportable_findings(
            [
                {
                    "title": "Source file evidence log path",
                    "evidence": [
                        {
                            "summary": "The checkout regression is reproduced by the focused test.",
                            "logPath": "src/app.py",
                        }
                    ],
                    "whyNotFalsePositive": ["The focused checkout flow was inspected."],
                    "verificationStatus": "potential_risk",
                }
            ]
        )

        self.assertEqual(findings, [])
        self.assertEqual(rejected_reasons, {"missing_evidence": 1})
        self.assertEqual(rejected_samples[0]["title"], "Source file evidence log path")

    def test_reportability_filter_rejects_generic_evidence_summary(self) -> None:
        findings, rejected_reasons, rejected_samples = filter_reportable_findings(
            [
                {
                    "title": "Generic evidence summary",
                    "evidence": [{"summary": "Concrete evidence.", "file": "src/app.py", "startLine": 12}],
                    "limitations": ["False-positive check: Confirm no upstream guard exists."],
                    "verificationStatus": "potential_risk",
                }
            ]
        )

        self.assertEqual(findings, [])
        self.assertEqual(rejected_reasons, {"missing_evidence": 1})
        self.assertEqual(rejected_samples[0]["title"], "Generic evidence summary")

    def test_reportability_filter_rejects_vacuous_false_positive_check(self) -> None:
        findings, rejected_reasons, rejected_samples = filter_reportable_findings(
            [
                {
                    "title": "Vacuous false positive check",
                    "evidence": [
                        {
                            "summary": "The checkout regression is covered by the payment test.",
                            "command": "pytest tests/test_checkout.py",
                        }
                    ],
                    "whyNotFalsePositive": ["N/A"],
                    "verificationStatus": "potential_risk",
                }
            ]
        )

        self.assertEqual(findings, [])
        self.assertEqual(rejected_reasons, {"missing_false_positive_check": 1})
        self.assertEqual(rejected_samples[0]["title"], "Vacuous false positive check")

    def test_reportability_filter_rejects_punctuated_vacuous_false_positive_check(self) -> None:
        findings, rejected_reasons, rejected_samples = filter_reportable_findings(
            [
                {
                    "title": "Punctuated vacuous false positive check",
                    "evidence": [
                        {
                            "summary": "The checkout regression is covered by the payment test.",
                            "command": "pytest tests/test_checkout.py",
                        }
                    ],
                    "whyNotFalsePositive": ["N/A."],
                    "verificationStatus": "potential_risk",
                }
            ]
        )

        self.assertEqual(findings, [])
        self.assertEqual(rejected_reasons, {"missing_false_positive_check": 1})
        self.assertEqual(rejected_samples[0]["title"], "Punctuated vacuous false positive check")

    def test_convergence_gate_marks_missing_previous_finding_resolved(self) -> None:
        checkout_dir = Path(tempfile.mkdtemp())
        previous_finding = {
            "fingerprint": "fp-old",
            "issue_id": "issue-old",
            "title": "Old bug",
            "file": "src/app.py",
            "line": 12,
            "confidence": 0.92,
            "source": "correctness-reviewer",
            "status": "open",
        }
        job = {
            "repo": "acme/api",
            "branch": "main",
            "commit": "b" * 40,
            "convergence_context": {
                "protocol": "pullwise-convergence/0.1",
                "previous_head_sha": "a" * 40,
                "open_findings": [previous_finding],
                "source_stats": {},
            },
        }

        reported, rejected_reasons, rejected_samples, state = worker_main.apply_convergence_gate(job, checkout_dir, [])

        self.assertEqual(reported, [])
        self.assertEqual(rejected_reasons, {})
        self.assertEqual(rejected_samples, [])
        self.assertEqual(state["resolved_fingerprints"], ["fp-old"])
        self.assertEqual(state["open_findings"], [])
        self.assertEqual(state["head_sha"], "b" * 40)

    def test_convergence_gate_accepts_context_for_matching_scope(self) -> None:
        checkout_dir = Path(tempfile.mkdtemp())
        job = {
            "repo": "acme/api",
            "branch": "main",
            "commit": "b" * 40,
            "convergence_context": {
                "protocol": "pullwise-convergence/0.1",
                "scope_key": "repo:acme/api|branch:main",
                "previous_head_sha": "a" * 40,
                "open_findings": [
                    {
                        "fingerprint": "fp-matching-scope",
                        "title": "Matching scope bug",
                        "file": "src/app.py",
                        "source": "correctness-reviewer",
                    }
                ],
                "source_stats": {},
            },
        }

        _reported, _rejected_reasons, _samples, state = worker_main.apply_convergence_gate(job, checkout_dir, [])

        self.assertEqual(state["resolved_fingerprints"], ["fp-matching-scope"])

    def test_convergence_gate_ignores_context_for_different_scope(self) -> None:
        checkout_dir = Path(tempfile.mkdtemp())
        job = {
            "repo": "acme/api",
            "branch": "main",
            "commit": "b" * 40,
            "convergence_context": {
                "protocol": "pullwise-convergence/0.1",
                "scope_key": "repo:acme/other|branch:main",
                "previous_head_sha": "a" * 40,
                "open_findings": [
                    {
                        "fingerprint": "fp-other-repo",
                        "title": "Other repo bug",
                        "file": "src/app.py",
                        "source": "correctness-reviewer",
                    }
                ],
                "source_stats": {"correctness-reviewer": {"reported": 0, "confirmed": 0, "resolved": 0, "rejected": 50}},
            },
        }

        reported, rejected_reasons, _samples, state = worker_main.apply_convergence_gate(job, checkout_dir, [])

        self.assertEqual(reported, [])
        self.assertEqual(rejected_reasons, {})
        self.assertEqual(state["resolved_fingerprints"], [])
        self.assertEqual(state["source_stats"], {})

    def test_convergence_gate_rejects_unproven_new_finding_after_prior_run(self) -> None:
        checkout_dir = Path(tempfile.mkdtemp())
        finding = (audit_swarm_findings_from_payload(audit_payload([issue_card("Latent old issue")])) or [])[0]
        finding["confidence"] = 0.9
        job = {
            "repo": "acme/api",
            "branch": "main",
            "commit": "b" * 40,
            "convergence_context": {
                "protocol": "pullwise-convergence/0.1",
                "previous_head_sha": "a" * 40,
                "open_findings": [],
                "source_stats": {},
            },
        }

        with patch("pullwise_worker.main.changed_files_between_heads", return_value=None):
            reported, rejected_reasons, rejected_samples, state = worker_main.apply_convergence_gate(
                job,
                checkout_dir,
                [finding],
            )

        self.assertEqual(reported, [])
        self.assertEqual(rejected_reasons, {"not_introduced_by_current_delta": 1})
        self.assertEqual(rejected_samples[0]["title"], "Latent old issue")
        self.assertEqual(state["open_findings"], [])

    def test_changed_files_fetches_previous_head_when_shallow_checkout_lacks_it(self) -> None:
        checkout_dir = Path(tempfile.mkdtemp())
        previous = "a" * 40
        current = "b" * 40
        missing_previous = subprocess.CalledProcessError(128, ["git", "diff"], stderr="unknown revision")
        fetch_ok = Mock(returncode=0, stdout="", stderr="")
        diff_ok = Mock(returncode=0, stdout="src/app.py\nREADME.md\n", stderr="")

        with patch("pullwise_worker.main.subprocess.run", side_effect=[missing_previous, fetch_ok, diff_ok]) as run:
            changed = worker_main.changed_files_between_heads(
                checkout_dir,
                previous,
                current,
                job={
                    "repo": "acme/api",
                    "clone_url": "https://github.com/acme/api.git",
                    "clone_token": {"token": "repo-token", "repo": "acme/api"},
                },
            )

        self.assertEqual(changed, {"src/app.py", "README.md"})
        self.assertEqual(run.call_args_list[0].args[0], ["git", "-C", str(checkout_dir), "diff", "--name-only", f"{previous}..{current}"])
        self.assertEqual(run.call_args_list[1].args[0], ["git", "-C", str(checkout_dir), "fetch", "--depth", "1", "origin", previous])
        self.assertNotIn("repo-token", run.call_args_list[1].args[0])
        self.assertIn("Authorization: Basic", run.call_args_list[1].kwargs["env"]["GIT_CONFIG_VALUE_0"])
        self.assertEqual(
            run.call_args_list[1].kwargs["env"]["GIT_CONFIG_KEY_0"],
            "http.https://github.com/acme/api.git.extraHeader",
        )
        self.assertEqual(run.call_args_list[2].args[0], ["git", "-C", str(checkout_dir), "diff", "--name-only", f"{previous}..{current}"])

    def test_convergence_gate_allows_new_finding_when_delta_touches_file(self) -> None:
        checkout_dir = Path(tempfile.mkdtemp())
        (checkout_dir / "src").mkdir(parents=True)
        (checkout_dir / "src" / "app.py").write_text("".join(f"line {index}\n" for index in range(1, 20)), encoding="utf-8")
        finding = (audit_swarm_findings_from_payload(audit_payload([issue_card("Fix introduced bug")])) or [])[0]
        finding["confidence"] = 0.9
        job = {
            "repo": "acme/api",
            "branch": "main",
            "commit": "b" * 40,
            "convergence_context": {
                "protocol": "pullwise-convergence/0.1",
                "previous_head_sha": "a" * 40,
                "open_findings": [],
                "source_stats": {},
            },
        }

        with patch("pullwise_worker.main.changed_files_between_heads", return_value={"src/app.py"}), \
            patch("pullwise_worker.main.changed_line_ranges_between_heads", return_value={"src/app.py": [(10, 14)]}):
            reported, rejected_reasons, rejected_samples, state = worker_main.apply_convergence_gate(
                job,
                checkout_dir,
                [finding],
            )

        self.assertEqual([item["title"] for item in reported], ["Fix introduced bug"])
        self.assertEqual(rejected_reasons, {})
        self.assertEqual(rejected_samples, [])
        self.assertEqual(state["open_findings"][0]["title"], "Fix introduced bug")

    def test_convergence_gate_rejects_new_finding_when_location_is_stale(self) -> None:
        checkout_dir = Path(tempfile.mkdtemp())
        (checkout_dir / "src").mkdir(parents=True)
        (checkout_dir / "src" / "app.py").write_text("line 1\nline 2\nline 3\n", encoding="utf-8")
        finding = (audit_swarm_findings_from_payload(audit_payload([issue_card("New stale line bug", line=12)])) or [])[0]
        finding["confidence"] = 0.95
        job = {
            "repo": "acme/api",
            "branch": "main",
            "commit": "b" * 40,
            "convergence_context": {
                "protocol": "pullwise-convergence/0.1",
                "scope_key": "repo:acme/api|branch:main",
                "previous_head_sha": "a" * 40,
                "open_findings": [],
                "source_stats": {
                    "correctness-reviewer": {"reported": 4, "confirmed": 4, "resolved": 0, "rejected": 0}
                },
            },
        }

        with patch("pullwise_worker.main.changed_files_between_heads", return_value={"src/app.py"}), \
            patch("pullwise_worker.main.changed_line_ranges_between_heads", return_value={"src/app.py": [(10, 14)]}):
            reported, rejected_reasons, rejected_samples, state = worker_main.apply_convergence_gate(
                job,
                checkout_dir,
                [finding],
            )

        self.assertEqual(reported, [])
        self.assertEqual(rejected_reasons, {"invalid_candidate_location": 1})
        self.assertEqual(rejected_samples[0]["title"], "New stale line bug")
        self.assertEqual(state["open_findings"], [])

    def test_convergence_gate_rejects_new_finding_outside_changed_hunks(self) -> None:
        checkout_dir = Path(tempfile.mkdtemp())
        (checkout_dir / "src").mkdir(parents=True)
        (checkout_dir / "src" / "app.py").write_text("".join(f"line {index}\n" for index in range(1, 20)), encoding="utf-8")
        finding = (audit_swarm_findings_from_payload(audit_payload([issue_card("Same file latent bug", line=12)])) or [])[0]
        finding["confidence"] = 0.95
        job = {
            "repo": "acme/api",
            "branch": "main",
            "commit": "b" * 40,
            "convergence_context": {
                "protocol": "pullwise-convergence/0.1",
                "scope_key": "repo:acme/api|branch:main",
                "previous_head_sha": "a" * 40,
                "open_findings": [],
                "source_stats": {
                    "correctness-reviewer": {"reported": 4, "confirmed": 4, "resolved": 0, "rejected": 0}
                },
            },
        }

        with patch("pullwise_worker.main.changed_files_between_heads", return_value={"src/app.py"}), \
            patch("pullwise_worker.main.changed_line_ranges_between_heads", return_value={"src/app.py": [(1, 3)]}):
            reported, rejected_reasons, rejected_samples, state = worker_main.apply_convergence_gate(
                job,
                checkout_dir,
                [finding],
            )

        self.assertEqual(reported, [])
        self.assertEqual(rejected_reasons, {"not_introduced_by_current_delta": 1})
        self.assertEqual(rejected_samples[0]["title"], "Same file latent bug")
        self.assertEqual(state["open_findings"], [])

    def test_convergence_gate_rejects_when_primary_location_is_outside_changed_hunks(self) -> None:
        checkout_dir = Path(tempfile.mkdtemp())
        (checkout_dir / "src").mkdir(parents=True)
        (checkout_dir / "src" / "app.py").write_text("".join(f"line {index}\n" for index in range(1, 20)), encoding="utf-8")
        finding = {
            "id": "multi-location",
            "title": "Mixed hunk latent bug",
            "file": "src/app.py",
            "line": 12,
            "confidence": 0.95,
            "_auditSwarmRole": "correctness-reviewer",
            "affectedLocations": [
                {"file": "src/app.py", "startLine": 12, "endLine": 12},
                {"file": "src/app.py", "startLine": 2, "endLine": 2},
            ],
        }
        job = {
            "repo": "acme/api",
            "branch": "main",
            "commit": "b" * 40,
            "convergence_context": {
                "protocol": "pullwise-convergence/0.1",
                "scope_key": "repo:acme/api|branch:main",
                "previous_head_sha": "a" * 40,
                "open_findings": [],
                "source_stats": {
                    "correctness-reviewer": {"reported": 4, "confirmed": 4, "resolved": 0, "rejected": 0}
                },
            },
        }

        with patch("pullwise_worker.main.changed_files_between_heads", return_value={"src/app.py"}), \
            patch("pullwise_worker.main.changed_line_ranges_between_heads", return_value={"src/app.py": [(1, 3)]}):
            reported, rejected_reasons, rejected_samples, state = worker_main.apply_convergence_gate(
                job,
                checkout_dir,
                [finding],
            )

        self.assertEqual(reported, [])
        self.assertEqual(rejected_reasons, {"not_introduced_by_current_delta": 1})
        self.assertEqual(rejected_samples[0]["title"], "Mixed hunk latent bug")
        self.assertEqual(state["open_findings"], [])

    def test_convergence_gate_allows_changed_primary_location_with_unchanged_support_evidence(self) -> None:
        checkout_dir = Path(tempfile.mkdtemp())
        (checkout_dir / "README.md").write_text("# App\nRun `npm run dev`\n", encoding="utf-8")
        (checkout_dir / "package.json").write_text('{"scripts":{"build":"vite build"}}\n', encoding="utf-8")
        finding = {
            "id": "readme-missing-script",
            "title": "README references missing package script",
            "file": "README.md",
            "line": 2,
            "confidence": 0.95,
            "_auditSwarmRole": "correctness-reviewer",
            "verificationStatus": "static_proof",
            "affectedLocations": [
                {"file": "README.md", "startLine": 2, "endLine": 2},
                {"file": "package.json", "startLine": 1, "endLine": 1},
            ],
            "evidence": [
                {"summary": "README documents `npm run dev`.", "file": "README.md", "startLine": 2},
                {"summary": "package.json does not define `dev`.", "file": "package.json", "startLine": 1},
            ],
        }
        job = {
            "repo": "acme/api",
            "branch": "main",
            "commit": "b" * 40,
            "convergence_context": {
                "protocol": "pullwise-convergence/0.1",
                "scope_key": "repo:acme/api|branch:main",
                "previous_head_sha": "a" * 40,
                "open_findings": [],
                "source_stats": {
                    "correctness-reviewer": {"reported": 4, "confirmed": 4, "resolved": 0, "rejected": 0}
                },
            },
        }

        with patch("pullwise_worker.main.changed_files_between_heads", return_value={"README.md"}), \
            patch("pullwise_worker.main.changed_line_ranges_between_heads", return_value={"README.md": [(2, 2)]}):
            reported, rejected_reasons, rejected_samples, state = worker_main.apply_convergence_gate(
                job,
                checkout_dir,
                [finding],
            )

        self.assertEqual([item["title"] for item in reported], ["README references missing package script"])
        self.assertEqual(rejected_reasons, {})
        self.assertEqual(rejected_samples, [])
        self.assertEqual(state["open_findings"][0]["title"], "README references missing package script")

    def test_convergence_gate_rejects_line_finding_when_changed_hunks_are_unknown(self) -> None:
        checkout_dir = Path(tempfile.mkdtemp())
        (checkout_dir / "src").mkdir(parents=True)
        (checkout_dir / "src" / "app.py").write_text("".join(f"line {index}\n" for index in range(1, 20)), encoding="utf-8")
        finding = (audit_swarm_findings_from_payload(audit_payload([issue_card("Unknown hunk latent bug", line=12)])) or [])[0]
        finding["confidence"] = 0.95
        job = {
            "repo": "acme/api",
            "branch": "main",
            "commit": "b" * 40,
            "convergence_context": {
                "protocol": "pullwise-convergence/0.1",
                "scope_key": "repo:acme/api|branch:main",
                "previous_head_sha": "a" * 40,
                "open_findings": [],
                "source_stats": {
                    "correctness-reviewer": {"reported": 4, "confirmed": 4, "resolved": 0, "rejected": 0}
                },
            },
        }

        with patch("pullwise_worker.main.changed_files_between_heads", return_value={"src/app.py"}), \
            patch("pullwise_worker.main.changed_line_ranges_between_heads", return_value=None):
            reported, rejected_reasons, rejected_samples, state = worker_main.apply_convergence_gate(
                job,
                checkout_dir,
                [finding],
            )

        self.assertEqual(reported, [])
        self.assertEqual(rejected_reasons, {"not_introduced_by_current_delta": 1})
        self.assertEqual(rejected_samples[0]["title"], "Unknown hunk latent bug")
        self.assertEqual(state["open_findings"], [])

    def test_convergence_gate_rejects_incremental_file_finding_without_line(self) -> None:
        checkout_dir = Path(tempfile.mkdtemp())
        (checkout_dir / "src").mkdir(parents=True)
        (checkout_dir / "src" / "app.py").write_text("".join(f"line {index}\n" for index in range(1, 20)), encoding="utf-8")
        finding = {
            "id": "file-only",
            "title": "File-only latent bug",
            "file": "src/app.py",
            "confidence": 0.95,
            "_auditSwarmRole": "correctness-reviewer",
            "evidence": [
                {
                    "summary": "The checkout regression is reproduced by the focused test.",
                    "file": "src/app.py",
                    "command": "pytest tests/test_checkout.py",
                }
            ],
            "verificationStatus": "potential_risk",
        }
        job = {
            "repo": "acme/api",
            "branch": "main",
            "commit": "b" * 40,
            "convergence_context": {
                "protocol": "pullwise-convergence/0.1",
                "scope_key": "repo:acme/api|branch:main",
                "previous_head_sha": "a" * 40,
                "open_findings": [],
                "source_stats": {
                    "correctness-reviewer": {"reported": 4, "confirmed": 4, "resolved": 0, "rejected": 0}
                },
            },
        }

        with patch("pullwise_worker.main.changed_files_between_heads", return_value={"src/app.py"}), \
            patch("pullwise_worker.main.changed_line_ranges_between_heads", return_value={"src/app.py": [(1, 3)]}):
            reported, rejected_reasons, rejected_samples, state = worker_main.apply_convergence_gate(
                job,
                checkout_dir,
                [finding],
            )

        self.assertEqual(reported, [])
        self.assertEqual(rejected_reasons, {"not_introduced_by_current_delta": 1})
        self.assertEqual(rejected_samples[0]["title"], "File-only latent bug")
        self.assertEqual(state["open_findings"], [])

    def test_convergence_gate_rejects_line_finding_when_changed_file_has_no_hunk(self) -> None:
        checkout_dir = Path(tempfile.mkdtemp())
        (checkout_dir / "src").mkdir(parents=True)
        (checkout_dir / "src" / "app.py").write_text("".join(f"line {index}\n" for index in range(1, 20)), encoding="utf-8")
        finding = (audit_swarm_findings_from_payload(audit_payload([issue_card("Metadata-only latent bug", line=12)])) or [])[0]
        finding["confidence"] = 0.95
        job = {
            "repo": "acme/api",
            "branch": "main",
            "commit": "b" * 40,
            "convergence_context": {
                "protocol": "pullwise-convergence/0.1",
                "scope_key": "repo:acme/api|branch:main",
                "previous_head_sha": "a" * 40,
                "open_findings": [],
                "source_stats": {
                    "correctness-reviewer": {"reported": 4, "confirmed": 4, "resolved": 0, "rejected": 0}
                },
            },
        }

        with patch("pullwise_worker.main.changed_files_between_heads", return_value={"src/app.py"}), \
            patch("pullwise_worker.main.changed_line_ranges_between_heads", return_value={"src/other.py": [(1, 3)]}):
            reported, rejected_reasons, rejected_samples, state = worker_main.apply_convergence_gate(
                job,
                checkout_dir,
                [finding],
            )

        self.assertEqual(reported, [])
        self.assertEqual(rejected_reasons, {"not_introduced_by_current_delta": 1})
        self.assertEqual(rejected_samples[0]["title"], "Metadata-only latent bug")
        self.assertEqual(state["open_findings"], [])

    def test_convergence_gate_resolves_previous_finding_when_location_deleted(self) -> None:
        checkout_dir = Path(tempfile.mkdtemp())
        stale_finding = (
            audit_swarm_findings_from_payload(
                audit_payload([issue_card("Deleted file bug", issue_id="issue-deleted", file="src/deleted.py")])
            )
            or []
        )[0]
        old_fingerprint = worker_main.finding_fingerprint(stale_finding)
        stale_finding["confidence"] = 0.95
        job = {
            "repo": "acme/api",
            "branch": "main",
            "commit": "b" * 40,
            "convergence_context": {
                "protocol": "pullwise-convergence/0.1",
                "previous_head_sha": "a" * 40,
                "open_findings": [
                    {
                        "fingerprint": old_fingerprint,
                        "issue_id": "issue-deleted",
                        "title": "Deleted file bug",
                        "file": "src/deleted.py",
                        "line": 12,
                        "source": "correctness-reviewer",
                    }
                ],
                "source_stats": {},
            },
        }

        reported, rejected_reasons, rejected_samples, state = worker_main.apply_convergence_gate(
            job,
            checkout_dir,
            [stale_finding],
        )

        self.assertEqual(reported, [])
        self.assertEqual(rejected_reasons, {"stale_previous_location": 1})
        self.assertEqual(rejected_samples[0]["title"], "Deleted file bug")
        self.assertEqual(state["resolved_fingerprints"], [old_fingerprint])
        self.assertEqual(state["open_findings"], [])

    def test_convergence_gate_resolves_previous_finding_when_line_is_stale(self) -> None:
        checkout_dir = Path(tempfile.mkdtemp())
        (checkout_dir / "src").mkdir(parents=True)
        (checkout_dir / "src" / "app.py").write_text("line 1\nline 2\nline 3\n", encoding="utf-8")
        stale_finding = (
            audit_swarm_findings_from_payload(
                audit_payload([issue_card("Removed block bug", issue_id="issue-stale-line", line=12)])
            )
            or []
        )[0]
        old_fingerprint = worker_main.finding_fingerprint(stale_finding)
        stale_finding["confidence"] = 0.95
        job = {
            "repo": "acme/api",
            "branch": "main",
            "commit": "b" * 40,
            "convergence_context": {
                "protocol": "pullwise-convergence/0.1",
                "previous_head_sha": "a" * 40,
                "open_findings": [
                    {
                        "fingerprint": old_fingerprint,
                        "issue_id": "issue-stale-line",
                        "title": "Removed block bug",
                        "file": "src/app.py",
                        "line": 12,
                        "source": "correctness-reviewer",
                    }
                ],
                "source_stats": {},
            },
        }

        reported, rejected_reasons, _samples, state = worker_main.apply_convergence_gate(
            job,
            checkout_dir,
            [stale_finding],
        )

        self.assertEqual(reported, [])
        self.assertEqual(rejected_reasons, {"stale_previous_location": 1})
        self.assertEqual(state["resolved_fingerprints"], [old_fingerprint])

    def test_convergence_gate_resolves_previous_finding_when_secondary_location_is_stale(self) -> None:
        checkout_dir = Path(tempfile.mkdtemp())
        (checkout_dir / "src").mkdir(parents=True)
        (checkout_dir / "src" / "app.py").write_text("line 1\nline 2\nline 3\n", encoding="utf-8")
        stale_finding = {
            "id": "issue-stale-secondary",
            "title": "Secondary location removed bug",
            "file": "src/app.py",
            "line": 2,
            "confidence": 0.95,
            "verificationStatus": "potential_risk",
            "affectedLocations": [
                {"file": "src/app.py", "startLine": 2, "endLine": 2},
                {"file": "src/app.py", "startLine": 12, "endLine": 12},
            ],
            "_auditSwarmRole": "correctness-reviewer",
        }
        job = {
            "repo": "acme/api",
            "branch": "main",
            "commit": "b" * 40,
            "convergence_context": {
                "protocol": "pullwise-convergence/0.1",
                "previous_head_sha": "a" * 40,
                "open_findings": [
                    {
                        "fingerprint": "fp-stale-secondary",
                        "issue_id": "issue-stale-secondary",
                        "title": "Secondary location removed bug",
                        "file": "src/app.py",
                        "line": 2,
                        "source": "correctness-reviewer",
                    }
                ],
                "source_stats": {},
            },
        }

        reported, rejected_reasons, _samples, state = worker_main.apply_convergence_gate(
            job,
            checkout_dir,
            [stale_finding],
        )

        self.assertEqual(reported, [])
        self.assertEqual(rejected_reasons, {"stale_previous_location": 1})
        self.assertEqual(state["resolved_fingerprints"], ["fp-stale-secondary"])

    def test_convergence_gate_uses_prior_location_when_repeated_finding_omits_file(self) -> None:
        checkout_dir = Path(tempfile.mkdtemp())
        repeated_without_location = {
            "id": "issue-omitted-location",
            "title": "Omitted location bug",
            "confidence": 0.95,
            "verificationStatus": "potential_risk",
            "reproduction": {"commands": ["pytest tests/test_app.py"]},
            "_auditSwarmRole": "correctness-reviewer",
        }
        job = {
            "repo": "acme/api",
            "branch": "main",
            "commit": "b" * 40,
            "convergence_context": {
                "protocol": "pullwise-convergence/0.1",
                "previous_head_sha": "a" * 40,
                "open_findings": [
                    {
                        "fingerprint": "fp-omitted-location",
                        "issue_id": "issue-omitted-location",
                        "title": "Omitted location bug",
                        "file": "src/deleted.py",
                        "line": 12,
                        "source": "correctness-reviewer",
                    }
                ],
                "source_stats": {},
            },
        }

        reported, rejected_reasons, _samples, state = worker_main.apply_convergence_gate(
            job,
            checkout_dir,
            [repeated_without_location],
        )

        self.assertEqual(reported, [])
        self.assertEqual(rejected_reasons, {"stale_previous_location": 1})
        self.assertEqual(state["resolved_fingerprints"], ["fp-omitted-location"])

    def test_convergence_gate_keeps_same_finding_when_line_shifts(self) -> None:
        checkout_dir = Path(tempfile.mkdtemp())
        (checkout_dir / "src").mkdir(parents=True)
        (checkout_dir / "src" / "app.py").write_text("".join(f"line {index}\n" for index in range(1, 26)), encoding="utf-8")
        old_finding = (audit_swarm_findings_from_payload(audit_payload([issue_card("Line shifted bug", line=12)])) or [])[0]
        old_fingerprint = worker_main.finding_fingerprint(old_finding)
        shifted_finding = (audit_swarm_findings_from_payload(audit_payload([issue_card("Line shifted bug", line=20)])) or [])[0]
        job = {
            "repo": "acme/api",
            "branch": "main",
            "commit": "b" * 40,
            "convergence_context": {
                "protocol": "pullwise-convergence/0.1",
                "previous_head_sha": "a" * 40,
                "open_findings": [
                    {
                        "fingerprint": old_fingerprint,
                        "title": "Line shifted bug",
                        "file": "src/app.py",
                        "line": 12,
                        "source": "correctness-reviewer",
                    }
                ],
                "source_stats": {},
            },
        }

        with patch("pullwise_worker.main.changed_files_between_heads", return_value=None):
            reported, rejected_reasons, _samples, state = worker_main.apply_convergence_gate(
                job,
                checkout_dir,
                [shifted_finding],
            )

        self.assertEqual([item["line"] for item in reported], [20])
        self.assertEqual(rejected_reasons, {})
        self.assertEqual(state["resolved_fingerprints"], [])

    def test_convergence_gate_matches_same_issue_id_when_fingerprint_drifts(self) -> None:
        checkout_dir = Path(tempfile.mkdtemp())
        (checkout_dir / "src").mkdir(parents=True)
        (checkout_dir / "src" / "app.py").write_text("".join(f"line {index}\n" for index in range(1, 26)), encoding="utf-8")
        old_finding = (audit_swarm_findings_from_payload(audit_payload([issue_card("Original title", issue_id="issue-stable")])) or [])[0]
        old_fingerprint = worker_main.finding_fingerprint(old_finding)
        rewritten_finding = (
            audit_swarm_findings_from_payload(audit_payload([issue_card("Rewritten title", issue_id="issue-stable")]))
            or []
        )[0]
        rewritten_finding["confidence"] = 0.9
        job = {
            "repo": "acme/api",
            "branch": "main",
            "commit": "b" * 40,
            "convergence_context": {
                "protocol": "pullwise-convergence/0.1",
                "scope_key": "repo:acme/api|branch:main",
                "previous_head_sha": "a" * 40,
                "open_findings": [
                    {
                        "fingerprint": old_fingerprint,
                        "issue_id": "issue-stable",
                        "title": "Original title",
                        "file": "src/app.py",
                        "line": 12,
                        "source": "correctness-reviewer",
                    }
                ],
                "source_stats": {},
            },
        }

        with patch("pullwise_worker.main.changed_files_between_heads", return_value=None):
            reported, rejected_reasons, _samples, state = worker_main.apply_convergence_gate(
                job,
                checkout_dir,
                [rewritten_finding],
            )

        self.assertEqual([item["title"] for item in reported], ["Rewritten title"])
        self.assertEqual(rejected_reasons, {})
        self.assertEqual(state["resolved_fingerprints"], [])
        self.assertEqual(state["open_findings"][0]["fingerprint"], old_fingerprint)

    def test_convergence_gate_rejects_low_confidence_unverified_candidate(self) -> None:
        checkout_dir = Path(tempfile.mkdtemp())
        finding = (audit_swarm_findings_from_payload(audit_payload([issue_card("Default confidence guess")])) or [])[0]
        finding["confidence"] = 0.7

        reported, rejected_reasons, rejected_samples, state = worker_main.apply_convergence_gate(
            {"repo": "acme/api", "branch": "main", "commit": "a" * 40},
            checkout_dir,
            [finding],
        )

        self.assertEqual(reported, [])
        self.assertEqual(rejected_reasons, {"low_statistical_confidence": 1})
        self.assertEqual(rejected_samples[0]["title"], "Default confidence guess")
        self.assertEqual(state["open_findings"], [])

    def test_convergence_gate_rejects_default_confidence_unverified_candidate(self) -> None:
        checkout_dir = Path(tempfile.mkdtemp())
        finding = (audit_swarm_findings_from_payload(audit_payload([issue_card("Unverified default guess")])) or [])[0]

        reported, rejected_reasons, rejected_samples, state = worker_main.apply_convergence_gate(
            {"repo": "acme/api", "branch": "main", "commit": "a" * 40},
            checkout_dir,
            [finding],
        )

        self.assertEqual(reported, [])
        self.assertEqual(rejected_reasons, {"low_statistical_confidence": 1})
        self.assertEqual(rejected_samples[0]["title"], "Unverified default guess")
        self.assertEqual(state["open_findings"], [])

    def test_convergence_gate_penalizes_small_sample_source_with_equal_rejections(self) -> None:
        checkout_dir = Path(tempfile.mkdtemp())
        (checkout_dir / "src").mkdir(parents=True)
        (checkout_dir / "src" / "app.py").write_text("".join(f"line {index}\n" for index in range(1, 20)), encoding="utf-8")
        finding = (audit_swarm_findings_from_payload(audit_payload([issue_card("Early mixed source bug")])) or [])[0]
        finding["_auditSwarmRole"] = "correctness-reviewer"
        finding["confidence"] = 0.9
        job = {
            "repo": "acme/api",
            "branch": "main",
            "commit": "b" * 40,
            "convergence_context": {
                "protocol": "pullwise-convergence/0.1",
                "scope_key": "repo:acme/api|branch:main",
                "previous_head_sha": "a" * 40,
                "source_stats": {
                    "correctness-reviewer": {"reported": 2, "confirmed": 1, "resolved": 0, "rejected": 1}
                },
            },
        }

        with patch("pullwise_worker.main.changed_files_between_heads", return_value={"src/app.py"}), \
            patch("pullwise_worker.main.changed_line_ranges_between_heads", return_value={"src/app.py": [(1, 20)]}):
            reported, rejected_reasons, rejected_samples, state = worker_main.apply_convergence_gate(
                job,
                checkout_dir,
                [finding],
            )

        self.assertEqual(reported, [])
        self.assertEqual(rejected_reasons, {"low_statistical_confidence": 1})
        self.assertEqual(rejected_samples[0]["title"], "Early mixed source bug")
        self.assertEqual(state["open_findings"], [])

    def test_convergence_gate_suppresses_same_run_duplicate_fingerprints(self) -> None:
        checkout_dir = Path(tempfile.mkdtemp())
        first = (audit_swarm_findings_from_payload(audit_payload([issue_card("Duplicate bug", issue_id="dup-1")])) or [])[0]
        second = (audit_swarm_findings_from_payload(audit_payload([issue_card("Duplicate bug", issue_id="dup-2")])) or [])[0]
        first["confidence"] = 0.9
        second["confidence"] = 0.9

        reported, rejected_reasons, rejected_samples, state = worker_main.apply_convergence_gate(
            {"repo": "acme/api", "branch": "main", "commit": "a" * 40},
            checkout_dir,
            [first, second],
        )

        self.assertEqual([item["id"] for item in reported], ["dup-1"])
        self.assertEqual(rejected_reasons, {"duplicate_finding": 1})
        self.assertEqual(rejected_samples[0]["title"], "Duplicate bug")
        self.assertEqual(len(state["open_findings"]), 1)

    def test_statistical_confidence_penalizes_repeatedly_rejected_source(self) -> None:
        finding = (audit_swarm_findings_from_payload(audit_payload([issue_card("Noisy source bug")])) or [])[0]
        finding["_auditSwarmRole"] = "noisy-reviewer"

        confidence = worker_main.statistically_calibrated_confidence(
            finding,
            {"reported": 0, "confirmed": 0, "resolved": 0, "rejected": 50},
        )

        self.assertLess(confidence, 0.7)

    def test_statistical_confidence_does_not_penalize_confirmed_source(self) -> None:
        finding = (audit_swarm_findings_from_payload(audit_payload([issue_card("Trusted source bug")])) or [])[0]

        confidence = worker_main.statistically_calibrated_confidence(
            finding,
            {"reported": 1, "confirmed": 1, "resolved": 0, "rejected": 0},
        )

        self.assertGreaterEqual(confidence, 0.75)

    def test_statistical_confidence_does_not_treat_resolved_only_history_as_confirmation(self) -> None:
        finding = (audit_swarm_findings_from_payload(audit_payload([issue_card("Resolved-only source bug")])) or [])[0]
        finding["confidence"] = 0.95

        confidence = worker_main.statistically_calibrated_confidence(
            finding,
            {"reported": 20, "confirmed": 0, "resolved": 20, "rejected": 0},
        )

        self.assertLess(confidence, worker_main.CONVERGENCE_MIN_UNVERIFIED_CONFIDENCE)

    def test_statistical_confidence_uses_conservative_source_reliability_bound(self) -> None:
        finding = (audit_swarm_findings_from_payload(audit_payload([issue_card("Source reliability bug")])) or [])[0]
        finding["confidence"] = 0.9

        strong_source = worker_main.statistically_calibrated_confidence(
            finding,
            {"reported": 10, "confirmed": 9, "resolved": 0, "rejected": 1},
        )
        mixed_source = worker_main.statistically_calibrated_confidence(
            finding,
            {"reported": 10, "confirmed": 5, "resolved": 0, "rejected": 5},
        )

        self.assertGreater(strong_source, 0.75)
        self.assertLess(mixed_source, 0.65)
        self.assertGreater(strong_source, mixed_source)

    def test_convergence_gate_applies_source_stats_across_separator_variants(self) -> None:
        checkout_dir = Path(tempfile.mkdtemp())
        finding = (audit_swarm_findings_from_payload(audit_payload([issue_card("Separator alias bug")])) or [])[0]
        finding["_auditSwarmRole"] = "correctness-reviewer"
        finding["confidence"] = 0.9
        job = {
            "repo": "acme/api",
            "branch": "main",
            "commit": "b" * 40,
            "convergence_context": {
                "protocol": "pullwise-convergence/0.1",
                "scope_key": "repo:acme/api|branch:main",
                "source_stats": {
                    "correctness_reviewer": {"reported": 0, "confirmed": 0, "resolved": 0, "rejected": 50}
                },
            },
        }

        reported, rejected_reasons, _samples, _state = worker_main.apply_convergence_gate(job, checkout_dir, [finding])

        self.assertEqual(reported, [])
        self.assertEqual(rejected_reasons, {"low_statistical_confidence": 1})

    def test_convergence_gate_rejects_unknown_unverified_source_after_prior_run(self) -> None:
        checkout_dir = Path(tempfile.mkdtemp())
        (checkout_dir / "src").mkdir(parents=True)
        (checkout_dir / "src" / "app.py").write_text("".join(f"line {index}\n" for index in range(1, 20)), encoding="utf-8")
        finding = (audit_swarm_findings_from_payload(audit_payload([issue_card("Unexpected source bug")])) or [])[0]
        finding["_auditSwarmRole"] = "surprise-reviewer"
        finding["confidence"] = 0.95
        job = {
            "repo": "acme/api",
            "branch": "main",
            "commit": "b" * 40,
            "convergence_context": {
                "protocol": "pullwise-convergence/0.1",
                "scope_key": "repo:acme/api|branch:main",
                "previous_head_sha": "a" * 40,
                "open_findings": [],
                "source_stats": {
                    "correctness-reviewer": {"reported": 3, "confirmed": 3, "resolved": 0, "rejected": 0}
                },
            },
        }

        with patch("pullwise_worker.main.changed_files_between_heads", return_value={"src/app.py"}):
            reported, rejected_reasons, rejected_samples, state = worker_main.apply_convergence_gate(
                job,
                checkout_dir,
                [finding],
            )

        self.assertEqual(reported, [])
        self.assertEqual(rejected_reasons, {"unknown_source_after_prior_run": 1})
        self.assertEqual(rejected_samples[0]["title"], "Unexpected source bug")
        self.assertEqual(state["open_findings"], [])

    def test_review_calibration_shadow_builds_events_without_changing_reported_findings(self) -> None:
        checkout_dir = Path(tempfile.mkdtemp())
        finding = (audit_swarm_findings_from_payload(audit_payload([issue_card("Shadow scored bug")])) or [])[0]
        finding["confidence"] = 0.95
        records: list[dict] = []

        reported, rejected_reasons, _samples, _state = worker_main.apply_convergence_gate(
            {"job_id": "job_shadow", "repo": "acme/api", "branch": "main", "commit": "a" * 40},
            checkout_dir,
            [finding],
            records,
        )
        result = worker_main.apply_review_calibration_decisions(
            config(),
            {"job_id": "job_shadow", "repo": "acme/api", "branch": "main", "commit": "a" * 40},
            reported,
            records,
            attempt_id="wk_1-1",
        )

        self.assertEqual(rejected_reasons, {})
        self.assertEqual(result["reported_findings"], reported)
        self.assertNotIn("reviewCalibration", result["reported_findings"][0])
        self.assertEqual(result["audit_only_findings"], [])
        self.assertEqual(len(result["decision_events"]), 1)
        event = result["decision_events"][0]
        self.assertEqual(event["protocol"], "pullwise-review-decision/0.1")
        self.assertEqual(event["decision"], "reported")
        self.assertEqual(event["score_factors"]["scoreKind"], "ranking_score")
        self.assertIn(event["score_factors"]["proposedDecision"], {"reported", "audit_only", "rejected"})
        self.assertEqual(event["base_sha"], "")
        self.assertEqual(event["head_sha"], "a" * 40)
        self.assertEqual(event["score_factors"]["workerVersion"], worker_main.__version__)
        self.assertEqual(event["score_factors"]["auditProtocol"], worker_main.AUDIT_SWARM_PROTOCOL_VERSION)
        self.assertEqual(event["score_factors"]["promptVersion"], "pullwise-review-prompt/0.1")
        self.assertEqual(event["score_factors"]["providerChain"], "codex")
        self.assertIn("decisionScore", event["score_factors"])

    def test_review_calibration_audit_only_moves_unverified_out_of_formal_reporting(self) -> None:
        checkout_dir = Path(tempfile.mkdtemp())
        finding = (audit_swarm_findings_from_payload(audit_payload([issue_card("Unverified high confidence bug")])) or [])[0]
        finding["confidence"] = 0.95
        finding["verificationStatus"] = "unverified"
        records: list[dict] = []
        reported, _rejected_reasons, _samples, _state = worker_main.apply_convergence_gate(
            {"job_id": "job_audit", "repo": "acme/api", "branch": "main", "commit": "a" * 40},
            checkout_dir,
            [finding],
            records,
        )

        with patch.dict(
            os.environ,
            {
                "PULLWISE_REVIEW_CALIBRATION_MODE": "audit_only",
                "PULLWISE_REVIEW_CALIBRATION_SAMPLE_AUDIT_RATE": "1.0",
            },
            clear=False,
        ):
            result = worker_main.apply_review_calibration_decisions(
                config(),
                {"job_id": "job_audit", "repo": "acme/api", "branch": "main", "commit": "a" * 40},
                reported,
                records,
                attempt_id="wk_1-1",
            )

        self.assertEqual(result["reported_findings"], [])
        self.assertEqual([item["title"] for item in result["audit_only_findings"]], ["Unverified high confidence bug"])
        self.assertEqual(result["decision_events"][0]["decision"], "audit_only")
        self.assertTrue(result["audit_only_samples"][0]["sampledForManualReview"])
        self.assertEqual(result["audit_only_samples"][0]["sampleReason"], "calibration_sample_audit_only")
        self.assertEqual(result["audit_only_samples"][0]["sampleRate"], 1.0)
        self.assertEqual(result["verified_suppression_count"], 0)

    def test_review_calibration_server_policy_caps_local_enforce_to_shadow(self) -> None:
        checkout_dir = Path(tempfile.mkdtemp())
        finding = (audit_swarm_findings_from_payload(audit_payload([issue_card("Server gated enforce bug")])) or [])[0]
        finding["confidence"] = 0.95
        finding["verificationStatus"] = "unverified"
        records: list[dict] = []
        reported, _rejected_reasons, _samples, _state = worker_main.apply_convergence_gate(
            {"job_id": "job_policy", "repo": "acme/api", "branch": "main", "commit": "a" * 40},
            checkout_dir,
            [finding],
            records,
        )
        job = {
            "job_id": "job_policy",
            "repo": "acme/api",
            "branch": "main",
            "commit": "a" * 40,
            "review_calibration_context": {
                "protocol": "pullwise-review-calibration/0.2",
                "mode": "shadow",
                "rollout_policy": {
                    "requested_mode": "enforce",
                    "effective_mode": "shadow",
                    "enforce_gate": {"canConsiderEnforce": False},
                },
            },
        }

        with patch.dict(os.environ, {"PULLWISE_REVIEW_CALIBRATION_MODE": "enforce"}, clear=False):
            result = worker_main.apply_review_calibration_decisions(
                config(),
                job,
                reported,
                records,
                attempt_id="wk_1-1",
            )

        self.assertEqual([item["title"] for item in result["reported_findings"]], ["Server gated enforce bug"])
        self.assertEqual(result["audit_only_findings"], [])
        self.assertEqual(result["decision_events"][0]["decision"], "reported")
        self.assertEqual(result["decision_events"][0]["score_factors"]["mode"], "shadow")
        self.assertEqual(result["decision_events"][0]["score_factors"]["proposedDecision"], "audit_only")

    def test_review_calibration_verified_guardrail_ignores_source_history_only_suppression(self) -> None:
        finding = (audit_swarm_findings_from_payload(audit_payload([issue_card("Static proof noisy source bug")])) or [])[0]
        finding["confidence"] = 0.8
        finding["verificationStatus"] = "static_proof"
        records = [
            {
                "stage": "convergence",
                "decision": "reported",
                "reason": "passed_convergence_gate",
                "finding": finding,
                "fingerprint": worker_main.finding_fingerprint(finding),
                "source_stats": {"reported": 0, "confirmed": 0, "resolved": 0, "rejected": 100},
            }
        ]

        with patch.dict(os.environ, {"PULLWISE_REVIEW_CALIBRATION_MODE": "audit_only"}, clear=False):
            result = worker_main.apply_review_calibration_decisions(
                config(),
                {"job_id": "job_guardrail", "repo": "acme/api", "branch": "main", "commit": "a" * 40},
                [finding],
                records,
                attempt_id="wk_1-1",
            )

        self.assertEqual([item["title"] for item in result["reported_findings"]], ["Static proof noisy source bug"])
        public_calibration = result["reported_findings"][0]["reviewCalibration"]
        self.assertEqual(public_calibration["protocol"], "pullwise-review-calibration-public/0.1")
        self.assertEqual(public_calibration["decision"], "reported")
        self.assertEqual(public_calibration["reason"], "verified_or_static_proof_guardrail")
        self.assertIn(public_calibration["scoreBand"], {"report_band", "audit_band", "reject_band"})
        self.assertEqual(public_calibration["scoreKind"], "ranking_score")
        self.assertEqual(public_calibration["verificationStatus"], "static_proof")
        self.assertFalse(public_calibration["auditOnly"])
        self.assertTrue(public_calibration["guardrailApplied"])
        self.assertEqual(result["audit_only_findings"], [])
        self.assertEqual(result["decision_events"][0]["decision_reason"], "verified_or_static_proof_guardrail")
        self.assertTrue(result["decision_events"][0]["score_factors"]["guardrailApplied"])

    def test_review_calibration_verified_guardrail_respects_hard_exceptions(self) -> None:
        invalid = (audit_swarm_findings_from_payload(audit_payload([issue_card("Invalid static proof bug")])) or [])[0]
        invalid["verificationStatus"] = "static_proof"
        delta_excluded = (audit_swarm_findings_from_payload(audit_payload([issue_card("Old verified bug")])) or [])[0]
        delta_excluded["verificationStatus"] = "verified"
        records = [
            {
                "stage": "convergence",
                "decision": "rejected",
                "reason": "invalid_candidate_location",
                "finding": invalid,
                "fingerprint": worker_main.finding_fingerprint(invalid),
                "source_stats": {},
            },
            {
                "stage": "convergence",
                "decision": "rejected",
                "reason": "not_introduced_by_current_delta",
                "finding": delta_excluded,
                "fingerprint": worker_main.finding_fingerprint(delta_excluded),
                "source_stats": {},
            },
        ]

        result = worker_main.apply_review_calibration_decisions(
            config(),
            {"job_id": "job_guardrail_exceptions", "repo": "acme/api", "branch": "main", "commit": "a" * 40},
            [],
            records,
            attempt_id="wk_1-1",
        )

        events_by_title = {event["normalized_title"]: event for event in result["decision_events"]}
        invalid_event = events_by_title["invalid static proof bug"]
        self.assertEqual(invalid_event["score_factors"]["proposedDecision"], "rejected")
        self.assertEqual(invalid_event["score_factors"]["proposedReason"], "invalid_candidate_location")
        self.assertFalse(invalid_event["score_factors"]["guardrailApplied"])
        delta_event = events_by_title["old verified bug"]
        self.assertEqual(delta_event["score_factors"]["proposedDecision"], "audit_only")
        self.assertEqual(delta_event["score_factors"]["proposedReason"], "not_delta_relevant_but_verified")
        self.assertTrue(delta_event["score_factors"]["guardrailApplied"])

    def test_review_calibration_context_reliability_requires_effective_samples(self) -> None:
        finding = (audit_swarm_findings_from_payload(audit_payload([issue_card("Sparse context bug")])) or [])[0]
        finding["confidence"] = 0.95
        finding["verificationStatus"] = "potential_risk"
        record = {
            "stage": "convergence",
            "decision": "reported",
            "reason": "passed_convergence_gate",
            "finding": finding,
            "fingerprint": worker_main.finding_fingerprint(finding),
            "source_stats": {},
        }
        job = {
            "job_id": "job_context",
            "repo": "acme/api",
            "branch": "main",
            "commit": "a" * 40,
            "review_calibration_context": {
                "protocol": "pullwise-review-calibration/0.2",
                "scope_key": "user:usr_1|repo:repo_123|branch:main",
                "source_reliability": {
                    "source:correctness reviewer|category:quality|status:potential_risk": {
                        "posterior_mean": 0.2,
                        "posterior_lb": 0.1,
                        "effective_samples": 1,
                    }
                },
            },
        }

        _features, sparse_score = worker_main.review_score_candidate(record, job, config())
        self.assertEqual(sparse_score["reliability_source"], "prior")
        self.assertAlmostEqual(sparse_score["source_adjustment"], 1.0)

        job["review_calibration_context"]["source_reliability"][
            "source:correctness reviewer|category:quality|status:potential_risk"
        ]["effective_samples"] = 30
        _features, active_score = worker_main.review_score_candidate(record, job, config())
        self.assertEqual(active_score["reliability_source"], "review_calibration_context")
        self.assertLess(active_score["source_adjustment"], 1.0)

    def test_review_calibration_prefers_provider_model_reliability_context(self) -> None:
        finding = (audit_swarm_findings_from_payload(audit_payload([issue_card("Provider model scoped bug")])) or [])[0]
        finding["confidence"] = 0.95
        finding["verificationStatus"] = "potential_risk"
        record = {
            "stage": "convergence",
            "decision": "reported",
            "reason": "passed_convergence_gate",
            "finding": finding,
            "fingerprint": worker_main.finding_fingerprint(finding),
            "source_stats": {},
        }
        provider_key = "provider:codex|model:gpt 5 5|source:correctness reviewer|category:quality|status:potential_risk"
        job = {
            "job_id": "job_provider_model",
            "repo": "acme/api",
            "branch": "main",
            "commit": "a" * 40,
            "review_calibration_context": {
                "protocol": "pullwise-review-calibration/0.2",
                "scope_key": "user:usr_1|repo:repo_123|branch:main",
                "source_reliability": {
                    provider_key: {
                        "posterior_mean": 0.30,
                        "posterior_lb": 0.20,
                        "effective_samples": 40,
                    },
                    "source:correctness reviewer|category:quality|status:potential_risk": {
                        "posterior_mean": 0.95,
                        "posterior_lb": 0.90,
                        "effective_samples": 40,
                    },
                },
            },
        }

        _features, score = worker_main.review_score_candidate(record, job, config())

        self.assertEqual(score["reliability_source"], "review_calibration_context")
        self.assertEqual(score["cohort_key"], provider_key)
        self.assertLess(score["source_adjustment"], 1.0)

    def test_review_calibration_prefers_provider_model_confidence_bucket(self) -> None:
        finding = (audit_swarm_findings_from_payload(audit_payload([issue_card("Provider bucket bug")])) or [])[0]
        finding["confidence"] = 0.92
        finding["verificationStatus"] = "potential_risk"
        record = {
            "stage": "convergence",
            "decision": "reported",
            "reason": "passed_convergence_gate",
            "finding": finding,
            "fingerprint": worker_main.finding_fingerprint(finding),
            "source_stats": {},
        }
        provider_key = "provider:codex|model:gpt 5 5|source:correctness reviewer|category:quality|status:potential_risk"
        job = {
            "job_id": "job_provider_bucket",
            "repo": "acme/api",
            "branch": "main",
            "commit": "a" * 40,
            "review_calibration_context": {
                "protocol": "pullwise-review-calibration/0.2",
                "scope_key": "user:usr_1|repo:repo_123|branch:main",
                "confidence_calibration": {
                    provider_key: {
                        "0.90-0.95": {
                            "bucket_precision": 0.40,
                            "labeled_weight": 25,
                        }
                    },
                    "global": {
                        "0.90-0.95": {
                            "bucket_precision": 0.95,
                            "labeled_weight": 25,
                        }
                    },
                },
            },
        }

        with patch.dict(os.environ, {"PULLWISE_REVIEW_CALIBRATION_ENABLE_BUCKETS": "true"}, clear=False):
            _features, score = worker_main.review_score_candidate(record, job, config())

        self.assertAlmostEqual(score["calibrated_confidence"], 0.66)

    def test_review_calibration_samples_rejected_candidates_for_manual_review(self) -> None:
        finding = (audit_swarm_findings_from_payload(audit_payload([issue_card("Low confidence sample bug")])) or [])[0]
        finding["confidence"] = 0.10
        finding["verificationStatus"] = "potential_risk"
        record = {
            "stage": "convergence",
            "decision": "reported",
            "reason": "passed_convergence_gate",
            "finding": finding,
            "fingerprint": worker_main.finding_fingerprint(finding),
            "source_stats": {},
        }

        with patch.dict(
            os.environ,
            {
                "PULLWISE_REVIEW_CALIBRATION_MODE": "audit_only",
                "PULLWISE_REVIEW_CALIBRATION_SAMPLE_AUDIT_RATE": "1.0",
            },
            clear=False,
        ):
            result = worker_main.apply_review_calibration_decisions(
                config(),
                {"job_id": "job_sample", "repo": "acme/api", "branch": "main", "commit": "a" * 40},
                [finding],
                [record],
                attempt_id="wk_1-1",
            )

        self.assertEqual(result["reported_findings"], [])
        self.assertEqual(result["audit_only_findings"], [])
        self.assertEqual(result["rejected_reasons"], {"potential_risk_below_audit_threshold": 1})
        sample = result["rejected_samples"][0]
        self.assertEqual(sample["title"], "Low confidence sample bug")
        self.assertTrue(sample["sampledForManualReview"])
        self.assertEqual(sample["sampleReason"], "calibration_sample_rejected")
        self.assertEqual(sample["scoreBand"], "reject_band")
        self.assertEqual(sample["scoreKind"], "ranking_score")
        self.assertLess(sample["decisionScore"], 0.65)

    def test_review_calibration_marks_borderline_candidates_for_manual_review(self) -> None:
        findings = audit_swarm_findings_from_payload(
            audit_payload(
                [
                    issue_card("Audit borderline bug", issue_id="borderline-audit"),
                    issue_card("Rejected borderline bug", issue_id="borderline-reject"),
                ]
            )
        )
        audit_finding, rejected_finding = findings
        audit_finding["confidence"] = 0.85
        audit_finding["verificationStatus"] = "potential_risk"
        audit_finding["evidence"] = [{"summary": "Verifier reproduced the boundary failure.", "logPath": "verification/failure.log"}]
        audit_finding["whyNotFalsePositive"] = ["The guarded caller path is not used here."]
        rejected_finding["confidence"] = 0.75
        rejected_finding["verificationStatus"] = "potential_risk"
        records = [
            {
                "stage": "convergence",
                "decision": "reported",
                "reason": "passed_convergence_gate",
                "finding": finding,
                "fingerprint": worker_main.finding_fingerprint(finding),
                "source_stats": {},
            }
            for finding in findings
        ]

        with patch.dict(
            os.environ,
            {
                "PULLWISE_REVIEW_CALIBRATION_MODE": "audit_only",
                "PULLWISE_REVIEW_CALIBRATION_SAMPLE_AUDIT_RATE": "0.0",
                "PULLWISE_REVIEW_CALIBRATION_BORDERLINE_SAMPLE_WINDOW": "0.03",
            },
            clear=False,
        ):
            result = worker_main.apply_review_calibration_decisions(
                config(),
                {"job_id": "job_borderline", "repo": "acme/api", "branch": "main", "commit": "a" * 40},
                findings,
                records,
                attempt_id="wk_1-1",
            )

        self.assertEqual([item["title"] for item in result["reported_findings"]], [])
        self.assertEqual([item["title"] for item in result["audit_only_findings"]], ["Audit borderline bug"])
        audit_sample = result["audit_only_samples"][0]
        self.assertTrue(audit_sample["sampledForManualReview"])
        self.assertEqual(audit_sample["sampleReason"], "calibration_borderline_report_threshold")
        self.assertEqual(audit_sample["sampleStrategy"], "threshold_borderline")
        self.assertEqual(audit_sample["scoreBand"], "audit_band")
        self.assertEqual(audit_sample["scoreKind"], "ranking_score")
        self.assertAlmostEqual(audit_sample["decisionThreshold"], 0.82)
        self.assertLess(audit_sample["thresholdDistance"], 0.03)
        self.assertNotIn("sampleRate", audit_sample)

        rejected_sample = result["rejected_samples"][0]
        self.assertTrue(rejected_sample["sampledForManualReview"])
        self.assertEqual(rejected_sample["sampleReason"], "calibration_borderline_audit_threshold")
        self.assertEqual(rejected_sample["sampleStrategy"], "threshold_borderline")
        self.assertEqual(rejected_sample["scoreBand"], "reject_band")
        self.assertEqual(rejected_sample["scoreKind"], "ranking_score")
        self.assertAlmostEqual(rejected_sample["decisionThreshold"], 0.65)
        self.assertLess(rejected_sample["thresholdDistance"], 0.03)
        self.assertNotIn("sampleRate", rejected_sample)

    def test_review_calibration_logit_beta_outputs_truth_probability(self) -> None:
        finding = (audit_swarm_findings_from_payload(audit_payload([issue_card("Logit beta bug")])) or [])[0]
        finding["confidence"] = 0.92
        finding["verificationStatus"] = "potential_risk"
        record = {
            "stage": "convergence",
            "decision": "reported",
            "reason": "passed_convergence_gate",
            "finding": finding,
            "fingerprint": worker_main.finding_fingerprint(finding),
            "source_stats": {},
        }
        features = worker_main.review_candidate_features(finding, record)
        cohort_key = (
            f"source:{worker_main.normalized_source_key(features['source'])}"
            f"|category:{worker_main.normalized_source_key(features['category'])}"
            f"|status:{features['verification_status']}"
        )
        job = {
            "job_id": "job_logit",
            "repo": "acme/api",
            "branch": "main",
            "commit": "a" * 40,
            "review_calibration_context": {
                "protocol": "pullwise-review-calibration/0.2",
                "scope_key": "user:usr_1|repo:repo_123|branch:main",
                "source_reliability": {
                    cohort_key: {
                        "posterior_mean": 0.28,
                        "posterior_lb": 0.18,
                        "effective_samples": 40,
                    }
                },
            },
        }

        with patch.dict(
            os.environ,
            {
                "PULLWISE_REVIEW_CALIBRATION_MODEL": "logit_beta",
                "PULLWISE_REVIEW_CALIBRATION_MODE": "audit_only",
            },
            clear=False,
        ):
            cfg = config()
            _features, score = worker_main.review_score_candidate(record, job, cfg)
            result = worker_main.apply_review_calibration_decisions(
                cfg,
                job,
                [finding],
                [record],
                attempt_id="wk_1-1",
            )

        self.assertIsNotNone(score["truth_probability"])
        self.assertAlmostEqual(score["decision_score"], score["truth_probability"])
        self.assertLess(score["source_adjustment"], 0)
        self.assertLess(score["truth_probability"], score["calibrated_confidence"])
        event = result["decision_events"][0]
        self.assertEqual(event["truth_probability"], score["truth_probability"])
        self.assertEqual(event["decision_score"], score["truth_probability"])
        self.assertEqual(event["score_factors"]["scoreKind"], "truth_probability")
        self.assertEqual(event["score_factors"]["model"], "logit_beta")

    def test_review_calibration_consumes_confidence_buckets_and_drift_safe_mode(self) -> None:
        finding = (audit_swarm_findings_from_payload(audit_payload([issue_card("Drifted source bug")])) or [])[0]
        finding["confidence"] = 0.92
        record = {
            "stage": "convergence",
            "decision": "reported",
            "reason": "passed_convergence_gate",
            "finding": finding,
            "fingerprint": worker_main.finding_fingerprint(finding),
            "source_stats": {},
        }
        job = {
            "job_id": "job_drift",
            "repo": "acme/api",
            "branch": "main",
            "commit": "a" * 40,
            "review_calibration_context": {
                "protocol": "pullwise-review-calibration/0.2",
                "scope_key": "user:usr_1|repo:repo_123|branch:main",
                "confidence_calibration": {
                    "global": {
                        "0.90-0.95": {
                            "bucket_precision": 0.40,
                            "labeled_weight": 25,
                        }
                    }
                },
                "drift_state": {"provider:codex|model:gpt 5 5|source:correctness reviewer": "audit_only"},
            },
        }

        with patch.dict(
            os.environ,
            {
                "PULLWISE_REVIEW_CALIBRATION_ENABLE_BUCKETS": "true",
                "PULLWISE_REVIEW_CALIBRATION_ENABLE_DRIFT": "true",
                "PULLWISE_REVIEW_CALIBRATION_MIN_EFFECTIVE_SAMPLES": "20",
                "PULLWISE_REVIEW_CALIBRATION_MODE": "audit_only",
            },
            clear=False,
        ):
            cfg = config()
            _features, score = worker_main.review_score_candidate(record, job, cfg)
            result = worker_main.apply_review_calibration_decisions(
                cfg,
                job,
                [finding],
                [record],
                attempt_id="wk_1-1",
            )

        self.assertLess(score["calibrated_confidence"], 0.90)
        self.assertEqual(score["drift_state"], "audit_only")
        self.assertEqual(result["reported_findings"], [])
        self.assertEqual([item["title"] for item in result["audit_only_findings"]], ["Drifted source bug"])
        self.assertEqual(result["decision_events"][0]["decision_reason"], "drift_audit_only_source")

    def test_run_job_uploads_progress_result_and_cleans_checkout(self) -> None:
        worker = Worker(config())
        worker.client = Mock()
        checkout_dir = Path(worker.config.work_dir) / "job_1"

        resolved_commit = "0123456789abcdef0123456789abcdef01234567"
        with patch("pullwise_worker.main.clone_repository", return_value=resolved_commit) as clone_repository, \
            patch("pullwise_worker.main.collect_preflight_metadata", return_value={"mode": "static"}) as collect_preflight, \
            patch(
                "pullwise_worker.main.run_verifier_commands",
                return_value=({"enabled": False, "runs": []}, [], "verifier disabled"),
            ) as run_verifier, \
            patch("pullwise_worker.main.run_codex_review") as run_codex_review, \
            patch("pullwise_worker.main.shutil.rmtree") as rmtree:
            audit_with_usage = audit_payload(
                [issue_card("Bug", severity="P1", issue_id="bug")],
                [
                    {
                        "issue_id": "bug",
                        "verifier_role": "prover",
                        "verdict": "confirmed",
                        "confidence": 0.86,
                        "proof_type": "static_proof",
                        "proof_strength": 2,
                        "evidence": ["Static proof confirms the candidate."],
                        "commands_run": [],
                        "result_summary": "Static proof confirms the candidate.",
                    }
                ],
            )
            audit_with_usage["aiUsage"] = {
                "model": "gpt-5.5",
                "input_tokens": 123,
                "output_tokens": 45,
                "total_tokens": 168,
            }
            run_codex_review.return_value = (
                audit_with_usage,
                {"critical": 0, "high": 1, "medium": 0, "low": 0, "info": 0},
                "review ok",
            )

            worker.run_job(worker_job(job_id="job_1", attempt=2, repo="acme/api", commit="pending"))

        clone_repository.assert_called_once()
        self.assertEqual(clone_repository.call_args.args[1], checkout_dir)
        collect_preflight.assert_called_once()
        run_verifier.assert_called_once()
        run_codex_review.assert_called_once()
        worker.client.result.assert_called_once()
        result_payload = worker.client.result.call_args.args[1]
        self.assertEqual(result_payload["status"], "done")
        self.assertEqual(result_payload["attempt_id"], "wk_1-2")
        self.assertEqual(result_payload["commit"], resolved_commit)
        self.assertEqual(result_payload["resolved_commit"], resolved_commit)
        self.assertEqual(result_payload["preflight"], {"mode": "static", "verifier": {"enabled": False, "runs": []}})
        self.assertEqual(result_payload["audit_protocol"], "audit-swarm/0.1")
        self.assertEqual(result_payload["issue_cards"][0]["title"], "Bug")
        self.assertEqual(result_payload["summary"]["high"], 1)
        self.assertNotIn("findings", result_payload)
        self.assertEqual(result_payload["convergence_state"]["protocol"], "pullwise-convergence/0.1")
        self.assertEqual(result_payload["convergence_state"]["open_findings"][0]["title"], "Bug")
        self.assertEqual(result_payload["aiUsage"], {"model": "gpt-5.5"})
        self.assertEqual(result_payload["verification_audit"]["candidateCount"], 1)
        self.assertEqual(result_payload["verification_audit"]["reportedCount"], 1)
        self.assertEqual(result_payload["verification_audit"]["rejectedCount"], 0)
        self.assertEqual(result_payload["completion_audit"], result_payload["completionAudit"])
        self.assertEqual(result_payload["completion_audit"]["protocol"], "pullwise-completion-audit/0.1")
        self.assertEqual(result_payload["completion_audit"]["status"], "passed")
        self.assertEqual(result_payload["job_trace"], result_payload["jobTrace"])
        self.assertEqual(result_payload["job_trace"]["protocol"], "pullwise-job-trace/0.1")
        self.assertEqual(result_payload["job_trace"]["candidateCountBeforeFilter"], 1)
        self.assertEqual(
            [checkpoint["stage"] for checkpoint in result_payload["job_trace"]["checkpoints"]],
            ["clone", "preflight", "graph", "verifier", "agent", "filter", "report"],
        )
        self.assertEqual(result_payload["result_checksum"], result_checksum({k: v for k, v in result_payload.items() if k != "result_checksum"}))
        self.assertGreaterEqual(worker.client.progress.call_count, 3)
        progress_payloads = [call.kwargs for call in worker.client.progress.call_args_list]
        self.assertTrue(any(payload.get("job_trace", {}).get("status") == "running" for payload in progress_payloads))
        final_progress = progress_payloads[-1]
        self.assertEqual(final_progress["completion_audit"]["protocol"], "pullwise-completion-audit/0.1")
        self.assertEqual(final_progress["completion_audit"], result_payload["completion_audit"])
        self.assertEqual(final_progress["job_trace"], result_payload["job_trace"])
        rmtree.assert_called_with(checkout_dir, ignore_errors=True)

    def test_run_job_uploads_repository_graph_progress_and_result(self) -> None:
        worker = Worker(config())
        worker.client = Mock()
        impact_graph = {
            "version": "impact-graph/0.1",
            "mode": "repository",
            "targets": [{"id": "file:src/app.py", "path": "src/app.py", "relations": {}}],
            "promptText": "Impact context:\n- src/app.py -> tests: none detected; docs: none detected; config: none detected.",
        }
        graph = {
            "version": "repository-graph/0.2",
            "nodes": [{"id": "file:src/app.py", "label": "app.py", "type": "entrypoint", "path": "src/app.py"}],
            "edges": [],
            "architectureSummary": {
                "entrypoints": ["src/app.py"],
                "modules": ["src"],
                "promptText": "Repository architecture: src/app.py handles requests.",
            },
            "impactGraph": impact_graph,
        }
        semantic_graph = {
            "version": "semantic-code-graph/0.1",
            "stats": {"source": "static", "symbols": 1, "relationships": 0},
            "nodes": [{"id": "symbol:src/app.py:app", "label": "app", "type": "function", "path": "src/app.py"}],
            "edges": [],
        }
        review_jobs = []

        def run_review(_config: WorkerConfig, job: dict, _checkout_dir: Path) -> tuple[dict, dict, str]:
            review_jobs.append(dict(job))
            self.assertTrue(
                any(call.kwargs.get("repository_graph") == graph for call in worker.client.progress.call_args_list)
            )
            self.assertTrue(
                any(call.kwargs.get("semantic_graph") == semantic_graph for call in worker.client.progress.call_args_list)
            )
            self.assertTrue(
                any(call.kwargs.get("impact_graph") == impact_graph for call in worker.client.progress.call_args_list)
            )
            return audit_payload(), {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0}, "review ok"

        with patch("pullwise_worker.main.clone_repository", return_value="0123456789abcdef0123456789abcdef01234567"), \
            patch("pullwise_worker.main.collect_preflight_metadata", return_value={"mode": "static"}) as collect_preflight, \
            patch("pullwise_worker.main.build_repository_graph_bundle", return_value=(graph, semantic_graph)) as build_graph, \
            patch(
                "pullwise_worker.main.run_verifier_commands",
                return_value=({"enabled": False, "runs": []}, [], "verifier disabled"),
            ), \
            patch("pullwise_worker.main.run_codex_review", side_effect=run_review), \
            patch.object(worker, "upload_result_with_retry") as upload_result, \
            patch("pullwise_worker.main.shutil.rmtree"):
            worker.run_job(worker_job(job_id="job_graph", attempt=1, repo="acme/api", commit="pending"))

        collect_preflight.assert_called_once()
        build_graph.assert_called_once()
        self.assertEqual(review_jobs[0]["architecture_summary"], graph["architectureSummary"])
        self.assertEqual(review_jobs[0]["semantic_graph"], semantic_graph)
        self.assertEqual(review_jobs[0]["impact_graph"], impact_graph)
        graph_progress = [call for call in worker.client.progress.call_args_list if call.kwargs.get("repository_graph")]
        self.assertEqual(graph_progress[0].kwargs["repository_graph"], graph)
        self.assertEqual(graph_progress[0].kwargs["semantic_graph"], semantic_graph)
        self.assertEqual(graph_progress[0].kwargs["impact_graph"], impact_graph)
        result_payload = upload_result.call_args.args[1]
        self.assertEqual(result_payload["repository_graph"], graph)
        self.assertEqual(result_payload["semantic_graph"], semantic_graph)
        self.assertEqual(result_payload["impact_graph"], impact_graph)

    def test_run_job_fails_repository_too_large_before_verifier_or_review(self) -> None:
        worker = Worker(config())
        worker.client = Mock()
        resolved_commit = "0123456789abcdef0123456789abcdef01234567"

        def clone_large_checkout(_job: dict, checkout_dir: Path) -> str:
            checkout_dir.mkdir(parents=True, exist_ok=True)
            (checkout_dir / "one.txt").write_text("one", encoding="utf-8")
            (checkout_dir / "two.txt").write_text("two", encoding="utf-8")
            return resolved_commit

        with patch("pullwise_worker.main.clone_repository", side_effect=clone_large_checkout), \
            patch("pullwise_worker.main.collect_preflight_metadata") as collect_preflight, \
            patch("pullwise_worker.main.run_verifier_commands") as run_verifier, \
            patch("pullwise_worker.main.run_codex_review") as run_codex_review:
            worker.run_job(
                worker_job(
                    job_id="job_large",
                    attempt=1,
                    repo="acme/large",
                    commit="pending",
                    repositoryLimits={"maxFiles": 1, "maxBytes": 50 * 1024 * 1024},
                )
            )

        collect_preflight.assert_not_called()
        run_verifier.assert_not_called()
        run_codex_review.assert_not_called()
        worker.client.result.assert_called_once()
        payload = worker.client.result.call_args.args[1]
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["error_code"], "REPOSITORY_TOO_LARGE")
        self.assertEqual(payload["errorCode"], "REPOSITORY_TOO_LARGE")
        self.assertEqual(payload["commit"], resolved_commit)
        self.assertEqual(payload["preflight"]["repositoryStats"]["fileCount"], 2)
        self.assertTrue(payload["preflight"]["repositoryStats"]["scanStoppedEarly"])
        self.assertEqual(payload["preflight"]["repositoryLimits"]["maxFiles"], 1)
        self.assertTrue(payload["preflight"]["repositoryLimitExceeded"])
        self.assertEqual(payload["preflight"]["repositoryLimitReasons"], ["file_count"])
        self.assertIn("Repository is too large", payload["error"])
        self.assertEqual(payload["completion_audit"], payload["completionAudit"])
        self.assertEqual(payload["completion_audit"]["status"], "passed")
        self.assertFalse(payload["completion_audit"]["retryRecommended"])
        self.assertEqual(payload["job_trace"], payload["jobTrace"])
        self.assertEqual(payload["job_trace"]["status"], "failed")
        self.assertIn("Repository exceeds configured limits", payload["job_trace"]["nextRetryHint"])
        final_progress = worker.client.progress.call_args_list[-1].kwargs
        self.assertEqual(final_progress["completion_audit"], payload["completion_audit"])
        self.assertEqual(final_progress["job_trace"], payload["job_trace"])

    def test_run_job_continues_when_verifier_errors(self) -> None:
        worker = Worker(config())
        worker.client = Mock()

        with patch("pullwise_worker.main.clone_repository", return_value="0123456789abcdef0123456789abcdef01234567"), \
            patch("pullwise_worker.main.collect_preflight_metadata", return_value={"mode": "static"}), \
            patch("pullwise_worker.main.run_verifier_commands", side_effect=RuntimeError("verifier boom")), \
            patch(
                "pullwise_worker.main.run_codex_review",
                return_value=(audit_payload(), {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0}, "review ok"),
            ), \
            patch("pullwise_worker.main.shutil.rmtree"):
            worker.run_job(worker_job(job_id="job_verifier_error", attempt=1, repo="acme/api"))

        result_payload = worker.client.result.call_args.args[1]
        self.assertEqual(result_payload["status"], "done")
        self.assertIn("Verifier failed before completing", result_payload["preflight"]["verifier"]["summary"])
        self.assertEqual(result_payload["preflight"]["verifier"]["runs"], [])

    def test_run_job_uploads_verifier_findings_and_execution_scope(self) -> None:
        worker = Worker(config())
        worker.client = Mock()
        verifier_finding = {
            "title": "Verifier failure",
            "severity": "high",
            "file": "package.json",
            "line": 3,
            "verificationStatus": "verified",
            "evidence": [
                {
                    "type": "runtime_log",
                    "summary": "npm run test failed with exit code 1.",
                    "command": "npm run test",
                    "logPath": "verification/job/test.log",
                }
            ],
            "reproduction": {"commands": ["npm run test"]},
        }

        with patch("pullwise_worker.main.clone_repository", return_value="0123456789abcdef0123456789abcdef01234567"), \
            patch(
                "pullwise_worker.main.collect_preflight_metadata",
                return_value={"mode": "static", "execution": "no_project_scripts", "summary": "Static only."},
            ), \
            patch(
                "pullwise_worker.main.run_verifier_commands",
                return_value=(
                    {"enabled": True, "runs": [{"script": "test", "status": "failed"}]},
                    [verifier_finding],
                    "verifier ran 1 command",
                ),
            ), \
            patch(
                "pullwise_worker.main.run_codex_review",
                return_value=(
                    audit_payload([issue_card("Provider finding", severity="low", issue_id="provider", file="", evidence=[])]),
                    {"critical": 0, "high": 0, "medium": 0, "low": 1, "info": 0},
                    "review ok",
                ),
            ), \
            patch("pullwise_worker.main.shutil.rmtree"):
            worker.run_job(worker_job(job_id="job_verifier_findings", attempt=1, repo="acme/api"))

        result_payload = worker.client.result.call_args.args[1]
        self.assertEqual(result_payload["preflight"]["execution"], "allowlisted_verifier_scripts")
        self.assertEqual(result_payload["issue_cards"][0]["title"], "Verifier failure")
        self.assertEqual(result_payload["verification_results"][0]["verdict"], "confirmed")
        self.assertEqual(result_payload["summary"]["high"], 1)
        self.assertEqual(result_payload["summary"]["low"], 0)
        self.assertEqual(result_payload["verification_audit"]["candidateCount"], 2)
        self.assertEqual(result_payload["verification_audit"]["reportedCount"], 1)
        self.assertEqual(result_payload["verification_audit"]["rejectedCount"], 1)
        self.assertEqual(result_payload["verification_audit"]["verifiedCount"], 1)
        self.assertEqual(result_payload["verification_audit"]["rejectedReasons"], [{"reason": "missing_evidence", "count": 1}])
        self.assertEqual(
            result_payload["verification_audit"]["rejectedSamples"],
            [
                {
                    "reason": "missing_evidence",
                    "title": "Provider finding",
                    "severity": "low",
                    "category": "Quality",
                    "verificationStatus": "potential_risk",
                }
            ],
        )

    def test_done_result_upload_timeout_retries_same_payload_without_failed_result(self) -> None:
        worker = Worker(config())
        worker.client = Mock()
        worker.client.result.side_effect = [PullwiseRequestError("timed out"), None]

        with patch("pullwise_worker.main.clone_repository", return_value="0123456789abcdef0123456789abcdef01234567"), \
            patch(
                "pullwise_worker.main.run_codex_review",
                return_value=(audit_payload(), {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0}, "review ok"),
            ), \
            patch("pullwise_worker.main.time.sleep"), \
            patch("pullwise_worker.main.shutil.rmtree"):
            worker.run_job(worker_job(job_id="job_retry", attempt=1, repo="acme/api"))

        self.assertEqual(worker.client.result.call_count, 2)
        first_payload = worker.client.result.call_args_list[0].args[1]
        second_payload = worker.client.result.call_args_list[1].args[1]
        self.assertEqual(first_payload, second_payload)
        self.assertEqual(second_payload["status"], "done")
        self.assertIsNone(worker.last_error)

    def test_successful_job_cleanup_does_not_use_previous_last_error(self) -> None:
        worker = Worker(config())
        worker.client = Mock()
        worker.last_error = "previous job failed"
        worker.config.failed_checkout_retention_seconds = 3600
        checkout_dir = Path(worker.config.work_dir) / "job_success_after_failure"

        def clone(_job: dict, path: Path) -> str:
            path.mkdir(parents=True)
            (path / "file.txt").write_text("ok", encoding="utf-8")
            return "0123456789abcdef0123456789abcdef01234567"

        with patch("pullwise_worker.main.clone_repository", side_effect=clone), \
            patch("pullwise_worker.main.collect_preflight_metadata", return_value={"mode": "static"}), \
            patch(
                "pullwise_worker.main.run_verifier_commands",
                return_value=({"enabled": False, "runs": []}, [], "verifier disabled"),
            ), \
            patch(
                "pullwise_worker.main.run_codex_review",
                return_value=(audit_payload(), {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0}, "review ok"),
            ):
            worker.run_job(worker_job(job_id="job_success_after_failure", attempt=1, repo="acme/api"))

        self.assertIsNone(worker.last_error)
        self.assertFalse(checkout_dir.exists())
        self.assertFalse(checkout_dir.with_suffix(".failed-retain").exists())

    def test_done_result_upload_exhaustion_does_not_submit_failed_result(self) -> None:
        worker = Worker(config())
        worker.config.result_upload_attempts = 2
        worker.client = Mock()
        worker.client.result.side_effect = PullwiseRequestError("timed out")

        with patch("pullwise_worker.main.clone_repository", return_value="0123456789abcdef0123456789abcdef01234567"), \
            patch(
                "pullwise_worker.main.run_codex_review",
                return_value=(audit_payload(), {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0}, "review ok"),
            ), \
            patch("pullwise_worker.main.time.sleep"), \
            patch("pullwise_worker.main.shutil.rmtree"):
            worker.run_job(worker_job(job_id="job_timeout", attempt=1, repo="acme/api"))

        self.assertEqual(worker.client.result.call_count, 2)
        statuses = [call.args[1]["status"] for call in worker.client.result.call_args_list]
        self.assertEqual(statuses, ["done", "done"])
        self.assertIn("result upload failed", worker.last_error)

    def test_done_result_upload_retries_server_http_errors(self) -> None:
        worker = Worker(config())
        worker.config.result_upload_attempts = 2
        worker.client = Mock()
        worker.client.result.side_effect = [PullwiseHTTPError("HTTP 500", 500), None]

        with patch("pullwise_worker.main.clone_repository", return_value="0123456789abcdef0123456789abcdef01234567"), \
            patch(
                "pullwise_worker.main.run_codex_review",
                return_value=(audit_payload(), {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0}, "review ok"),
            ), \
            patch("pullwise_worker.main.time.sleep"), \
            patch("pullwise_worker.main.shutil.rmtree"):
            worker.run_job(worker_job(job_id="job_http_retry", attempt=1, repo="acme/api"))

        self.assertEqual(worker.client.result.call_count, 2)
        self.assertIsNone(worker.last_error)

    def test_poll_sleep_backs_off_empty_and_failed_polls_with_jitter(self) -> None:
        worker = Worker(config())
        worker.config.poll_seconds = 5
        worker.config.poll_jitter_seconds = 0
        worker.config.max_backoff_seconds = 20

        self.assertEqual(worker.next_poll_sleep(claimed_jobs=0, loop_error=False), 5)
        self.assertEqual(worker.next_poll_sleep(claimed_jobs=0, loop_error=False), 10)
        self.assertEqual(worker.next_poll_sleep(claimed_jobs=0, loop_error=False), 20)
        self.assertEqual(worker.next_poll_sleep(claimed_jobs=1, loop_error=False), 1)
        self.assertEqual(worker.next_poll_sleep(claimed_jobs=0, loop_error=False, free_slots=2), 5)
        self.assertEqual(worker.next_poll_sleep(claimed_jobs=0, loop_error=False, free_slots=1), 10)
        self.assertEqual(worker.next_poll_sleep(claimed_jobs=0, loop_error=True), 5)
        self.assertEqual(worker.next_poll_sleep(claimed_jobs=0, loop_error=True), 10)

    def test_once_loop_reports_heartbeat_error_without_crashing(self) -> None:
        worker = Worker(config())
        worker.client = Mock()
        worker.client.heartbeat.side_effect = PullwiseRequestError("server down")

        with patch.object(worker, "refresh_readiness_if_due", return_value=True), \
            patch("pullwise_worker.main.time.sleep") as sleep:
            worker.run(once=True)

        worker.client.heartbeat.assert_called_once()
        worker.client.claim_many.assert_not_called()
        sleep.assert_not_called()
        self.assertIn("heartbeat failed", worker.last_error)

    def test_once_loop_reports_malformed_heartbeat_json_without_crashing(self) -> None:
        worker = Worker(config())
        worker.client = Mock()
        worker.client.heartbeat.side_effect = lambda **_kwargs: PullwiseResponse(b"{").json()

        with patch.object(worker, "refresh_readiness_if_due", return_value=True), \
            patch("pullwise_worker.main.time.sleep") as sleep:
            worker.run(once=True)

        worker.client.heartbeat.assert_called_once()
        worker.client.claim_many.assert_not_called()
        sleep.assert_not_called()
        self.assertIn("heartbeat failed", worker.last_error)
        self.assertIn("invalid JSON response", worker.last_error)

    def test_once_loop_sends_machine_metrics_on_heartbeat(self) -> None:
        worker = Worker(config())
        worker.client = Mock()
        worker.client.heartbeat.return_value = {}
        worker.client.claim_many.return_value = []
        expected_metrics = {
            "ok": True,
            "collectedAt": 1781200000,
            "worker": {"hostname": "worker-host"},
            "cpu": {"logicalCount": 8, "loadAverage": None},
            "memory": {"usedPercent": 62.5},
            "storage": {"usedPercent": 40.0},
        }

        with patch.object(worker, "refresh_readiness_if_due", return_value=True), \
            patch("pullwise_worker.main.worker_machine_metrics_payload", return_value=expected_metrics) as collect, \
            patch("pullwise_worker.main.time.time", return_value=1781200000), \
            patch("pullwise_worker.main.time.sleep") as sleep:
            worker.run(once=True)

        collect.assert_called_once_with(storage_path=str(worker.config.work_dir), timestamp=1781200000)
        worker.client.heartbeat.assert_called_once()
        self.assertIs(worker.client.heartbeat.call_args.kwargs["machine_metrics"], expected_metrics)
        sleep.assert_not_called()

    def test_once_loop_reports_malformed_claim_json_without_crashing(self) -> None:
        worker = Worker(config())
        worker.client = Mock()
        worker.client.heartbeat.return_value = {}
        worker.client.claim_many.side_effect = lambda _limit: PullwiseResponse(b"{").json()

        with patch.object(worker, "refresh_readiness_if_due", return_value=True), \
            patch("pullwise_worker.main.time.sleep") as sleep:
            worker.run(once=True)

        worker.client.heartbeat.assert_called_once()
        worker.client.claim_many.assert_called_once_with(1)
        sleep.assert_not_called()
        self.assertIn("job claim failed", worker.last_error)
        self.assertIn("invalid JSON response", worker.last_error)

    def test_once_loop_does_not_claim_when_readiness_checks_fail(self) -> None:
        worker = Worker(config())
        worker.client = Mock()
        checks = [
            ("git", False, "not found"),
            ("codex", True, "codex ok"),
            ("codex_ready", True, "ready"),
        ]

        with patch("pullwise_worker.main.worker_readiness_checks", return_value=(checks, True)), \
            patch("pullwise_worker.main.time.sleep") as sleep:
            worker.run(once=True)

        worker.client.heartbeat.assert_called_once()
        heartbeat_kwargs = worker.client.heartbeat.call_args.kwargs
        self.assertEqual(heartbeat_kwargs["doctor_status"], "degraded")
        self.assertTrue(heartbeat_kwargs["codex_ready"])
        worker.client.claim_many.assert_not_called()
        sleep.assert_not_called()
        self.assertIn("worker not ready: git: not found", worker.last_error)

    def test_once_loop_claims_and_submits_at_most_one_job(self) -> None:
        worker = Worker(config())
        worker.config.max_concurrent_jobs = 2
        worker.client = Mock()
        worker.client.heartbeat.return_value = {}
        worker.client.claim_many.return_value = [{"job_id": "job_1"}, {"job_id": "job_2"}]

        with patch.object(worker, "refresh_readiness_if_due", return_value=True), \
            patch.object(worker, "run_job", return_value=None) as run_job, \
            patch("pullwise_worker.main.time.sleep") as sleep:
            worker.run(once=True)

        worker.client.claim_many.assert_called_once_with(1)
        self.assertEqual(run_job.call_count, 1)
        self.assertEqual(run_job.call_args.args[0]["job_id"], "job_1")
        sleep.assert_not_called()

    def test_loop_limits_claim_batch_below_free_slots(self) -> None:
        class StopLoop(Exception):
            pass

        worker = Worker(config())
        worker.config.max_concurrent_jobs = 5
        worker.config.max_claim_jobs = 2
        worker.client = Mock()
        worker.client.heartbeat.return_value = {}
        worker.client.claim_many.return_value = []

        with patch.object(worker, "refresh_readiness_if_due", return_value=True), \
            patch("pullwise_worker.main.time.sleep", side_effect=StopLoop):
            with self.assertRaises(StopLoop):
                worker.run()

        worker.client.claim_many.assert_called_once_with(2)

    def test_refresh_readiness_reports_codex_specific_state_with_opencode_fallback(self) -> None:
        worker = Worker(config())
        checks = [
            ("git", True, "git ok"),
            ("codex_ready", False, "not logged in"),
            ("opencode", True, "opencode 1.0.0"),
            ("provider_ready", True, "opencode"),
            ("checkout_root", True, "ok"),
            ("log_dir", True, "ok"),
            ("disk_space", True, "2048 MB free"),
        ]

        with patch("pullwise_worker.main.worker_readiness_checks", return_value=(checks, True)):
            ready = worker.refresh_readiness_if_due()

        self.assertTrue(ready)
        self.assertEqual(worker._doctor_status, "ok")
        self.assertFalse(worker._codex_ready)
        self.assertTrue(worker.refresh_readiness_if_due())

    def test_pullwise_client_posts_json_with_authorization(self) -> None:
        class FakeResponse:
            def __enter__(self) -> "FakeResponse":
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def read(self) -> bytes:
                return b'{"ok": true}'

        cfg = config()
        client = PullwiseClient(cfg)

        with patch("pullwise_worker.main.urllib.request.urlopen", return_value=FakeResponse()) as urlopen:
            response = client.post("/worker/heartbeat", {"worker_id": "wk_1"})

        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://server.test/worker/heartbeat")
        self.assertEqual(request.get_header("Authorization"), "Bearer worker-token")
        self.assertEqual(json.loads(request.data.decode("utf-8")), {"worker_id": "wk_1"})
        self.assertEqual(response.json(), {"ok": True})

    def test_pullwise_client_progress_uploads_repository_graph(self) -> None:
        cfg = config()
        client = PullwiseClient(cfg)
        with patch.object(client, "post", return_value=PullwiseResponse(b"{}")) as post:
            client.progress(
                "job_1",
                "index",
                30,
                "Repository graph ready",
                repository_graph={"version": "repository-graph/0.2", "nodes": [], "edges": []},
                semantic_graph={"version": "semantic-code-graph/0.1", "nodes": [], "edges": []},
                impact_graph={"version": "impact-graph/0.1", "targets": []},
                completion_audit={"protocol": "pullwise-completion-audit/0.1", "status": "passed"},
                job_trace={"protocol": "pullwise-job-trace/0.1", "status": "running"},
            )

        payload = post.call_args.args[1]
        self.assertEqual(payload["repository_graph"]["version"], "repository-graph/0.2")
        self.assertEqual(payload["semantic_graph"]["version"], "semantic-code-graph/0.1")
        self.assertEqual(payload["impact_graph"]["version"], "impact-graph/0.1")
        self.assertEqual(payload["completion_audit"], payload["completionAudit"])
        self.assertEqual(payload["completion_audit"]["protocol"], "pullwise-completion-audit/0.1")
        self.assertEqual(payload["job_trace"], payload["jobTrace"])
        self.assertEqual(payload["job_trace"]["protocol"], "pullwise-job-trace/0.1")

    def test_once_loop_executes_lifecycle_command_from_heartbeat(self) -> None:
        worker = Worker(config())
        worker.client = Mock()
        worker.client.heartbeat.return_value = {
            "worker": {"worker_id": "wk_1", "status": "disabled"},
            "command": {"id": "cmd_stop", "command": "stop", "status": "pending"},
        }

        with patch.object(worker, "refresh_readiness_if_due", return_value=True), \
            patch("pullwise_worker.main.execute_lifecycle_command", return_value=0) as execute, \
            patch("pullwise_worker.main.time.sleep") as sleep:
            worker.run(once=True)

        execute.assert_called_once_with("stop")
        worker.client.command_status.assert_any_call("cmd_stop", "running")
        worker.client.command_status.assert_any_call("cmd_stop", "succeeded")
        worker.client.claim_many.assert_not_called()
        sleep.assert_not_called()

    def test_worker_readiness_checks_cover_dependencies_paths_and_disk(self) -> None:
        cfg = config()

        with patch("pullwise_worker.main.command_ok", side_effect=[(False, "git missing"), (True, "v22.21.0"), (True, "codex ok")]), \
            patch("pullwise_worker.main.codex_ready_check", return_value=(True, "ready")), \
            patch("pullwise_worker.main.shutil.disk_usage", return_value=Mock(free=2 * 1024 * 1024 * 1024)):
            checks, codex_ready = worker_readiness_checks(cfg)

        by_name = {name: (ok, detail) for name, ok, detail in checks}
        self.assertFalse(by_name["git"][0])
        self.assertTrue(by_name["node"][0])
        self.assertTrue(by_name["codex"][0])
        self.assertTrue(by_name["codex_ready"][0])
        self.assertTrue(by_name["checkout_root"][0])
        self.assertTrue(by_name["log_dir"][0])
        self.assertTrue(by_name["disk_space"][0])
        self.assertTrue(codex_ready)

    def test_worker_readiness_rejects_remote_http_server_url_if_config_is_mutated(self) -> None:
        cfg = config()
        cfg.server_url = "http://server.test"

        with patch("pullwise_worker.main.command_ok", side_effect=[(True, "git ok"), (True, "v22.21.0"), (True, "codex ok")]), \
            patch("pullwise_worker.main.codex_ready_check", return_value=(True, "ready")), \
            patch("pullwise_worker.main.shutil.disk_usage", return_value=Mock(free=2 * 1024 * 1024 * 1024)):
            checks, _provider_ready = worker_readiness_checks(cfg)

        by_name = {name: (ok, detail) for name, ok, detail in checks}
        self.assertFalse(by_name["server_url"][0])
        self.assertEqual(by_name["server_url"][1], "http://server.test")

    def test_worker_readiness_allows_opencode_fallback_when_codex_login_fails(self) -> None:
        cfg = config()
        cfg.provider_chain = ["codex", "opencode"]
        cfg.opencode_command = "opencode"

        with patch(
                "pullwise_worker.main.command_ok",
                side_effect=[
                    (True, "git ok"),
                    (True, "v22.21.0"),
                    (True, "codex ok"),
                    (True, "opencode 1.0.0"),
                ],
            ), \
            patch("pullwise_worker.main.codex_ready_check", return_value=(False, "not logged in")), \
            patch("pullwise_worker.main.opencode_auth_check", return_value=(True, "authenticated for opencode")), \
            patch("pullwise_worker.main.shutil.disk_usage", return_value=Mock(free=2 * 1024 * 1024 * 1024)):
            checks, provider_ready = worker_readiness_checks(cfg)

        by_name = {name: (ok, detail) for name, ok, detail in checks}
        self.assertFalse(by_name["codex_ready"][0])
        self.assertTrue(by_name["opencode"][0])
        self.assertTrue(by_name["opencode_ready"][0])
        self.assertTrue(by_name["provider_ready"][0])
        self.assertTrue(provider_ready)

    def test_clone_repository_uses_short_lived_token(self) -> None:
        head = "abcdefabcdefabcdefabcdefabcdefabcdefabcd"
        with patch("pullwise_worker.main.subprocess.run") as run:
            run.return_value = Mock(stdout=f"{head}\n", stderr="", returncode=0)
            resolved = clone_repository(
                {
                    "repo": "acme/api",
                    "branch": "main",
                    "commit": "pending",
                    "clone_url": "https://github.com/acme/api.git",
                    "clone_token": {"token": "short-token", "repo": "acme/api"},
                },
                Path("checkout"),
            )

        clone_command = run.call_args_list[0].args[0]
        clone_env = run.call_args_list[0].kwargs["env"]
        self.assertEqual(resolved, head)
        self.assertEqual(run.call_args_list[-1].args[0], ["git", "-C", "checkout", "rev-parse", "HEAD"])
        self.assertEqual(clone_command[:4], ["git", "clone", "--depth", "1"])
        self.assertEqual(clone_command[-2], "https://github.com/acme/api.git")
        self.assertNotIn("short-token", " ".join(clone_command))
        self.assertNotIn("short-token", " ".join(str(value) for value in clone_env.values()))
        self.assertEqual(clone_env["GIT_CONFIG_KEY_0"], "http.https://github.com/acme/api.git.extraHeader")

    def test_clone_repository_rejects_clone_token_for_untrusted_clone_url(self) -> None:
        with patch("pullwise_worker.main.subprocess.run") as run:
            with self.assertRaisesRegex(RuntimeError, "host does not match configured GitHub host"):
                clone_repository(
                    {
                        "repo": "acme/api",
                        "branch": "main",
                        "commit": "pending",
                        "clone_url": "https://evil.example/acme/api.git",
                        "clone_token": {"token": "short-token", "repo": "acme/api"},
                    },
                    Path("checkout"),
                )

        run.assert_not_called()

    def test_clone_repository_rejects_clone_token_for_wrong_repository_path(self) -> None:
        with patch("pullwise_worker.main.subprocess.run") as run:
            with self.assertRaisesRegex(RuntimeError, "path does not match requested repository"):
                clone_repository(
                    {
                        "repo": "acme/api",
                        "branch": "main",
                        "commit": "pending",
                        "clone_url": "https://github.com/acme/other.git",
                        "clone_token": {"token": "short-token", "repo": "acme/api"},
                    },
                    Path("checkout"),
                )

        run.assert_not_called()

    @unittest.skipIf(shutil.which("git") is None, "git is required for clone integration coverage")
    def test_clone_repository_can_checkout_pinned_non_tip_commit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            origin = Path(tmp) / "origin"
            checkout = Path(tmp) / "checkout"
            subprocess.run(
                ["git", "init", "--initial-branch", "main", str(origin)],
                check=True,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            subprocess.run(["git", "-C", str(origin), "config", "user.email", "ci@example.com"], check=True)
            subprocess.run(["git", "-C", str(origin), "config", "user.name", "CI"], check=True)
            (origin / "file.txt").write_text("first\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(origin), "add", "file.txt"], check=True)
            subprocess.run(["git", "-C", str(origin), "commit", "-m", "first"], check=True, stdout=subprocess.PIPE)
            first = subprocess.run(
                ["git", "-C", str(origin), "rev-parse", "HEAD"],
                check=True,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            ).stdout.strip()
            (origin / "file.txt").write_text("second\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(origin), "commit", "-am", "second"], check=True, stdout=subprocess.PIPE)

            resolved = clone_repository(
                {"clone_url": origin.as_uri(), "branch": "main", "commit": first},
                checkout,
            )

        self.assertEqual(resolved, first.lower())

    def test_clone_repository_reports_git_stderr_on_failure(self) -> None:
        error = subprocess.CalledProcessError(
            128,
            ["git", "clone"],
            output="",
            stderr="remote: Repository not found.\nfatal: Authentication failed for 'https://github.com/acme/api.git/'",
        )
        with patch("pullwise_worker.main.subprocess.run", side_effect=error):
            with self.assertRaisesRegex(RuntimeError, "git clone failed: remote: Repository not found"):
                clone_repository(
                    {
                        "repo": "acme/api",
                        "branch": "main",
                        "commit": "pending",
                        "clone_url": "https://github.com/acme/api.git",
                    },
                    Path("checkout"),
                )

    def test_codex_provider_review_reports_model_without_token_usage(self) -> None:
        cfg = config()
        cfg.codex_model = "gpt-5.5"

        def fake_run(command: list[str], **_kwargs: object) -> Mock:
            output_path = Path(command[command.index("--output-last-message") + 1])
            output_path.write_text(json.dumps(audit_payload([])), encoding="utf-8")
            return Mock(
                returncode=0,
                stdout="",
                stderr="Review complete. Token usage: input=123 output=45 total=168",
            )

        with tempfile.TemporaryDirectory() as tmp, patch("pullwise_worker.main.subprocess.run", side_effect=fake_run):
            _payload, _summary, _logs, ai_usage = run_codex_provider_review(
                cfg,
                {"repo": "acme/api", "branch": "main", "commit": "pending"},
                Path(tmp),
            )

        self.assertEqual(
            ai_usage,
            {"model": "gpt-5.5"},
        )

    def test_codex_provider_review_invocations_are_serialized(self) -> None:
        cfg = config()
        entered = threading.Event()
        release = threading.Event()
        calls = []
        concurrent_entries = []
        in_run = 0
        run_lock = threading.Lock()

        def fake_run(command: list[str], **_kwargs: object) -> Mock:
            nonlocal in_run
            with run_lock:
                in_run += 1
                concurrent_entries.append(in_run)
            calls.append(command)
            try:
                entered.set()
                release.wait(timeout=5)
                output_path = Path(command[command.index("--output-last-message") + 1])
                output_path.write_text(json.dumps(audit_payload()), encoding="utf-8")
                return Mock(returncode=0, stdout="", stderr="")
            finally:
                with run_lock:
                    in_run -= 1

        with tempfile.TemporaryDirectory() as tmp, patch("pullwise_worker.main.subprocess.run", side_effect=fake_run):
            checkout_dir = Path(tmp)

            def run_call() -> tuple[dict, dict, str, dict]:
                return run_codex_provider_review(cfg, {"repo": "acme/api"}, checkout_dir)

            try:
                with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                    first = pool.submit(run_call)
                    self.assertTrue(entered.wait(timeout=5))
                    second = pool.submit(run_call)
                    time.sleep(0.05)
                    self.assertEqual(len(calls), 1)
                    release.set()
                    first.result(timeout=5)
                    second.result(timeout=5)
            finally:
                release.set()

        self.assertEqual(len(calls), 2)
        self.assertTrue(concurrent_entries)
        self.assertLessEqual(max(concurrent_entries), 1)

    def test_codex_auth_failure_cooldown_skips_next_process_launch(self) -> None:
        cfg = config()
        cfg.codex_auth_failure_cooldown_seconds = 3600
        auth_error = (
            "ERROR codex_api::endpoint::responses_websocket: failed to connect to websocket: "
            "HTTP error: 401 Unauthorized\n"
            "ERROR codex_login::auth::manager: Failed to refresh token: Your access token "
            "could not be refreshed because your refresh token was already used. "
            "Please log out and sign in again."
        )

        with tempfile.TemporaryDirectory() as tmp, patch(
            "pullwise_worker.main.subprocess.run",
            return_value=Mock(returncode=1, stdout="", stderr=auth_error),
        ) as run:
            with self.assertRaisesRegex(RuntimeError, "401 Unauthorized"):
                run_codex_provider_review(cfg, {"repo": "acme/api"}, Path(tmp))
            with self.assertRaisesRegex(RuntimeError, "temporarily disabled after auth failure"):
                run_codex_provider_review(cfg, {"repo": "acme/api"}, Path(tmp))

        self.assertEqual(run.call_count, 1)

    def test_run_codex_review_invokes_codex_exec_and_parses_audit_swarm_payload(self) -> None:
        def fake_run(command: list[str], **_kwargs: object) -> Mock:
            schema_path = Path(command[command.index("--output-schema") + 1])
            output_path = Path(command[command.index("--output-last-message") + 1])
            schema = json.loads(schema_path.read_text(encoding="utf-8"))
            self.assertEqual(schema["properties"]["issue_cards"]["maxItems"], 25)
            output_path.write_text(json.dumps(audit_payload([issue_card("Bug", severity="P2")])), encoding="utf-8")
            return Mock(returncode=0, stdout=json.dumps(audit_payload()), stderr="")

        with patch("pullwise_worker.main.subprocess.run", side_effect=fake_run) as run:
            payload, summary, _logs = run_codex_review(config(), {"repo": "acme/api"}, Path("checkout"))

        command = run.call_args.args[0]
        self.assertEqual(command[:2], ["codex", "exec"])
        self.assertIn("--skip-git-repo-check", command)
        self.assertIn("--ignore-user-config", command)
        self.assertEqual(command[command.index("--config") + 1], 'model_reasoning_effort="medium"')
        self.assertEqual(command[command.index("--model") + 1], "gpt-5.5")
        self.assertEqual(command[command.index("--sandbox") + 1], "read-only")
        self.assertIn("--output-schema", command)
        self.assertIn("--output-last-message", command)
        findings = audit_swarm_findings_from_payload(payload) or []
        self.assertEqual(findings[0]["title"], "Bug")
        self.assertEqual(summary["medium"], 1)

    def test_audit_swarm_output_schema_matches_codex_strict_structured_output_subset(self) -> None:
        def assert_strict_schema(schema: dict, path: str = "$") -> None:
            self.assertNotIn("oneOf", schema, path)
            schema_type = schema.get("type")
            if schema_type == "object" or (isinstance(schema_type, list) and "object" in schema_type):
                properties = schema.get("properties", {})
                self.assertIs(schema.get("additionalProperties"), False, path)
                self.assertEqual(set(schema.get("required", [])), set(properties), path)
                for name, child in properties.items():
                    assert_strict_schema(child, f"{path}.properties.{name}")
            elif schema_type == "array":
                assert_strict_schema(schema.get("items", {}), f"{path}.items")
            for keyword in ("anyOf", "allOf"):
                for index, child in enumerate(schema.get(keyword, [])):
                    assert_strict_schema(child, f"{path}.{keyword}[{index}]")

        assert_strict_schema(audit_swarm_output_schema())

    def test_worker_generated_audit_swarm_locations_match_output_schema(self) -> None:
        payload = audit_swarm_payload_from_findings(
            [
                {
                    "id": "deterministic-1",
                    "title": "Generated card",
                    "category": "Quality",
                    "severity": "medium",
                    "summary": "Generated finding.",
                    "file": "src/app.py",
                    "line": 7,
                    "verificationStatus": "static_proof",
                }
            ],
            verifier_role="verifier",
        )
        location = payload["issue_cards"][0]["locations"][0]
        location_schema = audit_swarm_output_schema()["properties"]["issue_cards"]["items"]["properties"]["locations"]["items"]

        self.assertEqual(set(location_schema["required"]) - set(location), set())
        self.assertEqual(location["lines"], "7")

    def test_worker_config_defaults_to_codex_only_provider_chain(self) -> None:
        cfg = config()

        self.assertEqual(cfg.provider_chain, ["codex"])
        self.assertEqual(cfg.codex_model, "gpt-5.5")
        self.assertEqual(cfg.codex_reasoning_effort, "medium")
        self.assertEqual(cfg.codex_auth_failure_cooldown_seconds, 3600)
        self.assertEqual(cfg.opencode_command, "opencode")
        self.assertEqual(cfg.opencode_model, "opencode/big-pickle")
        self.assertEqual(cfg.opencode_variant, "medium")

    def test_worker_config_rejects_remote_http_server_url_by_default(self) -> None:
        namespace = Namespace(
            server_url="http://server.test",
            worker_token="worker-token",
            worker_id="wk_1",
            max_concurrent_jobs=2,
            poll_seconds=1,
            work_dir=tempfile.mkdtemp(),
            checkout_root=None,
            log_dir=tempfile.mkdtemp(),
            provider="codex",
            codex_command="codex",
            codex_timeout_seconds=60,
        )

        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, "must use https"):
                WorkerConfig(namespace)

    def test_worker_config_allows_loopback_http_server_url(self) -> None:
        namespace = Namespace(
            server_url="http://127.0.0.1:8080",
            worker_token="worker-token",
            worker_id="wk_1",
            max_concurrent_jobs=2,
            poll_seconds=1,
            work_dir=tempfile.mkdtemp(),
            checkout_root=None,
            log_dir=tempfile.mkdtemp(),
            provider="codex",
            codex_command="codex",
            codex_timeout_seconds=60,
        )

        with patch.dict(os.environ, {}, clear=True):
            cfg = WorkerConfig(namespace)

        self.assertEqual(cfg.server_url, "http://127.0.0.1:8080")

    def test_worker_config_allows_remote_http_server_url_with_explicit_override(self) -> None:
        namespace = Namespace(
            server_url="http://server.test",
            worker_token="worker-token",
            worker_id="wk_1",
            max_concurrent_jobs=2,
            poll_seconds=1,
            work_dir=tempfile.mkdtemp(),
            checkout_root=None,
            log_dir=tempfile.mkdtemp(),
            provider="codex",
            codex_command="codex",
            codex_timeout_seconds=60,
        )

        with patch.dict(os.environ, {"PULLWISE_ALLOW_INSECURE_SERVER_URL": "true"}, clear=True):
            cfg = WorkerConfig(namespace)

        self.assertEqual(cfg.server_url, "http://server.test")

    def test_worker_config_reads_provider_chain_and_model_settings(self) -> None:
        namespace = Namespace(
            server_url="https://server.test",
            worker_token="worker-token",
            worker_id="wk_1",
            max_concurrent_jobs=2,
            poll_seconds=1,
            work_dir=tempfile.mkdtemp(),
            checkout_root=None,
            log_dir=tempfile.mkdtemp(),
            provider=None,
            codex_command=None,
            codex_timeout_seconds=60,
        )

        with patch.dict(
            "os.environ",
            {
                "PULLWISE_PROVIDER_CHAIN": "codex, opencode",
                "PULLWISE_CODEX_MODEL": "gpt-5.5",
                "PULLWISE_CODEX_REASONING_EFFORT": "high",
                "PULLWISE_CODEX_AUTH_FAILURE_COOLDOWN_SECONDS": "120",
                "PULLWISE_OPENCODE_COMMAND": "opencode-cli",
                "PULLWISE_OPENCODE_MODEL": "openai/gpt-5",
                "PULLWISE_OPENCODE_VARIANT": "xhigh",
            },
            clear=False,
        ):
            cfg = WorkerConfig(namespace)

        self.assertEqual(cfg.provider_chain, ["codex", "opencode"])
        self.assertEqual(cfg.codex_model, "gpt-5.5")
        self.assertEqual(cfg.codex_reasoning_effort, "high")
        self.assertEqual(cfg.codex_auth_failure_cooldown_seconds, 120)
        self.assertEqual(cfg.opencode_command, "opencode-cli")
        self.assertEqual(cfg.opencode_model, "openai/gpt-5")
        self.assertEqual(cfg.opencode_variant, "xhigh")

    def test_worker_config_defaults_provider_to_first_provider_chain_entry(self) -> None:
        namespace = Namespace(
            server_url="https://server.test",
            worker_token="worker-token",
            worker_id="wk_1",
            max_concurrent_jobs=2,
            poll_seconds=1,
            work_dir=tempfile.mkdtemp(),
            checkout_root=None,
            log_dir=tempfile.mkdtemp(),
            provider=None,
            codex_command=None,
            codex_timeout_seconds=60,
        )

        with patch.dict("os.environ", {"PULLWISE_PROVIDER_CHAIN": "opencode,codex"}, clear=True):
            cfg = WorkerConfig(namespace)

        self.assertEqual(cfg.provider_chain, ["opencode", "codex"])
        self.assertEqual(cfg.provider, "opencode")

    def test_run_codex_review_uses_configured_codex_model_and_effort(self) -> None:
        cfg = config()
        cfg.codex_model = "gpt-5.5"
        cfg.codex_reasoning_effort = "high"

        def fake_run(command: list[str], **_kwargs: object) -> Mock:
            output_path = Path(command[command.index("--output-last-message") + 1])
            output_path.write_text(json.dumps(audit_payload()), encoding="utf-8")
            return Mock(returncode=0, stdout="", stderr="")

        with patch("pullwise_worker.main.subprocess.run", side_effect=fake_run) as run:
            payload, _summary, _logs = run_codex_review(cfg, {"repo": "acme/api"}, Path("checkout"))

        command = run.call_args.args[0]
        self.assertEqual(command[command.index("--model") + 1], "gpt-5.5")
        self.assertIn('model_reasoning_effort="high"', command)
        self.assertEqual(payload["effectiveAgentConfig"]["provider"], "codex")
        self.assertEqual(payload["effectiveAgentConfig"]["cli"], "codex")
        self.assertEqual(payload["effectiveAgentConfig"]["model"], "gpt-5.5")
        self.assertEqual(payload["effectiveAgentConfig"]["reasoningEffort"], "high")

    def test_run_codex_review_falls_back_to_opencode_after_codex_failure(self) -> None:
        cfg = config()
        cfg.provider_chain = ["codex", "opencode"]
        cfg.opencode_command = "opencode"
        cfg.opencode_model = "openai/gpt-5"
        cfg.opencode_variant = "xhigh"

        def fake_run(command: list[str], **_kwargs: object) -> Mock:
            if command[:2] == ["codex", "exec"]:
                return Mock(returncode=1, stdout="", stderr="codex failed")
            self.assertEqual(command[:2], ["opencode", "run"])
            self.assertEqual(command[command.index("--model") + 1], "openai/gpt-5")
            self.assertEqual(command[command.index("--variant") + 1], "xhigh")
            return Mock(returncode=0, stdout=json.dumps(audit_payload([issue_card("Fallback", severity="low")])), stderr="")

        with patch("pullwise_worker.main.subprocess.run", side_effect=fake_run):
            payload, summary, logs = run_codex_review(cfg, {"repo": "acme/api"}, Path("checkout"))

        findings = audit_swarm_findings_from_payload(payload) or []
        self.assertEqual(findings[0]["title"], "Fallback")
        self.assertEqual(summary["low"], 1)
        self.assertIn("codex failed", logs)
        self.assertEqual(payload["effectiveAgentConfig"]["provider"], "opencode")
        self.assertEqual(payload["effectiveAgentConfig"]["cli"], "opencode")
        self.assertEqual(payload["effectiveAgentConfig"]["model"], "openai/gpt-5")
        self.assertEqual(payload["effectiveAgentConfig"]["reasoningEffort"], "xhigh")

    def test_run_codex_review_surfaces_codex_json_error_detail(self) -> None:
        cfg = config()
        codex_stderr = "\n".join(
            [
                "warning: Codex could not find bubblewrap on PATH.",
                'ERROR: {"type": "error", "message": "Sandbox helper failed to create the namespace", "code": "sandbox_unavailable"}',
            ]
        )

        with patch(
            "pullwise_worker.main.subprocess.run",
            return_value=Mock(returncode=1, stdout="", stderr=codex_stderr),
        ):
            with self.assertRaisesRegex(RuntimeError, "sandbox_unavailable.*Sandbox helper failed"):
                run_codex_review(cfg, {"repo": "acme/api"}, Path("checkout"))

    def test_job_checkout_dir_refuses_path_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp) / "work"

            self.assertEqual(checkout_dir_for_job(work_dir, "job_1"), (work_dir / "job_1").resolve())
            for job_id in ("../outside", "nested/job", "nested\\job", ".", "..", ""):
                with self.subTest(job_id=job_id):
                    with self.assertRaises(ValueError):
                        safe_job_id(job_id)
                    with self.assertRaises(ValueError):
                        checkout_dir_for_job(work_dir, job_id)

    def test_run_git_command_passes_configured_timeout_to_subprocess(self) -> None:
        with patch.dict(os.environ, {"PULLWISE_GIT_TIMEOUT_SECONDS": "17"}, clear=False), \
            patch("pullwise_worker.main.subprocess.run", return_value=Mock(returncode=0)) as run:
            run_git_command(["git", "status"], phase="status")

        self.assertEqual(run.call_args.kwargs["timeout"], 17)

    def test_redact_secrets_removes_worker_and_clone_tokens(self) -> None:
        cfg = config()
        text = "token worker-token clone https://x-access-token:short-token@github.com/acme/api.git"

        redacted = redact_secrets(text, cfg)

        self.assertNotIn("worker-token", redacted)
        self.assertNotIn("short-token", redacted)
        self.assertIn("[redacted]", redacted)
        self.assertIn("x-access-token:[redacted]@github.com", redacted)

    def test_run_doctor_checks_dependencies_capacity_paths_and_heartbeat(self) -> None:
        cfg = config()

        with patch(
                "pullwise_worker.main.command_ok",
                side_effect=[(True, "git ok"), (True, "v22.21.0"), (True, "codex ok"), (True, "active")],
            ), \
            patch("pullwise_worker.main.codex_ready_check", return_value=(False, "not logged in")), \
            patch("pullwise_worker.main.opencode_auth_check", return_value=(True, "authenticated for opencode")), \
            patch("pullwise_worker.main.PullwiseClient") as client_class:
            client_class.return_value.heartbeat.return_value = None
            ok = run_doctor(cfg)

        self.assertFalse(ok)
        client_class.return_value.heartbeat.assert_called_once()
        heartbeat_kwargs = client_class.return_value.heartbeat.call_args.kwargs
        self.assertEqual(heartbeat_kwargs["doctor_status"], "degraded")
        self.assertFalse(heartbeat_kwargs["codex_ready"])
        self.assertTrue(heartbeat_kwargs["systemd_active"])

    def test_main_service_commands_do_not_require_worker_token(self) -> None:
        for action in ("start", "stop", "status", "restart"):
            with self.subTest(action=action):
                with patch.dict(os.environ, {"PULLWISE_SERVER_URL": "https://server.test"}, clear=True), \
                    patch.object(sys, "argv", ["pullwise-worker", action, "--dry-run"]), \
                    patch("pullwise_worker.main.service_action", return_value=0) as service:
                    with self.assertRaises(SystemExit) as raised:
                        worker_main.main()

                self.assertEqual(raised.exception.code, 0)
                service.assert_called_once_with(action, dry_run=True)

    def test_main_update_cleanup_and_uninstall_do_not_require_worker_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"PULLWISE_SERVER_URL": "https://server.test"}, clear=True), \
                patch.object(sys, "argv", ["pullwise-worker", "update", "--dry-run"]), \
                patch("pullwise_worker.main.update_worker", return_value=0) as update:
                with self.assertRaises(SystemExit) as raised:
                    worker_main.main()

            self.assertEqual(raised.exception.code, 0)
            self.assertEqual(update.call_args.args[0].worker_token, "")

            with patch.dict(os.environ, {"PULLWISE_SERVER_URL": "https://server.test"}, clear=True), \
                patch.object(sys, "argv", ["pullwise-worker", "cleanup", "--work-dir", tmp]), \
                patch("pullwise_worker.main.cleanup_worker_resources") as cleanup:
                with self.assertRaises(SystemExit) as raised:
                    worker_main.main()

            self.assertEqual(raised.exception.code, 0)
            self.assertEqual(cleanup.call_args.args[0].worker_token, "")
            self.assertEqual(cleanup.call_args.args[0].work_dir, Path(tmp) / "pullwise-worker")

            with patch.dict(os.environ, {"PULLWISE_SERVER_URL": "https://server.test"}, clear=True), \
                patch.object(sys, "argv", ["pullwise-worker", "uninstall", "--remove-config", "--dry-run"]), \
                patch("pullwise_worker.main.uninstall_worker", return_value=0) as uninstall:
                with self.assertRaises(SystemExit) as raised:
                    worker_main.main()

            self.assertEqual(raised.exception.code, 0)
            uninstall.assert_called_once_with(remove_config=True, remove_logs=False, dry_run=True)

    def test_main_uninstall_unregisters_worker_before_local_cleanup(self) -> None:
        events = []
        client = Mock()
        client.delete.side_effect = lambda path: events.append(("delete", path))

        def local_uninstall(**kwargs):
            events.append(("uninstall", kwargs))
            return 0

        with patch.dict(
            os.environ,
            {
                "PULLWISE_SERVER_URL": "https://server.test",
                "PULLWISE_WORKER_TOKEN": "worker-token",
                "PULLWISE_WORKER_ID": "wk_1",
            },
            clear=True,
        ), patch.object(sys, "argv", ["pullwise-worker", "uninstall", "--remove-config"]), \
            patch("pullwise_worker.main.PullwiseClient", return_value=client), \
            patch("pullwise_worker.main.uninstall_worker", side_effect=local_uninstall):
            with self.assertRaises(SystemExit) as raised:
                worker_main.main()

        self.assertEqual(raised.exception.code, 0)
        self.assertEqual(events[0], ("delete", "/worker/registry"))
        self.assertEqual(events[1], ("uninstall", {"remove_config": True, "remove_logs": False, "dry_run": False}))

    def test_main_uninstall_aborts_local_cleanup_when_registry_unregister_fails(self) -> None:
        client = Mock()
        client.delete.side_effect = PullwiseRequestError("connection refused")

        with patch.dict(
            os.environ,
            {
                "PULLWISE_SERVER_URL": "https://server.test",
                "PULLWISE_WORKER_TOKEN": "worker-token",
                "PULLWISE_WORKER_ID": "wk_1",
            },
            clear=True,
        ), patch.object(sys, "argv", ["pullwise-worker", "uninstall"]), \
            patch("pullwise_worker.main.PullwiseClient", return_value=client), \
            patch("pullwise_worker.main.uninstall_worker", return_value=0) as local_uninstall:
            with self.assertRaises(SystemExit) as raised:
                worker_main.main()

        self.assertEqual(raised.exception.code, 1)
        local_uninstall.assert_not_called()

    def test_run_doctor_prints_device_auth_login_command_when_codex_is_not_ready(self) -> None:
        cfg = config()

        with patch(
                "pullwise_worker.main.command_ok",
                side_effect=[(True, "git ok"), (True, "v22.21.0"), (True, "codex ok"), (True, "active")],
            ), \
            patch("pullwise_worker.main.codex_ready_check", return_value=(False, "not logged in")), \
            patch("pullwise_worker.main.PullwiseClient") as client_class, \
            patch("builtins.print") as print_mock:
            client_class.return_value.heartbeat.return_value = None
            run_doctor(cfg)

        printed = "\n".join(str(call.args[0]) for call in print_mock.call_args_list if call.args)
        self.assertIn(CODEX_LOGIN_COMMAND, printed)
        self.assertIn("--device-auth", printed)

    def test_run_doctor_prints_opencode_auth_command_when_opencode_is_configured_but_missing(self) -> None:
        cfg = config()
        cfg.provider_chain = ["opencode"]

        with patch(
                "pullwise_worker.main.command_ok",
                side_effect=[(True, "git ok"), (False, "not found"), (True, "active")],
            ), \
            patch("pullwise_worker.main.PullwiseClient") as client_class, \
            patch("builtins.print") as print_mock:
            client_class.return_value.heartbeat.return_value = None
            run_doctor(cfg)

        printed = "\n".join(str(call.args[0]) for call in print_mock.call_args_list if call.args)
        self.assertIn(OPENCODE_AUTH_COMMAND, printed)
        self.assertIn("auth login --provider opencode", printed)

    def test_opencode_auth_command_uses_provider_from_model_prefix(self) -> None:
        cfg = config()
        cfg.opencode_model = "deepseek/deepseek-v4-pro"

        command = worker_main.opencode_auth_command(cfg)

        self.assertIn("opencode auth login --provider deepseek", command)

    def test_opencode_auth_check_requires_current_model_provider(self) -> None:
        cfg = config()
        cfg.opencode_model = "minimax/MiniMax-M3"
        completed = Mock(returncode=0, stdout="deepseek\n", stderr="")

        with patch("pullwise_worker.main.subprocess.run", return_value=completed):
            ok, detail = worker_main.opencode_auth_check(cfg)

        self.assertFalse(ok)
        self.assertEqual(detail, "not authenticated for minimax")

    def test_run_doctor_reports_ready_when_codex_probe_succeeds(self) -> None:
        cfg = config()

        with patch("pullwise_worker.main.command_ok", side_effect=[(True, "git ok"), (True, "v22.21.0"), (True, "codex ok"), (True, "active")]), \
            patch("pullwise_worker.main.codex_ready_check", return_value=(True, "ready")), \
            patch("pullwise_worker.main.PullwiseClient") as client_class:
            client_class.return_value.heartbeat.return_value = None
            ok = run_doctor(cfg)

        self.assertTrue(ok)
        heartbeat_kwargs = client_class.return_value.heartbeat.call_args.kwargs
        self.assertEqual(heartbeat_kwargs["doctor_status"], "ok")
        self.assertTrue(heartbeat_kwargs["codex_ready"])

    def test_run_doctor_sends_codex_not_ready_when_opencode_fallback_is_ready(self) -> None:
        cfg = config()
        cfg.provider_chain = ["codex", "opencode"]

        with patch(
                "pullwise_worker.main.command_ok",
                side_effect=[
                    (True, "git ok"),
                    (True, "v22.21.0"),
                    (True, "codex ok"),
                    (True, "opencode 1.0.0"),
                    (True, "active"),
                ],
            ), \
            patch("pullwise_worker.main.codex_ready_check", return_value=(False, "not logged in")), \
            patch("pullwise_worker.main.opencode_auth_check", return_value=(True, "authenticated for opencode")), \
            patch("pullwise_worker.main.PullwiseClient") as client_class:
            client_class.return_value.heartbeat.return_value = None
            ok = run_doctor(cfg)

        self.assertTrue(ok)
        heartbeat_kwargs = client_class.return_value.heartbeat.call_args.kwargs
        self.assertEqual(heartbeat_kwargs["doctor_status"], "ok")
        self.assertFalse(heartbeat_kwargs["codex_ready"])

    def test_codex_ready_check_identifies_login_failure(self) -> None:
        cfg = config()
        completed = Mock(returncode=1, stdout="", stderr="Reading additional input from stdin...\nnot authenticated; run codex login")

        with patch("pullwise_worker.main.subprocess.run", return_value=completed):
            ok, detail = codex_ready_check(cfg)

        self.assertFalse(ok)
        self.assertEqual(detail, "not logged in")

    def test_codex_ready_check_retries_after_cached_auth_failure(self) -> None:
        cfg = config()
        worker_main.mark_codex_auth_failure(cfg, "not authenticated; run codex login")
        completed = Mock(returncode=0, stdout='{"ok": true}', stderr="")

        with patch("pullwise_worker.main.subprocess.run", return_value=completed) as run:
            ok, detail = codex_ready_check(cfg)

        self.assertTrue(ok)
        self.assertEqual(detail, "ready")
        run.assert_called_once()

    def test_codex_ready_check_defers_when_codex_invocation_is_running(self) -> None:
        cfg = config()
        self.assertTrue(worker_main._CODEX_EXEC_LOCK.acquire(blocking=False))
        try:
            with patch("pullwise_worker.main.subprocess.run") as run:
                ok, detail = codex_ready_check(cfg)
        finally:
            worker_main._CODEX_EXEC_LOCK.release()

        self.assertTrue(ok)
        self.assertIn("deferred", detail)
        run.assert_not_called()

    def test_codex_ready_check_skips_git_repo_trust_check(self) -> None:
        cfg = config()
        completed = Mock(returncode=0, stdout='{"ok": true}', stderr="")

        with patch("pullwise_worker.main.subprocess.run", return_value=completed) as run:
            ok, detail = codex_ready_check(cfg)

        command = run.call_args.args[0]
        self.assertTrue(ok)
        self.assertEqual(detail, "ready")
        self.assertEqual(command[:2], ["codex", "exec"])
        self.assertIn("--skip-git-repo-check", command)
        self.assertIn("--ignore-user-config", command)
        self.assertIn("--json", command)
        self.assertEqual(command[command.index("--sandbox") + 1], "read-only")
        self.assertIn('model_reasoning_effort="medium"', command)
        self.assertEqual(command[command.index("--model") + 1], "gpt-5.5")

    def test_node_version_check_requires_node_20(self) -> None:
        with patch("pullwise_worker.main.command_ok", return_value=(True, "v12.22.9")):
            ok, detail = node_version_check()

        self.assertFalse(ok)
        self.assertEqual(detail, "Node.js 20+ required, found v12.22.9")

    def test_codex_ready_check_reports_codex_node_runtime_failure(self) -> None:
        cfg = config()
        completed = Mock(
            returncode=1,
            stdout="",
            stderr=(
                "file:///usr/local/lib/node_modules/@openai/codex/bin/codex.js:213\n"
                "const childResult = await new Promise((resolve) => {\n"
                "SyntaxError: Unexpected reserved word"
            ),
        )

        with patch("pullwise_worker.main.subprocess.run", return_value=completed), \
            patch("pullwise_worker.main.node_version_check", return_value=(False, "Node.js 20+ required, found v12.22.9")):
            ok, detail = codex_ready_check(cfg)

        self.assertFalse(ok)
        self.assertEqual(detail, "Node.js 20+ required, found v12.22.9")

    def test_cleanup_checkouts_removes_expired_failed_retention(self) -> None:
        cfg = config()
        mark_checkout_root_owned(cfg)
        cfg.max_checkout_bytes = 1024 * 1024
        retained = Path(cfg.work_dir) / "retained"
        expired = Path(cfg.work_dir) / "expired"
        retained.mkdir(parents=True)
        expired.mkdir(parents=True)
        (retained / "big.txt").write_text("xx", encoding="utf-8")
        (expired / "file.txt").write_text("x", encoding="utf-8")
        retained.with_suffix(".failed-retain").write_text("9999999999", encoding="utf-8")
        expired.with_suffix(".failed-retain").write_text("1", encoding="utf-8")

        cleanup_checkouts(cfg)

        self.assertFalse(expired.exists())
        self.assertFalse(expired.with_suffix(".failed-retain").exists())
        self.assertTrue(retained.exists())
        self.assertTrue(Path(cfg.work_dir).exists())

    def test_cleanup_checkouts_skips_active_jobs_and_removes_oldest_over_budget(self) -> None:
        cfg = config()
        mark_checkout_root_owned(cfg)
        cfg.max_checkout_bytes = 5
        active = Path(cfg.work_dir) / "active_job"
        old = Path(cfg.work_dir) / "old_job"
        active.mkdir(parents=True)
        old.mkdir(parents=True)
        (active / "big.txt").write_text("xxxxx", encoding="utf-8")
        (old / "big.txt").write_text("xxxxx", encoding="utf-8")
        os.utime(old, (1, 1))
        os.utime(active, (2, 2))

        cleanup_checkouts(cfg, active_job_ids={"active_job"})

        self.assertTrue(active.exists())
        self.assertFalse(old.exists())

    def test_cleanup_checkouts_preserves_verifier_scratch_dirs_during_active_jobs(self) -> None:
        cfg = config()
        mark_checkout_root_owned(cfg)
        cfg.max_checkout_bytes = 1
        active = Path(cfg.work_dir) / "active_job"
        old = Path(cfg.work_dir) / "old_job"
        verifier_home = Path(cfg.work_dir) / ".verifier-home"
        verifier_tmp = Path(cfg.work_dir) / ".verifier-tmp"
        for path in (active, old, verifier_home, verifier_tmp):
            path.mkdir(parents=True)
            (path / "big.txt").write_text("xxxxx", encoding="utf-8")
        os.utime(verifier_home, (1, 1))
        os.utime(verifier_tmp, (2, 2))
        os.utime(old, (3, 3))
        os.utime(active, (4, 4))

        cleanup_checkouts(cfg, active_job_ids={"active_job"})

        self.assertTrue(active.exists())
        self.assertTrue(verifier_home.exists())
        self.assertTrue(verifier_tmp.exists())
        self.assertFalse(old.exists())

    def test_cleanup_checkouts_requires_owned_checkout_root_before_deleting(self) -> None:
        cfg = config()
        cfg.max_checkout_bytes = 1
        unrelated = Path(cfg.work_dir) / "unrelated"
        unrelated.mkdir(parents=True)
        (unrelated / "big.txt").write_text("xxxxx", encoding="utf-8")

        cleanup_checkouts(cfg)

        self.assertTrue(unrelated.exists())

    def test_cleanup_worker_resources_prunes_recursive_verifier_logs(self) -> None:
        cfg = config()
        cfg.log_retention_seconds = 60
        cfg.max_log_bytes = 8
        expired = Path(cfg.log_dir) / "verification" / "old_job" / "test.log"
        active = Path(cfg.log_dir) / "verification" / "active_job" / "test.log"
        newest = Path(cfg.log_dir) / "verification" / "new_job" / "test.log"
        for path, content in ((expired, "expired"), (active, "active-log"), (newest, "new-log")):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        old_time = int(time.time()) - 3600
        os.utime(expired, (old_time, old_time))

        cleanup_worker_resources(cfg, active_job_ids={"active_job"})

        self.assertFalse(expired.exists())
        self.assertTrue(active.exists())
        self.assertFalse(newest.exists())

    def test_lifecycle_uninstall_dry_run_does_not_remove_files(self) -> None:
        with patch("pullwise_worker.main.subprocess.run") as run:
            code = uninstall_worker(remove_config=True, remove_logs=True, dry_run=True)

        self.assertEqual(code, 0)
        run.assert_not_called()

    def test_update_dry_run_backs_up_env_and_does_not_run_commands(self) -> None:
        with patch("pullwise_worker.main.subprocess.run") as run:
            code = update_worker(config(), dry_run=True)

        self.assertEqual(code, 0)
        run.assert_not_called()

    def test_update_uses_installed_service_interpreter(self) -> None:
        cfg = config()
        expected_package = default_worker_package()
        with patch.dict("os.environ", {"PULLWISE_PYTHON_BIN": "/custom/python"}, clear=False), \
            patch("pullwise_worker.main.subprocess.run") as run, \
            patch("builtins.print") as print_mock:
            code = update_worker(cfg, dry_run=True)

        printed = "\n".join(str(call.args[0]) for call in print_mock.call_args_list if call.args)
        self.assertEqual(code, 0)
        self.assertIn(
            f"/custom/python -m pip install --upgrade --force-reinstall --no-cache-dir {expected_package}",
            printed,
        )
        run.assert_not_called()

    def test_update_falls_back_to_python3_when_service_interpreter_is_missing(self) -> None:
        cfg = config()
        expected_package = default_worker_package()

        with patch.dict(
                "os.environ",
                {"PULLWISE_WORKER_ENV_FILE": "/tmp/worker.env", "PULLWISE_WORKER_ENV_BACKUP_FILE": "/tmp/worker.env.bak"},
                clear=True,
            ), \
            patch("pullwise_worker.main.subprocess.run") as run, \
            patch("builtins.print") as print_mock:
            code = update_worker(cfg, dry_run=True)

        printed = "\n".join(str(call.args[0]) for call in print_mock.call_args_list if call.args)
        self.assertEqual(code, 0)
        self.assertIn(
            f"python3 -m pip install --upgrade --force-reinstall --no-cache-dir {expected_package}",
            printed,
        )
        run.assert_not_called()

    def test_update_dry_run_restarts_service_before_running_doctor(self) -> None:
        with patch("pullwise_worker.main.subprocess.run") as run, \
            patch("builtins.print") as print_mock:
            code = update_worker(config(), dry_run=True)

        printed = [str(call.args[0]) for call in print_mock.call_args_list if call.args]
        doctor_command = next(item for item in printed if item.endswith(" doctor"))
        self.assertEqual(code, 0)
        self.assertIn("runuser -u pullwise-worker -- env HOME=/var/lib/pullwise-worker", doctor_command)
        self.assertIn("/usr/local/bin/pullwise-worker doctor", doctor_command)
        self.assertLess(printed.index("systemctl restart pullwise-worker"), printed.index(doctor_command))
        run.assert_not_called()

    def test_update_rewrites_env_loading_wrapper_after_package_upgrade(self) -> None:
        cfg = config()
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / "worker.env"
            backup_file = Path(tmp) / "worker.env.bak"
            bin_path = Path(tmp) / "pullwise-worker"
            env_file.write_text("PULLWISE_WORKER_TOKEN=worker-token\n", encoding="utf-8")

            with patch.dict(
                    "os.environ",
                    {
                        "PULLWISE_WORKER_ENV_FILE": str(env_file),
                        "PULLWISE_WORKER_ENV_BACKUP_FILE": str(backup_file),
                        "PULLWISE_WORKER_BIN_PATH": str(bin_path),
                    },
                    clear=False,
                ), \
                patch("pullwise_worker.main.subprocess.run", return_value=Mock(returncode=0)):
                code = update_worker(cfg)

            self.assertEqual(code, 0)
            wrapper = bin_path.read_text(encoding="utf-8")
            self.assertIn("load_worker_env", wrapper)
            self.assertIn(str(env_file), wrapper)

    def test_update_restores_existing_env_when_upgrade_fails(self) -> None:
        cfg = config()
        expected_package = default_worker_package()
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / "worker.env"
            backup_file = Path(tmp) / "worker.env.bak"
            env_file.write_text("PULLWISE_WORKER_TOKEN=worker-token\n", encoding="utf-8")
            failed = Mock(returncode=1)
            ok = Mock(returncode=0)

            with patch.dict(
                    "os.environ",
                    {
                        "PULLWISE_WORKER_ENV_FILE": str(env_file),
                        "PULLWISE_WORKER_ENV_BACKUP_FILE": str(backup_file),
                    },
                    clear=False,
                ), \
                patch("pullwise_worker.main.subprocess.run", side_effect=[ok, failed, ok]) as run:
                code = update_worker(cfg)

            self.assertEqual(code, 1)
            self.assertEqual(
                run.call_args_list[1].args[0],
                [
                    "python3",
                    "-m",
                    "pip",
                    "install",
                    "--upgrade",
                    "--force-reinstall",
                    "--no-cache-dir",
                    expected_package,
                ],
            )
            self.assertEqual(env_file.read_text(encoding="utf-8"), "PULLWISE_WORKER_TOKEN=worker-token\n")
            self.assertEqual(backup_file.read_text(encoding="utf-8"), "PULLWISE_WORKER_TOKEN=worker-token\n")

    def test_service_action_supports_systemd_start_stop_status_restart(self) -> None:
        for action in ("start", "stop", "status", "restart"):
            with self.subTest(action=action):
                with patch("pullwise_worker.main.subprocess.run") as run:
                    self.assertEqual(service_action(action, dry_run=True), 0)
                run.assert_not_called()

    def test_lifecycle_stop_exits_without_systemd_authorization(self) -> None:
        with patch("pullwise_worker.main.service_action", return_value=0) as service:
            self.assertEqual(execute_lifecycle_command("stop"), 0)

        service.assert_not_called()

    def test_lifecycle_uninstall_without_systemd_authorization_fails(self) -> None:
        with patch("pullwise_worker.main.uninstall_worker", return_value=1) as uninstall, \
            patch("pullwise_worker.main.service_action", return_value=1) as service:
            self.assertEqual(execute_lifecycle_command("uninstall"), 2)

        uninstall.assert_not_called()
        service.assert_not_called()

    def test_remote_lifecycle_uninstall_reports_failed_not_succeeded(self) -> None:
        worker = Worker(config())
        worker.client = Mock()

        handled = worker.handle_lifecycle_command({"id": "cmd_uninstall", "command": "uninstall"})

        self.assertFalse(handled)
        self.assertEqual(worker.client.command_status.call_args_list[0].args, ("cmd_uninstall", "running"))
        self.assertEqual(worker.client.command_status.call_args_list[1].args, ("cmd_uninstall", "failed"))
        self.assertEqual(
            worker.client.command_status.call_args_list[1].kwargs,
            {"error": "uninstall command exited 2"},
        )

    def test_write_scan_summary_redacts_tokens(self) -> None:
        cfg = config()
        write_scan_summary(cfg, "job_1", "failed", 12, "worker-token https://x-access-token:repo-token@github.com/acme/api.git")

        summary_log = Path(cfg.log_dir) / "scan-summary.log"
        content = summary_log.read_text(encoding="utf-8")
        self.assertNotIn("worker-token", content)
        self.assertNotIn("repo-token", content)
        self.assertIn("[redacted]", content)

    def test_safe_rmtree_refuses_non_worker_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            allowed = Path(tmp) / "allowed"
            target.mkdir()
            allowed.mkdir()

            with self.assertRaises(ValueError):
                safe_rmtree(target, allowed)
            self.assertTrue(target.exists())

    def test_safe_rmtree_refuses_symlinked_allowed_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            allowed = Path(tmp) / "allowed"
            allowed.mkdir()

            with patch.object(Path, "is_symlink", return_value=True):
                with self.assertRaises(ValueError):
                    safe_rmtree(allowed, allowed)
            self.assertTrue(allowed.exists())

    def test_ci_dependency_bounds_keep_python_39_support(self) -> None:
        root = Path(__file__).resolve().parents[1]
        workflow = (root / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
        audit_requirements = (root / "requirements-audit.txt").read_text(encoding="utf-8")

        self.assertIn('"pip>=25.3,<26.1"', workflow)
        self.assertIn('"pip-audit>=2.9,<2.10"', workflow)
        self.assertIn('"filelock>=3.19.1,<3.20"', workflow)
        self.assertIn('python -m unittest discover -s tests -p "test_*.py"', workflow)
        self.assertIn("dependencies = []", pyproject)
        self.assertIn("no third-party runtime dependencies", audit_requirements)
        self.assertNotIn("pip>=26.1", workflow)
        self.assertNotIn("filelock>=3.20.3", workflow)
        self.assertNotIn("requests", pyproject)

    def test_deploy_assets_cover_service_env_logrotate_and_lifecycle(self) -> None:
        deploy_root = Path(__file__).resolve().parents[1] / "deploy"
        expected = [
            "worker.env.template",
            "pullwise-worker.service",
            "logrotate.conf",
            "cleanup-checkouts.sh",
            "update-worker.sh",
            "restart-worker.sh",
            "uninstall-worker.sh",
        ]

        for name in expected:
            self.assertTrue((deploy_root / name).exists(), name)
        self.assertFalse(
            (deploy_root / "install-worker.sh").exists(),
            "server /install-worker.sh is the only supported worker installer",
        )
        env_template = (deploy_root / "worker.env.template").read_text(encoding="utf-8")
        service = (deploy_root / "pullwise-worker.service").read_text(encoding="utf-8")
        self.assertIn("PULLWISE_CODEX_MODEL=gpt-5.5", env_template)
        self.assertIn("PULLWISE_CODEX_REASONING_EFFORT=medium", env_template)
        self.assertIn("PULLWISE_OPENCODE_MODEL=opencode/big-pickle", env_template)
        self.assertIn("PULLWISE_OPENCODE_VARIANT=medium", env_template)
        self.assertIn("PULLWISE_REVIEW_CALIBRATION_MODE=shadow", env_template)
        self.assertIn("PULLWISE_REVIEW_CALIBRATION_MODEL=relative_factor", env_template)
        self.assertIn("PULLWISE_REVIEW_CALIBRATION_HALF_LIFE_DAYS=45", env_template)
        self.assertIn("PULLWISE_REVIEW_CALIBRATION_MIN_EFFECTIVE_SAMPLES=20", env_template)
        self.assertIn("PULLWISE_REVIEW_CALIBRATION_ENABLE_BUCKETS=false", env_template)
        self.assertIn("PULLWISE_REVIEW_CALIBRATION_ENABLE_HIERARCHY=false", env_template)
        self.assertIn("PULLWISE_REVIEW_CALIBRATION_ENABLE_DRIFT=false", env_template)
        self.assertIn("PULLWISE_REVIEW_CALIBRATION_SAMPLE_AUDIT_RATE=0.02", env_template)
        self.assertIn("PULLWISE_REVIEW_CALIBRATION_BORDERLINE_SAMPLE_WINDOW=0.03", env_template)
        for key in (
            "PULLWISE_PROVIDER_CHAIN",
            "PULLWISE_CODEX_MODEL",
            "PULLWISE_CODEX_REASONING_EFFORT",
            "PULLWISE_OPENCODE_COMMAND",
            "PULLWISE_OPENCODE_MODEL",
            "PULLWISE_OPENCODE_VARIANT",
            "PULLWISE_REVIEW_CALIBRATION_MODE",
            "PULLWISE_REVIEW_CALIBRATION_MODEL",
            "PULLWISE_REVIEW_CALIBRATION_HALF_LIFE_DAYS",
            "PULLWISE_REVIEW_CALIBRATION_MIN_EFFECTIVE_SAMPLES",
            "PULLWISE_REVIEW_CALIBRATION_ENABLE_BUCKETS",
            "PULLWISE_REVIEW_CALIBRATION_ENABLE_HIERARCHY",
            "PULLWISE_REVIEW_CALIBRATION_ENABLE_DRIFT",
            "PULLWISE_REVIEW_CALIBRATION_SAMPLE_AUDIT_RATE",
            "PULLWISE_REVIEW_CALIBRATION_BORDERLINE_SAMPLE_WINDOW",
        ):
            self.assertIn(key, env_template)
        self.assertIn("Restart=on-failure", service)
        self.assertNotIn("Restart=always", service)
        self.assertIn("ReadWritePaths=/var/lib/pullwise-worker /var/log/pullwise-worker", service)


if __name__ == "__main__":
    unittest.main()
