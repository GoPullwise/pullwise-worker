from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

from pullwise_worker.codex_sdk_runtime import CodexRuntimeResources, CodexTokenUsage


def usage(
    *,
    input_tokens: int,
    cached_input_tokens: int,
    output_tokens: int,
    reasoning_output_tokens: int,
    total_tokens: int,
    snake_case: bool = False,
) -> dict[str, int]:
    if snake_case:
        return {
            "input_tokens": input_tokens,
            "cached_input_tokens": cached_input_tokens,
            "output_tokens": output_tokens,
            "reasoning_output_tokens": reasoning_output_tokens,
            "total_tokens": total_tokens,
        }
    return {
        "inputTokens": input_tokens,
        "cachedInputTokens": cached_input_tokens,
        "outputTokens": output_tokens,
        "reasoningOutputTokens": reasoning_output_tokens,
        "totalTokens": total_tokens,
    }


def token_event(
    turn_id: str,
    thread_id: str,
    *,
    total: dict[str, object],
    last: dict[str, object],
    snake_case: bool = False,
) -> dict[str, object]:
    if snake_case:
        return {
            "turn_id": turn_id,
            "thread_id": thread_id,
            "token_usage": {"total": total, "last": last},
        }
    return {
        "turnId": turn_id,
        "threadId": thread_id,
        "tokenUsage": {"total": total, "last": last},
    }


