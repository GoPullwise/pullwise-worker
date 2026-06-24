from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codereview.config import CodexConfig, ReviewConfig
from codereview.simple_review import (
    CommandEvidence,
    DiscoveryBatch,
    ReviewUnit,
    _write_reports,
    codex_account_preflight,
    command_is_reproduction,
    limit_candidates_per_unit,
    load_simple_settings,
    normalize_candidate,
    parse_command_events,
    plan_discovery_batches,
    plan_review_units,
    run_review,
    validate_discovery_payload,
    validate_unit_coverage,
    validate_verification_result,
)


class SimpleReviewTests(unittest.TestCase):
    def test_settings_cap_turn_parallelism_at_two(self) -> None:
        config = ReviewConfig()
        settings = load_simple_settings(
            {
                "simple": {
                    "discovery_parallel": 99,
                    "verification_parallel": 99,
                    "subagents_per_turn": 99,
                }
            },
            config,
        )
        self.assertEqual(settings.discovery_parallel, 2)
        self.assertEqual(settings.verification_parallel, 2)
        self.assertEqual(settings.subagents_per_turn, 4)

    def test_unit_and_batch_planners_cover_every_file_once(self) -> None:
        files = [
            {
                "path": f"src/pkg_{index % 3}/file_{index}.py",
                "size_bytes": 100 + index,
                "line_count": 10,
                "content_hash": f"sha256:{index}",
            }
            for index in range(27)
        ]
        units = plan_review_units(files, max_files=5, max_bytes=1_000)
        validate_unit_coverage(units, {item["path"] for item in files})
        batches = plan_discovery_batches(
            units,
            target_turns=3,
            max_turns=12,
            max_batch_files=15,
            max_batch_bytes=3_000,
            subagents_per_turn=3,
        )
        batched_ids = [unit.unit_id for batch in batches for unit in batch.units]
        self.assertEqual(set(batched_ids), {unit.unit_id for unit in units})
        self.assertEqual(len(batched_ids), len(set(batched_ids)))
        self.assertLessEqual(len(batches), 3)
        self.assertTrue(all(len(batch.agent_groups) <= 3 for batch in batches))


    def test_batch_planner_adds_sequential_turns_to_respect_context_budget(self) -> None:
        units = [
            ReviewUnit(
                unit_id=f"unit-{index:04d}",
                area="src",
                files=tuple(f"src/{index}_{file_index}.py" for file_index in range(4)),
                size_bytes=400_000,
                line_count=100,
            )
            for index in range(1, 7)
        ]
        batches = plan_discovery_batches(
            units,
            target_turns=2,
            max_turns=8,
            max_batch_files=12,
            max_batch_bytes=800_000,
            subagents_per_turn=3,
        )
        self.assertEqual(len(batches), 3)
        self.assertTrue(all(sum(unit.size_bytes for unit in batch.units) <= 800_000 for batch in batches))
        self.assertTrue(all(sum(len(unit.files) for unit in batch.units) <= 12 for batch in batches))

    def test_batch_planner_fails_instead_of_overstuffing_token_budget(self) -> None:
        units = [
            ReviewUnit(f"unit-{index:04d}", "src", (f"src/{index}.py",), 500_000, 100)
            for index in range(1, 6)
        ]
        with self.assertRaisesRegex(RuntimeError, "exceeds the bounded discovery plan"):
            plan_discovery_batches(
                units,
                target_turns=2,
                max_turns=2,
                max_batch_files=10,
                max_batch_bytes=500_000,
                subagents_per_turn=2,
            )

    def test_candidate_normalization_rejects_primary_evidence_outside_unit(self) -> None:
        unit = ReviewUnit("unit-0001", "src", ("src/a.py",), 10, 2)
        inventory = {
            "src/a.py": {"path": "src/a.py", "line_count": 2},
            "src/b.py": {"path": "src/b.py", "line_count": 2},
        }
        raw = {
            "unit_id": unit.unit_id,
            "severity": "high",
            "category": "Correctness",
            "title": "Wrong result",
            "claim": "The handler returns the wrong result.",
            "trigger_condition": "Call the handler with an empty value.",
            "expected_behavior": "It should return a validation error.",
            "expected_behavior_source": "The public function contract.",
            "actual_behavior_hypothesis": "It returns success.",
            "impact": "Invalid state is accepted.",
            "evidence": [
                {"file": "src/b.py", "line_start": 1, "line_end": 1, "why_it_matters": "Wrong branch."}
            ],
            "path_summary": ["request -> handler"],
            "reproduction_idea": "Run the handler with an empty input.",
        }
        with self.assertRaisesRegex(ValueError, "outside its reviewed unit"):
            normalize_candidate(raw, {unit.unit_id: unit}, inventory)

    def test_per_unit_candidate_budget_is_enforced_deterministically(self) -> None:
        candidates = [
            {
                "candidate_id": f"cand-{index}",
                "unit_id": "unit-0001",
                "severity": severity,
                "evidence": [{"file": "src/a.py"}],
            }
            for index, severity in enumerate(("medium", "critical", "high"), start=1)
        ]
        kept, rejected = limit_candidates_per_unit(candidates, 2)
        self.assertEqual([item["severity"] for item in kept], ["critical", "high"])
        self.assertEqual(len(rejected), 1)
        self.assertEqual(rejected[0]["stage"], "unit-budget")

    def test_discovery_payload_rejects_candidate_outside_assigned_batch(self) -> None:
        assigned = ReviewUnit("unit-0001", "src", ("src/a.py",), 10, 1)
        batch = DiscoveryBatch("discovery-001", (assigned,), ((assigned.unit_id,),))
        with self.assertRaisesRegex(RuntimeError, "outside its assignment"):
            validate_discovery_payload(
                batch,
                {
                    "reviewed_unit_ids": [assigned.unit_id],
                    "candidates": [{"unit_id": "unit-9999"}],
                },
            )

    def test_command_events_are_grounded_in_item_completed_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            events = Path(tmp) / "events.jsonl"
            events.write_text(
                json.dumps(
                    {
                        "method": "item/completed",
                        "params": {
                            "item": {
                                "type": "commandExecution",
                                "command": "python3 .codereview/repro/check.py",
                                "cwd": tmp,
                                "status": "completed",
                                "exitCode": 0,
                                "aggregatedOutput": "PULLWISE_REPRO: observed invalid state",
                            }
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            parsed = parse_command_events(events)
            self.assertEqual(len(parsed), 1)
            self.assertEqual(parsed[0].exit_code, 0)
            self.assertIn("PULLWISE_REPRO", parsed[0].output)

    def test_verification_gate_requires_real_command_marker_and_source_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            source = repo / "src" / "handler.py"
            source.parent.mkdir(parents=True)
            source.write_text("def handle():\n    return False\n", encoding="utf-8")
            harness = repo / ".codereview" / "repro" / "check.py"
            harness.parent.mkdir(parents=True)
            harness.write_text(
                "from src.handler import handle\n"
                "value = handle()\n"
                "print(f'PULLWISE_REPRO:{str(value).lower()}')\n",
                encoding="utf-8",
            )
            candidate = {
                "candidate_id": "cand-1",
                "evidence": [{"file": "src/handler.py", "lines": "1-2", "why_it_matters": "branch"}],
            }
            payload = {
                "candidate_id": "cand-1",
                "status": "confirmed",
                "safe_to_show_user": True,
                "reason": "The command reaches the bad branch.",
                "expected_behavior": "The handler should return true.",
                "observed_behavior": "The handler returns false.",
                "reproduction_command": "python3 .codereview/repro/check.py",
                "output_marker": "PULLWISE_REPRO:false",
                "exercised_files": ["src/handler.py"],
                "skeptic_agreed": True,
                "independent_check": "The skeptic traced the same branch.",
                "limitations": [],
            }
            commands = [
                CommandEvidence(
                    command="python3 .codereview/repro/check.py",
                    cwd=str(repo),
                    exit_code=0,
                    output="PULLWISE_REPRO:false",
                    status="completed",
                ),
                CommandEvidence(
                    command="python3 .codereview/repro/check.py",
                    cwd=str(repo),
                    exit_code=0,
                    output="PULLWISE_REPRO:false",
                    status="completed",
                ),
            ]
            actual, marker = validate_verification_result(
                candidate,
                payload,
                commands,
                repo,
                source_changed=False,
            )
            self.assertEqual(actual.exit_code, 0)
            self.assertEqual(marker, "PULLWISE_REPRO:false")


    def test_verification_gate_rejects_changed_expected_behavior(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            source = repo / "src" / "handler.py"
            source.parent.mkdir(parents=True)
            source.write_text("def handle():\n    return False\n", encoding="utf-8")
            harness = repo / ".codereview" / "repro" / "check.py"
            harness.parent.mkdir(parents=True)
            harness.write_text(
                "from src.handler import handle\n"
                "value = handle()\n"
                "print(f'PULLWISE_REPRO:{str(value).lower()}')\n",
                encoding="utf-8",
            )
            candidate = {
                "candidate_id": "cand-1",
                "expected_behavior": "The handler should return true.",
                "evidence": [{"file": "src/handler.py", "lines": "1-2", "why_it_matters": "branch"}],
            }
            payload = {
                "candidate_id": "cand-1",
                "status": "confirmed",
                "safe_to_show_user": True,
                "reason": "The command reaches the branch.",
                "expected_behavior": "The handler is allowed to return false.",
                "observed_behavior": "The handler returns false.",
                "reproduction_command": "python3 .codereview/repro/check.py",
                "output_marker": "PULLWISE_REPRO:false",
                "exercised_files": ["src/handler.py"],
                "skeptic_agreed": True,
                "independent_check": "The skeptic traced the same branch.",
                "limitations": [],
            }
            commands = [
                CommandEvidence(
                    command="python3 .codereview/repro/check.py",
                    cwd=str(repo),
                    exit_code=0,
                    output="PULLWISE_REPRO:false",
                    status="completed",
                ),
                CommandEvidence(
                    command="python3 .codereview/repro/check.py",
                    cwd=str(repo),
                    exit_code=0,
                    output="PULLWISE_REPRO:false",
                    status="completed",
                ),
            ]
            with self.assertRaisesRegex(ValueError, "changed the candidate's expected behavior"):
                validate_verification_result(candidate, payload, commands, repo, source_changed=False)

    def test_verification_gate_rejects_single_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            source = repo / "src" / "handler.py"
            source.parent.mkdir(parents=True)
            source.write_text("def handle():\n    return False\n", encoding="utf-8")
            harness = repo / ".codereview" / "repro" / "check.py"
            harness.parent.mkdir(parents=True)
            harness.write_text("from src.handler import handle\nprint(handle())\n", encoding="utf-8")
            candidate = {
                "candidate_id": "cand-1",
                "evidence": [{"file": "src/handler.py", "lines": "1-2", "why_it_matters": "branch"}],
            }
            payload = {
                "candidate_id": "cand-1",
                "status": "confirmed",
                "safe_to_show_user": True,
                "reason": "The command reaches the bad branch.",
                "expected_behavior": "The handler should return true.",
                "observed_behavior": "The handler returns false.",
                "reproduction_command": "python3 .codereview/repro/check.py",
                "output_marker": "OBSERVED_FALSE",
                "exercised_files": ["src/handler.py"],
                "skeptic_agreed": True,
                "independent_check": "The skeptic traced the same branch.",
                "limitations": [],
            }
            commands = [
                CommandEvidence(
                    command="python3 .codereview/repro/check.py",
                    cwd=str(repo),
                    exit_code=0,
                    output="OBSERVED_FALSE",
                    status="completed",
                )
            ]
            with self.assertRaisesRegex(ValueError, "at least two"):
                validate_verification_result(candidate, payload, commands, repo, source_changed=False)


    def test_verification_gate_rejects_hardcoded_harness_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            source = repo / "src" / "handler.py"
            source.parent.mkdir(parents=True)
            source.write_text("def handle():\n    return False\n", encoding="utf-8")
            harness = repo / ".codereview" / "repro" / "check.py"
            harness.parent.mkdir(parents=True)
            harness.write_text(
                "from src.handler import handle\nhandle()\nprint('HARDCODED_MARKER')\n",
                encoding="utf-8",
            )
            candidate = {
                "candidate_id": "cand-1",
                "evidence": [{"file": "src/handler.py", "lines": "1-2", "why_it_matters": "branch"}],
            }
            payload = {
                "candidate_id": "cand-1",
                "status": "confirmed",
                "safe_to_show_user": True,
                "reason": "The command reaches the bad branch.",
                "expected_behavior": "The handler should return true.",
                "observed_behavior": "The handler returns false.",
                "reproduction_command": "python3 .codereview/repro/check.py",
                "output_marker": "HARDCODED_MARKER",
                "exercised_files": ["src/handler.py"],
                "skeptic_agreed": True,
                "independent_check": "The skeptic traced the same branch.",
                "limitations": [],
            }
            commands = [
                CommandEvidence(
                    command="python3 .codereview/repro/check.py",
                    cwd=str(repo),
                    exit_code=0,
                    output="HARDCODED_MARKER",
                    status="completed",
                ),
                CommandEvidence(
                    command="python3 .codereview/repro/check.py",
                    cwd=str(repo),
                    exit_code=0,
                    output="HARDCODED_MARKER",
                    status="completed",
                ),
            ]
            with self.assertRaisesRegex(ValueError, "do not ground execution"):
                validate_verification_result(candidate, payload, commands, repo, source_changed=False)

    def test_inspection_command_cannot_smuggle_runtime_name(self) -> None:
        self.assertFalse(command_is_reproduction('echo "python fake proof"'))
        self.assertFalse(command_is_reproduction("cat src/handler.py"))
        self.assertTrue(command_is_reproduction("python3 .codereview/repro/check.py"))
        self.assertTrue(command_is_reproduction("npm test -- --runInBand"))


    def test_run_review_orchestrates_full_repo_to_confirmed_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            checkout = Path(tmp)
            source = checkout / "src" / "handler.py"
            source.parent.mkdir(parents=True)
            source.write_text("def handle():\n    return False\n", encoding="utf-8")
            inventory = {
                "files": [
                    {
                        "path": "src/handler.py",
                        "size_bytes": source.stat().st_size,
                        "line_count": 2,
                        "content_hash": "sha256:test",
                        "scope": "analyze",
                    }
                ],
                "summary": {"files": 1, "analyzable_files": 1},
            }

            def source_state(root, include_untracked=True):
                del include_untracked
                target = Path(root) / "src" / "handler.py"
                return {"src/handler.py": target.read_text(encoding="utf-8")}

            def create_snapshot(_checkout, _inventory, run):
                del _checkout, _inventory
                snapshot = Path(run) / "snapshot" / "repo"
                target = snapshot / "src" / "handler.py"
                target.parent.mkdir(parents=True)
                shutil.copy2(source, target)
                return {"snapshot_repo": str(snapshot)}

            def create_worker(snapshot_repo, worker_root, candidate):
                del candidate
                shutil.copytree(Path(snapshot_repo), Path(worker_root) / "repo")

            def fake_codex_json(**kwargs):
                schema_name = Path(kwargs["schema_path"]).name
                cd = Path(kwargs["cd"])
                if schema_name == "discovery.schema.json":
                    assignment = next((cd / ".codereview" / "simple" / "assignments").glob("*.json"))
                    payload = json.loads(assignment.read_text(encoding="utf-8"))
                    unit_id = payload["units"][0]["unit_id"]
                    return {
                        "reviewed_unit_ids": [unit_id],
                        "candidates": [
                            {
                                "unit_id": unit_id,
                                "severity": "high",
                                "category": "Correctness",
                                "title": "Handler returns the wrong value",
                                "claim": "The handler returns false for the supported call.",
                                "trigger_condition": "Call handle without arguments.",
                                "expected_behavior": "The handler should return true.",
                                "expected_behavior_source": "The public function contract.",
                                "actual_behavior_hypothesis": "The handler returns false.",
                                "impact": "Callers receive an invalid result.",
                                "evidence": [
                                    {
                                        "file": "src/handler.py",
                                        "line_start": 1,
                                        "line_end": 2,
                                        "why_it_matters": "This is the returned value.",
                                    }
                                ],
                                "path_summary": ["caller -> src/handler.py"],
                                "reproduction_idea": "Invoke handle and print the observed value.",
                            }
                        ],
                    }
                harness = cd / ".codereview" / "repro" / "check.py"
                harness.parent.mkdir(parents=True, exist_ok=True)
                harness.write_text(
                    "from src.handler import handle\n"
                    "value = handle()\n"
                    "print(f'OBSERVED_VALUE:{str(value).lower()}')\n",
                    encoding="utf-8",
                )
                command = "python3 .codereview/repro/check.py"
                event = {
                    "method": "item/completed",
                    "params": {
                        "item": {
                            "type": "commandExecution",
                            "command": command,
                            "cwd": str(cd),
                            "status": "completed",
                            "exitCode": 0,
                            "aggregatedOutput": "OBSERVED_VALUE:false",
                        }
                    },
                }
                events = Path(kwargs["events_file"])
                events.parent.mkdir(parents=True, exist_ok=True)
                events.write_text(json.dumps(event) + "\n" + json.dumps(event) + "\n", encoding="utf-8")
                candidate = json.loads(
                    (cd / ".codereview" / "simple" / "candidate.json").read_text(encoding="utf-8")
                )
                return {
                    "candidate_id": candidate["candidate_id"],
                    "status": "confirmed",
                    "safe_to_show_user": True,
                    "reason": "The repeated command observes the same wrong value.",
                    "expected_behavior": "The handler should return true.",
                    "observed_behavior": "The handler returns false.",
                    "reproduction_command": command,
                    "output_marker": "OBSERVED_VALUE:false",
                    "exercised_files": ["src/handler.py"],
                    "skeptic_agreed": True,
                    "independent_check": "The skeptic independently traced the return statement.",
                    "limitations": [],
                }

            with (
                patch("codereview.simple_review.build_git_inventory", return_value=inventory),
                patch("codereview.simple_review.create_immutable_snapshot", side_effect=create_snapshot),
                patch("codereview.simple_review.source_state_from_inventory", return_value=source_state(checkout)),
                patch("codereview.simple_review.capture_source_state", side_effect=source_state),
                patch("codereview.simple_review.codex_account_preflight", return_value={"shared_app_server": True}),
                patch("codereview.simple_review.create_worker_dir", side_effect=create_worker),
                patch("codereview.simple_review._run_codex_json", side_effect=fake_codex_json),
            ):
                final = run_review(checkout, mode="standard", scan_mode="full-cached")

            final_json = json.loads(final.with_name("final.json").read_text(encoding="utf-8"))
            self.assertEqual(len(final_json["confirmed"]), 1)
            confirmed = final_json["confirmed"][0]
            self.assertEqual(confirmed["judge"]["level"], "L2")
            self.assertEqual(confirmed["repro"]["status"], "reproduced")
            self.assertEqual(confirmed["repro"]["level"], "L2")
            self.assertEqual(confirmed["verification"]["level"], "L2")
            self.assertTrue(confirmed["candidate"]["minimal_repro_idea"])
            summary = json.loads(final.with_name("summary.json").read_text(encoding="utf-8"))
            self.assertTrue(summary["coverage"]["complete"])
            self.assertEqual(summary["reports"]["confirmed"], 1)

    def test_account_preflight_never_forces_token_refresh(self) -> None:
        class FakeClient:
            def __init__(self) -> None:
                self.started = False
                self.params = None

            def ensure_started(self) -> None:
                self.started = True

            def request(self, method, params, timeout_seconds):
                self.params = (method, params, timeout_seconds)
                return {
                    "requiresOpenaiAuth": True,
                    "account": {"type": "chatgpt", "planType": "pro", "email": "hidden@example.com"},
                }

        client = FakeClient()
        with patch("codereview.simple_review.get_codex_app_server_client", return_value=client):
            result = codex_account_preflight(CodexConfig(), Path("."))
        self.assertTrue(client.started)
        self.assertEqual(client.params[0], "account/read")
        self.assertEqual(client.params[1], {"refreshToken": False})
        self.assertFalse(result["refresh_forced"])
        self.assertNotIn("email", result)

    def test_account_preflight_generic_401_is_auth_required(self) -> None:
        class FakeClient:
            def __init__(self) -> None:
                self.closed = False

            def ensure_started(self) -> None:
                return None

            def request(self, method, params, timeout_seconds):
                del method, params, timeout_seconds
                raise RuntimeError("401 Unauthorized")

            def close(self) -> None:
                self.closed = True

        client = FakeClient()
        with (
            patch("codereview.simple_review.get_codex_app_server_client", return_value=client),
            self.assertRaisesRegex(RuntimeError, "codex_auth_required"),
        ):
            codex_account_preflight(CodexConfig(), Path("."))
        self.assertTrue(client.closed)

    def test_account_preflight_transport_error_does_not_recycle_shared_process(self) -> None:
        class FakeClient:
            def __init__(self) -> None:
                self.closed = False

            def ensure_started(self) -> None:
                return None

            def request(self, method, params, timeout_seconds):
                del method, params, timeout_seconds
                raise TimeoutError("account/read timed out")

            def close(self) -> None:
                self.closed = True

        client = FakeClient()
        with (
            patch("codereview.simple_review.get_codex_app_server_client", return_value=client),
            self.assertRaisesRegex(RuntimeError, "account/read timed out"),
        ):
            codex_account_preflight(CodexConfig(), Path("."))
        self.assertFalse(client.closed)

    def test_account_preflight_auth_error_recycles_shared_process(self) -> None:
        class FakeClient:
            def __init__(self) -> None:
                self.closed = False

            def ensure_started(self) -> None:
                return None

            def request(self, method, params, timeout_seconds):
                del method, params, timeout_seconds
                raise RuntimeError("Failed to refresh token: refresh token was already used")

            def close(self) -> None:
                self.closed = True

        client = FakeClient()
        with (
            patch("codereview.simple_review.get_codex_app_server_client", return_value=client),
            self.assertRaisesRegex(RuntimeError, "codex_auth_expired"),
        ):
            codex_account_preflight(CodexConfig(), Path("."))
        self.assertTrue(client.closed)

    def test_unconfirmed_candidates_never_enter_public_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp) / "run"
            run.mkdir()
            final = _write_reports(
                run,
                mode="standard",
                scan_mode="full-cached",
                inventory={"summary": {"files": 1, "analyzable_files": 1}},
                units=[],
                discovery_results=[],
                raw_candidates=[],
                valid_candidates=[],
                selected_candidates=[],
                confirmed=[],
                rejected=[{"stage": "verification", "candidate_id": "cand-x", "reason": "not reproducible"}],
                account={},
                progress=None,
            )
            reports = final.parent
            self.assertEqual(json.loads((reports / "rejected.json").read_text(encoding="utf-8")), [])
            internal = json.loads((run / "diagnostics" / "internal-rejections.json").read_text(encoding="utf-8"))
            self.assertEqual(internal[0]["candidate_id"], "cand-x")


if __name__ == "__main__":
    unittest.main()
