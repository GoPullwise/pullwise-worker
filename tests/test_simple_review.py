from __future__ import annotations

import json
import shutil
import threading
import time
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codereview.app_server_runner import stored_app_server_event
from codereview.config import CodexConfig, ReviewConfig
from codereview.simple_review import (
    CommandEvidence,
    DiscoveryBatch,
    ReviewUnit,
    _rejected_record,
    _run_verifications,
    _write_reports,
    codex_account_preflight,
    command_is_reproduction,
    limit_candidates_per_unit,
    load_simple_settings,
    recommended_simple_parallelism,
    normalize_candidate,
    parse_command_events,
    plan_discovery_batches,
    plan_review_units,
    run_review,
    tune_simple_parallelism,
    validate_discovery_payload,
    validate_unit_coverage,
    validate_verification_result,
    verification_prompt,
)


class SimpleReviewTests(unittest.TestCase):
    def test_settings_allow_auto_parallelism_and_cap_explicit_values_at_six(self) -> None:
        config = ReviewConfig()
        default_settings = load_simple_settings({"simple": {}}, config)
        self.assertEqual(default_settings.discovery_parallel, 0)
        self.assertEqual(default_settings.verification_parallel, 0)

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
        self.assertEqual(settings.discovery_parallel, 6)
        self.assertEqual(settings.verification_parallel, 6)
        self.assertEqual(settings.subagents_per_turn, 6)

    def test_verification_prompt_requires_verbatim_candidate_expected_behavior(self) -> None:
        settings = load_simple_settings({"simple": {}}, ReviewConfig())
        prompt = verification_prompt(
            ".codereview/simple/candidate.json",
            {"candidate_id": "cand-1", "expected_behavior": "The handler should return true."},
            settings,
        )

        self.assertIn("expected_behavior is a contract carried forward from discovery", prompt)
        self.assertIn("copy it exactly", prompt)
        self.assertIn("without paraphrasing", prompt)
        self.assertIn("\"The handler should return true.\"", prompt)
        self.assertIn("return status=rejected", prompt)
        self.assertIn("Use proof_type=runtime-command", prompt)
        self.assertIn("Use proof_type=static-proof", prompt)
        self.assertIn("A narrative description without inspected files and verification steps is not enough", prompt)

    def test_rejected_record_keeps_raw_title_out_of_candidate_id(self) -> None:
        record = _rejected_record(
            "discovery",
            {"title": "静态证明可无命令证据进入 confirmed", "unit_id": "unit-0001"},
            "candidate primary evidence is outside its reviewed unit",
        )

        self.assertEqual(record["candidate_id"], "")
        self.assertEqual(record["title"], "静态证明可无命令证据进入 confirmed")
        self.assertEqual(record["unit_id"], "unit-0001")

    def test_auto_parallelism_uses_app_server_and_resource_budget(self) -> None:
        inventory = {
            "files": [
                {"path": f"src/file_{index}.py", "size_bytes": 200_000, "scope": "analyze"}
                for index in range(1200)
            ],
            "summary": {"analyzable_files": 1200},
        }
        settings = load_simple_settings({"simple": {}}, ReviewConfig())
        with (
            patch("codereview.simple_review.os.cpu_count", return_value=16),
            patch("codereview.simple_review.available_memory_gib", return_value=32.0),
        ):
            self.assertEqual(recommended_simple_parallelism(inventory, {"shared_app_server": True}), 6)
            tuned = tune_simple_parallelism(settings, inventory, {"shared_app_server": True})
        self.assertEqual(tuned.discovery_parallel, 6)
        self.assertEqual(tuned.verification_parallel, 6)
        self.assertEqual(tune_simple_parallelism(settings, inventory, {}).discovery_parallel, 1)

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

    def test_command_events_parse_normalized_shell_exec_payloads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            raw_event = {
                "method": "item/completed",
                "params": {
                    "threadId": "thread-1",
                    "turnId": "turn-1",
                    "item": {
                        "type": "shellExec",
                        "commandLine": ["python3", ".codereview/repro/check.py"],
                        "cwd": tmp,
                        "status": "completed",
                        "exit_code": 0,
                        "stdout": "PULLWISE_REPRO:false",
                    },
                },
            }
            stored = stored_app_server_event(raw_event)
            self.assertIsNotNone(stored)
            events = Path(tmp) / "events.jsonl"
            events.write_text(json.dumps(stored) + "\n", encoding="utf-8")

            parsed = parse_command_events(events)
            self.assertEqual(len(parsed), 1)
            self.assertEqual(parsed[0].command, "python3 .codereview/repro/check.py")
            self.assertEqual(parsed[0].exit_code, 0)
            self.assertIn("PULLWISE_REPRO:false", parsed[0].output)

    def test_command_events_default_completed_status_for_completed_items(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            raw_event = {
                "method": "item/completed",
                "params": {
                    "threadId": "thread-1",
                    "turnId": "turn-1",
                    "item": {
                        "type": "shellExec",
                        "commandLine": ["python3", ".codereview/repro/check.py"],
                        "cwd": tmp,
                        "exit_code": 0,
                        "stdout": "PULLWISE_REPRO:false",
                    },
                },
            }
            stored = stored_app_server_event(raw_event)
            self.assertIsNotNone(stored)
            self.assertEqual(stored["params"]["item"]["status"], "completed")
            events = Path(tmp) / "events.jsonl"
            events.write_text(json.dumps(stored) + "\n", encoding="utf-8")

            parsed = parse_command_events(events)
            self.assertEqual(len(parsed), 1)
            self.assertEqual(parsed[0].status, "completed")

    def test_command_events_parse_commands_after_oversized_event_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            events = Path(tmp) / "events.jsonl"
            command = "python3 .codereview/repro/check.py"
            event = {
                "method": "item/completed",
                "params": {
                    "item": {
                        "type": "commandExecution",
                        "command": command,
                        "cwd": tmp,
                        "status": "completed",
                        "exitCode": 0,
                        "aggregatedOutput": "PULLWISE_REPRO:false",
                    }
                },
            }
            events.write_text(("x" * (9 * 1024 * 1024)) + "\n" + json.dumps(event) + "\n", encoding="utf-8")
            parsed = parse_command_events(events)
            self.assertEqual(len(parsed), 1)
            self.assertEqual(parsed[0].command, command)

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

    def test_verification_gate_accepts_single_grounded_execution(self) -> None:
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
            actual, marker = validate_verification_result(candidate, payload, commands, repo, source_changed=False)
            self.assertEqual(actual.command, "python3 .codereview/repro/check.py")
            self.assertEqual(marker, "OBSERVED_FALSE")


    def test_verification_gate_rejects_missing_command_event(self) -> None:
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
            with self.assertRaisesRegex(ValueError, "completed app-server command event"):
                validate_verification_result(candidate, payload, [], repo, source_changed=False)

    def test_verification_gate_accepts_static_proof_without_command_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            source = repo / "src" / "handler.py"
            source.parent.mkdir(parents=True)
            source.write_text("def handle():\n    return False\n", encoding="utf-8")
            candidate = {
                "candidate_id": "cand-1",
                "expected_behavior": "The handler should return true.",
                "evidence": [{"file": "src/handler.py", "lines": "1-2", "why_it_matters": "branch"}],
            }
            payload = {
                "candidate_id": "cand-1",
                "status": "confirmed",
                "proof_type": "static-proof",
                "safe_to_show_user": True,
                "reason": "The source returns false on the cited branch.",
                "expected_behavior": "The handler should return true.",
                "observed_behavior": "The handler returns false.",
                "reproduction_command": "",
                "output_marker": "",
                "exercised_files": ["src/handler.py"],
                "verification_steps": [
                    "Inspect src/handler.py and confirm handle() returns False.",
                    "Compare that observed return value with the expected true behavior.",
                ],
                "skeptic_agreed": True,
                "independent_check": "The skeptic traced the same static branch.",
                "limitations": [],
            }
            actual, marker = validate_verification_result(candidate, payload, [], repo, source_changed=False)
            self.assertIsNone(actual)
            self.assertEqual(marker, "")

    def test_verification_gate_rejects_static_proof_without_steps(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            source = repo / "src" / "handler.py"
            source.parent.mkdir(parents=True)
            source.write_text("def handle():\n    return False\n", encoding="utf-8")
            candidate = {
                "candidate_id": "cand-1",
                "expected_behavior": "The handler should return true.",
                "evidence": [{"file": "src/handler.py", "lines": "1-2", "why_it_matters": "branch"}],
            }
            payload = {
                "candidate_id": "cand-1",
                "status": "confirmed",
                "proof_type": "static-proof",
                "safe_to_show_user": True,
                "reason": "The source returns false on the cited branch.",
                "expected_behavior": "The handler should return true.",
                "observed_behavior": "The handler returns false.",
                "reproduction_command": "",
                "output_marker": "",
                "exercised_files": ["src/handler.py"],
                "verification_steps": [],
                "skeptic_agreed": True,
                "independent_check": "The skeptic traced the same static branch.",
                "limitations": [],
            }

            with self.assertRaisesRegex(ValueError, "static proof lacks verification steps"):
                validate_verification_result(candidate, payload, [], repo, source_changed=False)

    def test_verification_gate_rejects_static_proof_missing_primary_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            source = repo / "src" / "handler.py"
            other = repo / "src" / "other.py"
            source.parent.mkdir(parents=True)
            source.write_text("def handle():\n    return False\n", encoding="utf-8")
            other.write_text("EXPECTED = True\n", encoding="utf-8")
            candidate = {
                "candidate_id": "cand-1",
                "expected_behavior": "The handler should return true.",
                "evidence": [
                    {"file": "src/handler.py", "lines": "1-2", "why_it_matters": "observed"},
                    {"file": "src/other.py", "lines": "1", "why_it_matters": "contract"},
                ],
            }
            payload = {
                "candidate_id": "cand-1",
                "status": "confirmed",
                "proof_type": "static-proof",
                "safe_to_show_user": True,
                "reason": "The source returns false on the cited branch.",
                "expected_behavior": "The handler should return true.",
                "observed_behavior": "The handler returns false.",
                "reproduction_command": "",
                "output_marker": "",
                "exercised_files": ["src/handler.py"],
                "verification_steps": ["Inspect src/handler.py and src/other.py."],
                "skeptic_agreed": True,
                "independent_check": "The skeptic traced the same static branch.",
                "limitations": [],
            }

            with self.assertRaisesRegex(ValueError, "static proof did not inspect every primary evidence file"):
                validate_verification_result(candidate, payload, [], repo, source_changed=False)
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

    def test_verification_gate_rejects_inline_marker_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            source = repo / "src" / "handler.py"
            source.parent.mkdir(parents=True)
            source.write_text("def handle():\n    return False\n", encoding="utf-8")
            candidate = {
                "candidate_id": "cand-1",
                "evidence": [{"file": "src/handler.py", "lines": "1-2", "why_it_matters": "branch"}],
            }
            command = "python3 -c \"print('src/handler.py PULLWISE_REPRO:false')\""
            payload = {
                "candidate_id": "cand-1",
                "status": "confirmed",
                "safe_to_show_user": True,
                "reason": "The command claims to reach the bad branch.",
                "expected_behavior": "The handler should return true.",
                "observed_behavior": "The handler returns false.",
                "reproduction_command": command,
                "output_marker": "PULLWISE_REPRO:false",
                "exercised_files": ["src/handler.py"],
                "skeptic_agreed": True,
                "independent_check": "The skeptic accepted the proof.",
                "limitations": [],
            }
            commands = [
                CommandEvidence(command=command, cwd=str(repo), exit_code=0, output="src/handler.py PULLWISE_REPRO:false", status="completed"),
                CommandEvidence(command=command, cwd=str(repo), exit_code=0, output="src/handler.py PULLWISE_REPRO:false", status="completed"),
            ]
            with self.assertRaisesRegex(ValueError, "inline code"):
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

            def source_state(root, include_untracked=True, max_text_file_bytes=1_000_000):
                del include_untracked, max_text_file_bytes
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
    def test_sequential_verification_reuses_single_lane_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            snapshot = root / "snapshot"
            source = snapshot / "src" / "handler.py"
            source.parent.mkdir(parents=True)
            source.write_text("def handle():\n    return False\n", encoding="utf-8")
            run = root / "run"
            candidates = [
                {
                    "candidate_id": f"cand-{index}",
                    "expected_behavior": "The handler should return true.",
                    "evidence": [{"file": "src/handler.py", "lines": "1-2", "why_it_matters": "branch"}],
                }
                for index in range(2)
            ]
            settings = load_simple_settings({"simple": {"verification_parallel": 1}}, ReviewConfig())
            copy_calls: list[Path] = []
            codex_cwds: list[Path] = []

            def fake_create_worker(snapshot_repo, worker_root, candidate):
                del candidate
                copy_calls.append(Path(worker_root))
                worker_root = Path(worker_root)
                if worker_root.exists():
                    shutil.rmtree(worker_root)
                worker_root.mkdir(parents=True)
                shutil.copytree(Path(snapshot_repo), worker_root / "repo")
                return worker_root

            def fake_codex_json(**kwargs):
                cd = Path(kwargs["cd"])
                codex_cwds.append(cd)
                candidate = json.loads((cd / ".codereview" / "simple" / "candidate.json").read_text(encoding="utf-8"))
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
                    "params": {"item": {"type": "commandExecution", "command": command, "cwd": str(cd), "status": "completed", "exitCode": 0, "aggregatedOutput": "OBSERVED_VALUE:false"}},
                }
                Path(kwargs["events_file"]).write_text(json.dumps(event) + "\n" + json.dumps(event) + "\n", encoding="utf-8")
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
                patch("codereview.simple_review.create_worker_dir", side_effect=fake_create_worker),
                patch("codereview.simple_review._run_codex_json", side_effect=fake_codex_json),
            ):
                results = _run_verifications(snapshot, run, candidates, CodexConfig(), settings, threading.Event(), None, "run-id")

            self.assertEqual(len(copy_calls), 1)
            self.assertTrue(all(result["confirmed"] for result in results))
            self.assertEqual(len({path for path in codex_cwds}), 1)

    def test_sequential_verification_rebuilds_lane_after_source_change(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            snapshot = root / "snapshot"
            source = snapshot / "src" / "handler.py"
            source.parent.mkdir(parents=True)
            source.write_text("def handle():\n    return False\n", encoding="utf-8")
            run = root / "run"
            candidates = [
                {
                    "candidate_id": "cand-dirty",
                    "expected_behavior": "The handler should return true.",
                    "evidence": [{"file": "src/handler.py", "lines": "1-2", "why_it_matters": "branch"}],
                },
                {
                    "candidate_id": "cand-clean",
                    "expected_behavior": "The handler should return true.",
                    "evidence": [{"file": "src/handler.py", "lines": "1-2", "why_it_matters": "branch"}],
                },
            ]
            settings = load_simple_settings({"simple": {"verification_parallel": 1}}, ReviewConfig())
            copy_calls: list[Path] = []

            def fake_create_worker(snapshot_repo, worker_root, candidate):
                del candidate
                copy_calls.append(Path(worker_root))
                worker_root = Path(worker_root)
                if worker_root.exists():
                    shutil.rmtree(worker_root)
                worker_root.mkdir(parents=True)
                shutil.copytree(Path(snapshot_repo), worker_root / "repo")
                return worker_root

            def fake_codex_json(**kwargs):
                cd = Path(kwargs["cd"])
                candidate = json.loads((cd / ".codereview" / "simple" / "candidate.json").read_text(encoding="utf-8"))
                if candidate["candidate_id"] == "cand-dirty":
                    (cd / "src" / "handler.py").write_text("def handle():\n    return True\n", encoding="utf-8")
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
                    "params": {"item": {"type": "commandExecution", "command": command, "cwd": str(cd), "status": "completed", "exitCode": 0, "aggregatedOutput": "OBSERVED_VALUE:false"}},
                }
                Path(kwargs["events_file"]).write_text(json.dumps(event) + "\n" + json.dumps(event) + "\n", encoding="utf-8")
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
                patch("codereview.simple_review.create_worker_dir", side_effect=fake_create_worker),
                patch("codereview.simple_review._run_codex_json", side_effect=fake_codex_json),
            ):
                results = _run_verifications(snapshot, run, candidates, CodexConfig(), settings, threading.Event(), None, "run-id")

            self.assertEqual(len(copy_calls), 2)
            self.assertFalse(results[0]["confirmed"])
            self.assertIn("modified repository source files", results[0]["reason"])
            self.assertTrue(results[1]["confirmed"])

    def test_verification_turn_failure_rejects_candidate_without_aborting_scan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            snapshot = root / "snapshot"
            source = snapshot / "src" / "handler.py"
            source.parent.mkdir(parents=True)
            source.write_text("def handle():\n    return False\n", encoding="utf-8")
            run = root / "run"
            candidates = [
                {
                    "candidate_id": "cand-fail",
                    "expected_behavior": "The handler should return true.",
                    "evidence": [{"file": "src/handler.py", "lines": "1-2", "why_it_matters": "branch"}],
                },
                {
                    "candidate_id": "cand-pass",
                    "expected_behavior": "The handler should return true.",
                    "evidence": [{"file": "src/handler.py", "lines": "1-2", "why_it_matters": "branch"}],
                },
            ]
            settings = load_simple_settings({"simple": {"verification_parallel": 1}}, ReviewConfig())

            def fake_codex_json(**kwargs):
                cd = Path(kwargs["cd"])
                candidate = json.loads((cd / ".codereview" / "simple" / "candidate.json").read_text(encoding="utf-8"))
                if candidate["candidate_id"] == "cand-fail":
                    raise RuntimeError("Codex turn returned invalid structured JSON")
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

            with patch("codereview.simple_review._run_codex_json", side_effect=fake_codex_json):
                results = _run_verifications(
                    snapshot,
                    run,
                    candidates,
                    CodexConfig(),
                    settings,
                    threading.Event(),
                    None,
                    "run-id",
                    0.0,
                )

            self.assertFalse(results[0]["confirmed"])
            self.assertIn("verification turn failed", results[0]["reason"])
            self.assertTrue(results[1]["confirmed"])

    def test_verification_deadline_rejects_without_starting_codex_turn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            snapshot = root / "snapshot"
            snapshot.mkdir()
            run = root / "run"
            candidate = {"candidate_id": "cand-deadline", "evidence": []}
            settings = load_simple_settings({"simple": {"verification_parallel": 1}}, ReviewConfig())
            with patch("codereview.simple_review._run_codex_json") as codex_json:
                results = _run_verifications(
                    snapshot,
                    run,
                    [candidate],
                    CodexConfig(),
                    settings,
                    threading.Event(),
                    None,
                    "run-id",
                    time.monotonic() - 1,
                )
            codex_json.assert_not_called()
            self.assertFalse(results[0]["confirmed"])
            self.assertIn("global scan deadline", results[0]["reason"])

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
            diagnostics = json.loads((reports / "diagnostics.json").read_text(encoding="utf-8"))
            self.assertEqual(diagnostics["internalRejections"][0]["candidate_id"], "cand-x")
            self.assertEqual(diagnostics["reasonCounts"][0]["reason"], "not reproducible")
            debug_markdown = (reports / "debug.md").read_text(encoding="utf-8")
            self.assertIn("Internal rejection reason counts", debug_markdown)
            self.assertIn("not reproducible", debug_markdown)

    def test_report_summary_marks_verification_budget_drops_incomplete(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp) / "run"
            run.mkdir()
            unit = ReviewUnit(unit_id="unit-1", area="src", files=("src/a.py",), size_bytes=10, line_count=1)
            final = _write_reports(
                run,
                mode="standard",
                scan_mode="full-cached",
                inventory={"summary": {"files": 1, "analyzable_files": 1}},
                units=[unit],
                discovery_results=[{"reviewed_unit_ids": ["unit-1"], "candidates": []}],
                raw_candidates=[{"candidate_id": "cand-1"}],
                valid_candidates=[{"candidate_id": "cand-1"}, {"candidate_id": "cand-2"}],
                selected_candidates=[{"candidate_id": "cand-1"}],
                confirmed=[],
                rejected=[{"stage": "budget", "candidate_id": "cand-2", "reason": "runtime verification budget exhausted"}],
                account={},
                progress=None,
            )
            diagnostics = json.loads(final.with_name("diagnostics.json").read_text(encoding="utf-8"))
            self.assertEqual(diagnostics["selectedCandidateCount"], 1)
            self.assertEqual(diagnostics["selectedCandidates"][0]["candidate_id"], "cand-1")
            self.assertEqual(diagnostics["internalRejectionCount"], 1)
            summary = json.loads(final.with_name("summary.json").read_text(encoding="utf-8"))
            self.assertTrue(summary["coverage"]["discoveryComplete"])
            self.assertFalse(summary["coverage"]["verificationComplete"])
            self.assertFalse(summary["coverage"]["complete"])
            self.assertEqual(summary["coverage"]["verificationBudgetDropped"], 1)
            self.assertEqual(summary["reports"]["blocked"], 1)

if __name__ == "__main__":
    unittest.main()