class CodexUsageLedgerTests(unittest.TestCase):
    def test_real_cumulative_snapshots_aggregate_each_turn_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            resources = CodexRuntimeResources(Path(tmp_dir) / "codex-events.jsonl")
            auth = resources.begin_turn(
                phase="check_codex_auth",
                turn_id="turn-auth",
                thread_id="thread-root",
            )
            auth_usage = usage(
                input_tokens=15_384,
                cached_input_tokens=2_432,
                output_tokens=21,
                reasoning_output_tokens=10,
                total_tokens=15_405,
            )
            resources.record_event(
                auth,
                "thread/tokenUsage/updated",
                token_event(
                    "turn-auth",
                    "thread-root",
                    total=auth_usage,
                    last=auth_usage,
                ),
            )
            resources.abandon_turn(auth)

            bootstrap = resources.begin_turn(
                phase="bootstrap_helper_scripts",
                turn_id="turn-bootstrap",
                thread_id="thread-root",
            )
            snapshots = (
                (
                    usage(
                        input_tokens=31_636,
                        cached_input_tokens=17_664,
                        output_tokens=250,
                        reasoning_output_tokens=64,
                        total_tokens=31_886,
                    ),
                    usage(
                        input_tokens=16_252,
                        cached_input_tokens=15_232,
                        output_tokens=229,
                        reasoning_output_tokens=54,
                        total_tokens=16_481,
                    ),
                ),
                (
                    usage(
                        input_tokens=49_125,
                        cached_input_tokens=20_096,
                        output_tokens=627,
                        reasoning_output_tokens=133,
                        total_tokens=49_752,
                    ),
                    usage(
                        input_tokens=17_489,
                        cached_input_tokens=2_432,
                        output_tokens=377,
                        reasoning_output_tokens=69,
                        total_tokens=17_866,
                    ),
                ),
                (
                    usage(
                        input_tokens=67_463,
                        cached_input_tokens=22_528,
                        output_tokens=1_000,
                        reasoning_output_tokens=187,
                        total_tokens=68_463,
                    ),
                    usage(
                        input_tokens=18_338,
                        cached_input_tokens=2_432,
                        output_tokens=373,
                        reasoning_output_tokens=54,
                        total_tokens=18_711,
                    ),
                ),
                (
                    usage(
                        input_tokens=91_533,
                        cached_input_tokens=40_320,
                        output_tokens=1_366,
                        reasoning_output_tokens=301,
                        total_tokens=92_899,
                    ),
                    usage(
                        input_tokens=24_070,
                        cached_input_tokens=17_792,
                        output_tokens=366,
                        reasoning_output_tokens=114,
                        total_tokens=24_436,
                    ),
                ),
            )
            for total, last in snapshots:
                resources.record_event(
                    bootstrap,
                    "thread/tokenUsage/updated",
                    token_event(
                        "turn-bootstrap",
                        "thread-root",
                        total=total,
                        last=last,
                    ),
                )

            self.assertEqual(
                resources.turn_usage(bootstrap),
                CodexTokenUsage(
                    input_tokens=76_149,
                    cached_input_tokens=37_888,
                    output_tokens=1_345,
                    reasoning_output_tokens=291,
                    total_tokens=77_494,
                ),
            )
            snapshot = resources.usage_snapshot()

        self.assertEqual(snapshot["schema_version"], "codex-usage/v1")
        self.assertTrue(snapshot["observed"])
        self.assertEqual(snapshot["turns_started"], 2)
        self.assertEqual(snapshot["turns_with_usage"], 2)
        self.assertEqual(snapshot["threads_observed"], 1)
        self.assertEqual(
            snapshot["tokens"],
            {
                "input_tokens": 91_533,
                "cached_input_tokens": 40_320,
                "output_tokens": 1_366,
                "reasoning_output_tokens": 301,
                "total_tokens": 92_899,
            },
        )
        self.assertEqual(
            snapshot["by_phase"]["bootstrap_helper_scripts"]["tokens"]["total_tokens"],
            77_494,
        )

    def test_duplicate_and_out_of_order_snapshots_do_not_double_count(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            resources = CodexRuntimeResources(Path(tmp_dir) / "events.jsonl")
            scope = resources.begin_turn(
                phase="repo_map",
                turn_id="turn-1",
                thread_id="thread-1",
            )
            first = token_event(
                "turn-1",
                "thread-1",
                total=usage(
                    input_tokens=90,
                    cached_input_tokens=10,
                    output_tokens=10,
                    reasoning_output_tokens=2,
                    total_tokens=100,
                ),
                last=usage(
                    input_tokens=90,
                    cached_input_tokens=10,
                    output_tokens=10,
                    reasoning_output_tokens=2,
                    total_tokens=100,
                ),
            )
            latest = token_event(
                "turn-1",
                "thread-1",
                total=usage(
                    input_tokens=130,
                    cached_input_tokens=20,
                    output_tokens=20,
                    reasoning_output_tokens=4,
                    total_tokens=150,
                ),
                last=usage(
                    input_tokens=40,
                    cached_input_tokens=10,
                    output_tokens=10,
                    reasoning_output_tokens=2,
                    total_tokens=50,
                ),
            )
            stale = token_event(
                "turn-1",
                "thread-1",
                total=usage(
                    input_tokens=105,
                    cached_input_tokens=15,
                    output_tokens=15,
                    reasoning_output_tokens=3,
                    total_tokens=120,
                ),
                last=usage(
                    input_tokens=15,
                    cached_input_tokens=5,
                    output_tokens=5,
                    reasoning_output_tokens=1,
                    total_tokens=20,
                ),
            )
            for event in (first, latest, latest, stale):
                resources.record_event(scope, "thread/tokenUsage/updated", event)

            snapshot = resources.usage_snapshot()

        self.assertEqual(snapshot["tokens"]["total_tokens"], 150)
        self.assertEqual(snapshot["tokens"]["input_tokens"], 130)

    def test_invalid_or_mismatched_events_are_ignored_until_valid_snake_case_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            resources = CodexRuntimeResources(Path(tmp_dir) / "events.jsonl")
            scope = resources.begin_turn(phase="risk_routing")
            self.assertTrue(
                resources.bind_turn(
                    scope,
                    "turn-1",
                    thread_id="thread-1",
                )
            )
            valid = usage(
                input_tokens=45,
                cached_input_tokens=5,
                output_tokens=5,
                reasoning_output_tokens=1,
                total_tokens=50,
                snake_case=True,
            )
            invalid_events = (
                token_event(
                    "turn-other",
                    "thread-1",
                    total=valid,
                    last=valid,
                    snake_case=True,
                ),
                token_event(
                    "turn-1",
                    "thread-1",
                    total={**valid, "total_tokens": True},
                    last=valid,
                    snake_case=True,
                ),
                token_event(
                    "turn-1",
                    "thread-1",
                    total={**valid, "input_tokens": -1},
                    last=valid,
                    snake_case=True,
                ),
                token_event(
                    "turn-1",
                    "thread-1",
                    total=usage(
                        input_tokens=5,
                        cached_input_tokens=0,
                        output_tokens=0,
                        reasoning_output_tokens=0,
                        total_tokens=5,
                        snake_case=True,
                    ),
                    last=valid,
                    snake_case=True,
                ),
            )
            for event in invalid_events:
                resources.record_event(scope, "thread/tokenUsage/updated", event)
            self.assertIsNone(resources.turn_usage(scope))

            resources.record_event(
                scope,
                "thread/tokenUsage/updated",
                token_event(
                    "turn-1",
                    "thread-1",
                    total=valid,
                    last=valid,
                    snake_case=True,
                ),
            )

            snapshot = resources.usage_snapshot()

        self.assertEqual(snapshot["turns_started"], 1)
        self.assertEqual(snapshot["turns_with_usage"], 1)
        self.assertEqual(snapshot["tokens"]["total_tokens"], 50)

    def test_switch_run_resets_ledger_and_rejects_old_generation_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            resources = CodexRuntimeResources(root / "run-1.jsonl")
            old_scope = resources.begin_turn(
                phase="repo_map",
                turn_id="turn-old",
                thread_id="thread-old",
            )
            old_usage = usage(
                input_tokens=9,
                cached_input_tokens=0,
                output_tokens=1,
                reasoning_output_tokens=0,
                total_tokens=10,
            )
            resources.record_event(
                old_scope,
                "thread/tokenUsage/updated",
                token_event(
                    "turn-old",
                    "thread-old",
                    total=old_usage,
                    last=old_usage,
                ),
            )

            resources.switch_run(root / "run-2.jsonl")
            self.assertFalse(
                resources.record_event(
                    old_scope,
                    "thread/tokenUsage/updated",
                    token_event(
                        "turn-old",
                        "thread-old",
                        total=old_usage,
                        last=old_usage,
                    ),
                )
            )
            self.assertFalse(resources.usage_snapshot()["observed"])

            new_scope = resources.begin_turn(
                phase="repo_map",
                turn_id="turn-new",
                thread_id="thread-new",
            )
            new_usage = usage(
                input_tokens=6,
                cached_input_tokens=1,
                output_tokens=1,
                reasoning_output_tokens=0,
                total_tokens=7,
            )
            resources.record_event(
                new_scope,
                "thread/tokenUsage/updated",
                token_event(
                    "turn-new",
                    "thread-new",
                    total=new_usage,
                    last=new_usage,
                ),
            )

            snapshot = resources.usage_snapshot()

        self.assertEqual(snapshot["tokens"]["total_tokens"], 7)
        self.assertEqual(snapshot["turns_started"], 1)

    def test_concurrent_turn_updates_are_serialized_without_lost_usage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            resources = CodexRuntimeResources(Path(tmp_dir) / "events.jsonl")
            scopes = [
                resources.begin_turn(
                    phase="reviewer_fanout",
                    turn_id=f"turn-{index}",
                    thread_id=f"thread-{index}",
                )
                for index in (1, 2)
            ]
            barrier = threading.Barrier(2)

            def update(scope_index: int, total_tokens: int) -> None:
                scope = scopes[scope_index]
                turn_id = f"turn-{scope_index + 1}"
                thread_id = f"thread-{scope_index + 1}"
                first_total = total_tokens // 2
                first = usage(
                    input_tokens=first_total - 1,
                    cached_input_tokens=1,
                    output_tokens=1,
                    reasoning_output_tokens=0,
                    total_tokens=first_total,
                )
                final = usage(
                    input_tokens=total_tokens - 2,
                    cached_input_tokens=2,
                    output_tokens=2,
                    reasoning_output_tokens=1,
                    total_tokens=total_tokens,
                )
                resources.record_event(
                    scope,
                    "thread/tokenUsage/updated",
                    token_event(turn_id, thread_id, total=first, last=first),
                )
                barrier.wait()
                resources.record_event(
                    scope,
                    "thread/tokenUsage/updated",
                    token_event(
                        turn_id,
                        thread_id,
                        total=final,
                        last=usage(
                            input_tokens=total_tokens - first_total - 1,
                            cached_input_tokens=1,
                            output_tokens=1,
                            reasoning_output_tokens=1,
                            total_tokens=total_tokens - first_total,
                        ),
                    ),
                )

            threads = [
                threading.Thread(target=update, args=(0, 100), daemon=True),
                threading.Thread(target=update, args=(1, 200), daemon=True),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(2)
                self.assertFalse(thread.is_alive())

            snapshot = resources.usage_snapshot()

        self.assertEqual(snapshot["tokens"]["total_tokens"], 300)
        self.assertEqual(snapshot["turns_with_usage"], 2)
        self.assertEqual(snapshot["threads_observed"], 2)
        self.assertEqual(
            snapshot["by_phase"]["reviewer_fanout"]["tokens"]["total_tokens"],
            300,
        )

    def test_clear_preserves_frozen_usage_until_a_new_turn_begins(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            resources = CodexRuntimeResources(Path(tmp_dir) / "events.jsonl")
            scope = resources.begin_turn(
                phase="repo_map",
                turn_id="turn-1",
                thread_id="thread-1",
            )
            first = usage(
                input_tokens=9,
                cached_input_tokens=1,
                output_tokens=1,
                reasoning_output_tokens=0,
                total_tokens=10,
            )
            event = token_event(
                "turn-1",
                "thread-1",
                total=first,
                last=first,
            )
            resources.record_event(scope, "thread/tokenUsage/updated", event)

            resources.clear()

            self.assertEqual(resources.usage_snapshot()["tokens"]["total_tokens"], 10)
            self.assertFalse(resources.record_event(scope, "thread/tokenUsage/updated", event))

            resources.begin_turn(
                phase="risk_routing",
                turn_id="turn-2",
                thread_id="thread-2",
            )
            snapshot = resources.usage_snapshot()

        self.assertFalse(snapshot["observed"])
        self.assertEqual(snapshot["turns_started"], 1)


if __name__ == "__main__":
    unittest.main()
