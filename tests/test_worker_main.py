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
    PullwiseClient,
    PullwiseHTTPError,
    PullwiseRequestError,
    Worker,
    WorkerConfig,
    audit_swarm_findings_from_payload,
    audit_swarm_output_schema,
    audit_swarm_scan_artifacts,
    checkout_dir_for_job,
    cleanup_checkouts,
    cleanup_worker_resources,
    clone_repository,
    collect_preflight_metadata,
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
    worker_readiness_checks,
    write_scan_summary,
)


def audit_payload(issue_cards: list[dict] | None = None, verification_results: list[dict] | None = None) -> dict:
    return {
        "audit_protocol": "audit-swarm/0.1",
        "issue_cards": issue_cards or [],
        "verification_results": verification_results or [],
    }


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
        "false_positive_checks": ["Check for upstream guard."],
    }


def config() -> WorkerConfig:
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
        self.assertEqual(preflight["environment"]["checkoutRoot"], str(checkout_dir))
        self.assertIn("worker environment", preflight["summary"])
        self.assertIn("no project scripts were executed", preflight["summary"])

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
                {"title": "Precise code finding", "file": "src/app.py", "line": 12},
                {"title": "Repro command finding", "reproduction": {"commands": ["npm test"]}},
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
            audit_with_usage = audit_payload([issue_card("Bug", severity="P1", issue_id="bug")])
            audit_with_usage["ai_usage"] = {
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

            worker.run_job({"job_id": "job_1", "attempt": 2, "repo": "acme/api", "commit": "pending"})

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
        self.assertEqual(
            result_payload["ai_usage"],
            {"model": "gpt-5.5"},
        )
        self.assertEqual(result_payload["verification_audit"]["candidateCount"], 1)
        self.assertEqual(result_payload["verification_audit"]["reportedCount"], 1)
        self.assertEqual(result_payload["verification_audit"]["rejectedCount"], 0)
        self.assertEqual(result_payload["result_checksum"], result_checksum({k: v for k, v in result_payload.items() if k != "result_checksum"}))
        self.assertGreaterEqual(worker.client.progress.call_count, 3)
        rmtree.assert_called_with(checkout_dir, ignore_errors=True)

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
            worker.run_job({"job_id": "job_verifier_error", "attempt": 1, "repo": "acme/api"})

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
            worker.run_job({"job_id": "job_verifier_findings", "attempt": 1, "repo": "acme/api"})

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
            worker.run_job({"job_id": "job_retry", "attempt": 1, "repo": "acme/api"})

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
            worker.run_job({"job_id": "job_success_after_failure", "attempt": 1, "repo": "acme/api"})

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
            worker.run_job({"job_id": "job_timeout", "attempt": 1, "repo": "acme/api"})

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
            worker.run_job({"job_id": "job_http_retry", "attempt": 1, "repo": "acme/api"})

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
        self.assertEqual(worker.next_poll_sleep(claimed_jobs=1, loop_error=False), 5)
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
        self.assertEqual(request.full_url, "http://server.test/worker/heartbeat")
        self.assertEqual(request.get_header("Authorization"), "Bearer worker-token")
        self.assertEqual(json.loads(request.data.decode("utf-8")), {"worker_id": "wk_1"})
        self.assertEqual(response.json(), {"ok": True})

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
            patch("pullwise_worker.main.shutil.disk_usage", return_value=Mock(free=2 * 1024 * 1024 * 1024)):
            checks, provider_ready = worker_readiness_checks(cfg)

        by_name = {name: (ok, detail) for name, ok, detail in checks}
        self.assertFalse(by_name["codex_ready"][0])
        self.assertTrue(by_name["opencode"][0])
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
                    "clone_token": {"token": "short-token"},
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
        self.assertEqual(clone_env["GIT_CONFIG_KEY_0"], "http.extraHeader")

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

    def test_worker_config_defaults_to_codex_only_provider_chain(self) -> None:
        cfg = config()

        self.assertEqual(cfg.provider_chain, ["codex"])
        self.assertEqual(cfg.codex_model, "gpt-5.5")
        self.assertEqual(cfg.codex_reasoning_effort, "medium")
        self.assertEqual(cfg.codex_auth_failure_cooldown_seconds, 3600)
        self.assertEqual(cfg.opencode_command, "opencode")
        self.assertEqual(cfg.opencode_model, "opencode/big-pickle")
        self.assertEqual(cfg.opencode_variant, "medium")

    def test_worker_config_reads_provider_chain_and_model_settings(self) -> None:
        namespace = Namespace(
            server_url="http://server.test",
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

    def test_run_codex_review_uses_configured_codex_model_and_effort(self) -> None:
        cfg = config()
        cfg.codex_model = "gpt-5.5"
        cfg.codex_reasoning_effort = "high"

        def fake_run(command: list[str], **_kwargs: object) -> Mock:
            output_path = Path(command[command.index("--output-last-message") + 1])
            output_path.write_text(json.dumps(audit_payload()), encoding="utf-8")
            return Mock(returncode=0, stdout="", stderr="")

        with patch("pullwise_worker.main.subprocess.run", side_effect=fake_run) as run:
            run_codex_review(cfg, {"repo": "acme/api"}, Path("checkout"))

        command = run.call_args.args[0]
        self.assertEqual(command[command.index("--model") + 1], "gpt-5.5")
        self.assertIn('model_reasoning_effort="high"', command)

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
                with patch.dict(os.environ, {"PULLWISE_SERVER_URL": "http://server.test"}, clear=True), \
                    patch.object(sys, "argv", ["pullwise-worker", action, "--dry-run"]), \
                    patch("pullwise_worker.main.service_action", return_value=0) as service:
                    with self.assertRaises(SystemExit) as raised:
                        worker_main.main()

                self.assertEqual(raised.exception.code, 0)
                service.assert_called_once_with(action, dry_run=True)

    def test_main_update_cleanup_and_uninstall_do_not_require_worker_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"PULLWISE_SERVER_URL": "http://server.test"}, clear=True), \
                patch.object(sys, "argv", ["pullwise-worker", "update", "--dry-run"]), \
                patch("pullwise_worker.main.update_worker", return_value=0) as update:
                with self.assertRaises(SystemExit) as raised:
                    worker_main.main()

            self.assertEqual(raised.exception.code, 0)
            self.assertEqual(update.call_args.args[0].worker_token, "")

            with patch.dict(os.environ, {"PULLWISE_SERVER_URL": "http://server.test"}, clear=True), \
                patch.object(sys, "argv", ["pullwise-worker", "cleanup", "--work-dir", tmp]), \
                patch("pullwise_worker.main.cleanup_worker_resources") as cleanup:
                with self.assertRaises(SystemExit) as raised:
                    worker_main.main()

            self.assertEqual(raised.exception.code, 0)
            self.assertEqual(cleanup.call_args.args[0].worker_token, "")
            self.assertEqual(cleanup.call_args.args[0].work_dir, Path(tmp) / "pullwise-worker")

            with patch.dict(os.environ, {"PULLWISE_SERVER_URL": "http://server.test"}, clear=True), \
                patch.object(sys, "argv", ["pullwise-worker", "uninstall", "--remove-config", "--dry-run"]), \
                patch("pullwise_worker.main.uninstall_worker", return_value=0) as uninstall:
                with self.assertRaises(SystemExit) as raised:
                    worker_main.main()

            self.assertEqual(raised.exception.code, 0)
            uninstall.assert_called_once_with(remove_config=True, remove_logs=False, dry_run=True)

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
        self.assertIn(f"/custom/python -m pip install --upgrade {expected_package}", printed)
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
        self.assertIn(f"python3 -m pip install --upgrade {expected_package}", printed)
        run.assert_not_called()

    def test_update_dry_run_restarts_service_before_running_doctor(self) -> None:
        with patch("pullwise_worker.main.subprocess.run") as run, \
            patch("builtins.print") as print_mock:
            code = update_worker(config(), dry_run=True)

        printed = [str(call.args[0]) for call in print_mock.call_args_list if call.args]
        self.assertEqual(code, 0)
        self.assertLess(printed.index("systemctl restart pullwise-worker"), printed.index("pullwise-worker doctor"))
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

    def test_lifecycle_uninstall_exits_without_systemd_authorization(self) -> None:
        with patch("pullwise_worker.main.uninstall_worker", return_value=1) as uninstall, \
            patch("pullwise_worker.main.service_action", return_value=1) as service:
            self.assertEqual(execute_lifecycle_command("uninstall"), 0)

        uninstall.assert_not_called()
        service.assert_not_called()

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

    def test_deploy_assets_cover_install_systemd_logrotate_and_lifecycle(self) -> None:
        deploy_root = Path(__file__).resolve().parents[1] / "deploy"
        expected = [
            "install-worker.sh",
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
        install_script = (deploy_root / "install-worker.sh").read_text(encoding="utf-8")
        env_template = (deploy_root / "worker.env.template").read_text(encoding="utf-8")
        service = (deploy_root / "pullwise-worker.service").read_text(encoding="utf-8")
        self.assertIn("PULLWISE_WORKER_PACKAGE", install_script)
        self.assertIn(f'DEFAULT_WORKER_VERSION="{__version__}"', install_script)
        self.assertIn("https://github.com/GoPullwise/pullwise-worker/releases/download/v${DEFAULT_WORKER_VERSION}/pullwise_worker-${DEFAULT_WORKER_VERSION}-py3-none-any.whl", install_script)
        self.assertNotIn("pullwise-worker==0.1.0", install_script)
        self.assertIn("PULLWISE_CODEX_PACKAGE", install_script)
        self.assertIn("@openai/codex@0.135.0", install_script)
        self.assertIn("--codex-package", install_script)
        self.assertIn("--provider-chain", install_script)
        self.assertIn('write_env_value PULLWISE_CODEX_MODEL "${PULLWISE_CODEX_MODEL:-gpt-5.5}"', install_script)
        self.assertIn('write_env_value PULLWISE_CODEX_REASONING_EFFORT "${PULLWISE_CODEX_REASONING_EFFORT:-medium}"', install_script)
        self.assertIn('write_env_value PULLWISE_OPENCODE_MODEL "${PULLWISE_OPENCODE_MODEL:-opencode/big-pickle}"', install_script)
        self.assertIn('write_env_value PULLWISE_OPENCODE_VARIANT "${PULLWISE_OPENCODE_VARIANT:-medium}"', install_script)
        self.assertIn("PULLWISE_CODEX_MODEL=gpt-5.5", env_template)
        self.assertIn("PULLWISE_CODEX_REASONING_EFFORT=medium", env_template)
        self.assertIn("PULLWISE_OPENCODE_MODEL=opencode/big-pickle", env_template)
        self.assertIn("PULLWISE_OPENCODE_VARIANT=medium", env_template)
        for key in (
            "PULLWISE_PROVIDER_CHAIN",
            "PULLWISE_CODEX_MODEL",
            "PULLWISE_CODEX_REASONING_EFFORT",
            "PULLWISE_OPENCODE_COMMAND",
            "PULLWISE_OPENCODE_MODEL",
            "PULLWISE_OPENCODE_VARIANT",
        ):
            self.assertIn(key, install_script)
            self.assertIn(key, env_template)
        self.assertIn("uname -s", install_script)
        self.assertIn("uname -m", install_script)
        self.assertIn("need_cmd python3", install_script)
        self.assertIn("Python 3.9 or newer", install_script)
        self.assertIn("need_cmd git", install_script)
        self.assertIn("Node.js 20+ is required", install_script)
        self.assertIn("Node.js 20+ must be available to $SERVICE_USER", install_script)
        self.assertIn("PULLWISE_PYTHON_BIN", install_script)
        self.assertIn("run_as_service_user \"$BIN_PATH\" doctor || true", install_script)
        self.assertIn("codex login --device-auth", install_script)
        self.assertIn("PULLWISE_WORKER_TOKEN", install_script)
        self.assertIn("--worker-token-file", install_script)
        self.assertIn('write_env_value PULLWISE_SERVER_URL "$SERVER_URL"', install_script)
        self.assertIn('write_env_value PULLWISE_WORKER_TOKEN "$WORKER_TOKEN"', install_script)
        self.assertIn('while IFS="=" read -r key value', install_script)
        self.assertIn('export "$key=$value"', install_script)
        self.assertNotIn("PULLWISE_SERVER_URL=$SERVER_URL", install_script)
        self.assertNotIn("PULLWISE_WORKER_TOKEN=$WORKER_TOKEN", install_script)
        self.assertNotIn(". /etc/pullwise-worker/worker.env", install_script)
        self.assertNotIn("--worker-token) WORKER_TOKEN", install_script)
        self.assertNotIn("$(dirname \"$0\")", install_script)
        self.assertNotIn("cp \"$(dirname", install_script)
        self.assertNotIn("pww_", install_script)
        self.assertIn("Restart=on-failure", install_script)
        self.assertIn("Restart=on-failure", service)
        self.assertNotIn("Restart=always", service)
        self.assertIn("ReadWritePaths=/var/lib/pullwise-worker /var/log/pullwise-worker", service)


if __name__ == "__main__":
    unittest.main()
