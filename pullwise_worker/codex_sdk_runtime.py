from __future__ import annotations

import json
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


@dataclass(frozen=True)
class CodexTokenUsage:
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_output_tokens: int = 0
    total_tokens: int = 0

    @classmethod
    def from_payload(cls, value: object) -> CodexTokenUsage | None:
        if not isinstance(value, Mapping):
            return None

        def count(camel_case: str, snake_case: str) -> int | None:
            raw = value.get(camel_case) if camel_case in value else value.get(snake_case)
            if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
                return None
            return raw

        fields = {
            "input_tokens": count("inputTokens", "input_tokens"),
            "cached_input_tokens": count("cachedInputTokens", "cached_input_tokens"),
            "output_tokens": count("outputTokens", "output_tokens"),
            "reasoning_output_tokens": count("reasoningOutputTokens", "reasoning_output_tokens"),
            "total_tokens": count("totalTokens", "total_tokens"),
        }
        if any(item is None for item in fields.values()):
            return None
        return cls(**fields)  # type: ignore[arg-type]

    def plus(self, other: CodexTokenUsage) -> CodexTokenUsage:
        return CodexTokenUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            cached_input_tokens=self.cached_input_tokens + other.cached_input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            reasoning_output_tokens=self.reasoning_output_tokens + other.reasoning_output_tokens,
            total_tokens=self.total_tokens + other.total_tokens,
        )

    def minus(self, other: CodexTokenUsage) -> CodexTokenUsage | None:
        values = {
            "input_tokens": self.input_tokens - other.input_tokens,
            "cached_input_tokens": self.cached_input_tokens - other.cached_input_tokens,
            "output_tokens": self.output_tokens - other.output_tokens,
            "reasoning_output_tokens": self.reasoning_output_tokens - other.reasoning_output_tokens,
            "total_tokens": self.total_tokens - other.total_tokens,
        }
        if any(item < 0 for item in values.values()):
            return None
        return CodexTokenUsage(**values)

    def is_at_least(self, other: CodexTokenUsage) -> bool:
        return (
            self.input_tokens >= other.input_tokens
            and self.cached_input_tokens >= other.cached_input_tokens
            and self.output_tokens >= other.output_tokens
            and self.reasoning_output_tokens >= other.reasoning_output_tokens
            and self.total_tokens >= other.total_tokens
        )

    def payload(self) -> dict[str, int]:
        return {
            "input_tokens": self.input_tokens,
            "cached_input_tokens": self.cached_input_tokens,
            "output_tokens": self.output_tokens,
            "reasoning_output_tokens": self.reasoning_output_tokens,
            "total_tokens": self.total_tokens,
        }


@dataclass
class TurnEventScope:
    path: Path
    generation: int
    active: bool = True
    phase: str = ""
    turn_id: str = ""
    thread_id: str = ""


@dataclass
class _TurnUsageState:
    turn_id: str
    phase: str
    thread_id: str = ""
    baseline: CodexTokenUsage | None = None
    latest_total: CodexTokenUsage | None = None
    usage: CodexTokenUsage | None = None


class CodexRuntimeResources:
    """Owns run-scoped event sinks and transient SDK thread references."""

    def __init__(self, events_path: Path) -> None:
        self.events_path = Path(events_path)
        self.events_path.parent.mkdir(parents=True, exist_ok=True)
        self.threads: dict[str, Any] = {}
        self.events_lock = threading.Lock()
        self.threads_lock = threading.Lock()
        self._generation = 0
        self._usage_generation = self._generation
        self._turn_usage: dict[str, _TurnUsageState] = {}

    def switch_run(self, events_path: Path) -> None:
        next_path = Path(events_path)
        next_path.parent.mkdir(parents=True, exist_ok=True)
        # Serializing the generation change with event writes guarantees that
        # no prior-run consumer can pass validation and write after this call.
        with self.events_lock:
            self.events_path = next_path
            self._generation += 1
            self._reset_usage_locked()
        with self.threads_lock:
            self.threads.clear()

    def begin_turn(
        self,
        *,
        phase: str = "",
        turn_id: str = "",
        thread_id: str = "",
    ) -> TurnEventScope:
        with self.events_lock:
            self._ensure_usage_generation_locked()
            scope = TurnEventScope(
                path=self.events_path,
                generation=self._generation,
                phase=self._phase_name(phase),
            )
            if turn_id:
                self._bind_turn_locked(scope, turn_id, thread_id=thread_id)
            return scope

    def bind_turn(
        self,
        scope: TurnEventScope,
        turn_id: str,
        *,
        thread_id: str = "",
        phase: str | None = None,
    ) -> bool:
        with self.events_lock:
            if not scope.active or scope.generation != self._generation:
                return False
            self._ensure_usage_generation_locked()
            if phase is not None:
                scope.phase = self._phase_name(phase)
            self._bind_turn_locked(scope, turn_id, thread_id=thread_id)
            return True

    def abandon_turn(self, scope: TurnEventScope) -> None:
        # Once this returns, an in-flight write has either completed or will
        # observe active=False. run_turn may therefore safely return afterward.
        with self.events_lock:
            scope.active = False

    def record_event(self, scope: TurnEventScope, method: str, params: dict[str, Any]) -> bool:
        with self.events_lock:
            if not scope.active or scope.generation != self._generation:
                return False
            scope.path.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps(
                {"method": str(method or ""), "params": params},
                ensure_ascii=False,
                sort_keys=True,
            )
            with scope.path.open("a", encoding="utf-8") as handle:
                handle.write(payload + "\n")
            self._record_usage_locked(scope, method, params)
            return True

    def turn_usage(self, scope: TurnEventScope) -> CodexTokenUsage | None:
        with self.events_lock:
            if scope.generation != self._usage_generation or not scope.turn_id:
                return None
            state = self._turn_usage.get(scope.turn_id)
            return state.usage if state is not None else None

    def usage_snapshot(self) -> dict[str, Any]:
        with self.events_lock:
            observed_states = [state for state in self._turn_usage.values() if state.usage is not None]
            totals = CodexTokenUsage()
            phase_totals: dict[str, CodexTokenUsage] = {}
            phase_turns: dict[str, int] = {}
            phase_threads: dict[str, set[str]] = {}
            threads: set[str] = set()
            for state in observed_states:
                if state.usage is None:
                    continue
                totals = totals.plus(state.usage)
                phase_totals[state.phase] = phase_totals.get(state.phase, CodexTokenUsage()).plus(state.usage)
                phase_turns[state.phase] = phase_turns.get(state.phase, 0) + 1
                if state.thread_id:
                    threads.add(state.thread_id)
                    phase_threads.setdefault(state.phase, set()).add(state.thread_id)
            by_phase = {
                phase: {
                    "turns": phase_turns[phase],
                    "threads": len(phase_threads.get(phase, set())),
                    "tokens": phase_totals[phase].payload(),
                }
                for phase in sorted(phase_totals)
            }
            return {
                "schema_version": "codex-usage/v1",
                "observed": bool(observed_states),
                "turns_started": len(self._turn_usage),
                "turns_with_usage": len(observed_states),
                "threads_observed": len(threads),
                "tokens": totals.payload(),
                "by_phase": by_phase,
            }

    def register_thread(self, thread_id: str, thread: Any) -> None:
        if not thread_id:
            return
        with self.threads_lock:
            self.threads[thread_id] = thread

    def release_thread(self, thread_id: str) -> None:
        if not thread_id:
            return
        with self.threads_lock:
            self.threads.pop(thread_id, None)

    def clear(self) -> None:
        with self.events_lock:
            # Keep the last run's usage available for terminal result creation
            # after a failed SDK runtime is closed. A later switch_run(), or the
            # first turn in a replacement runtime generation, starts a new ledger.
            self._generation += 1
        with self.threads_lock:
            self.threads.clear()

    @staticmethod
    def _phase_name(value: object) -> str:
        return str(value or "").strip() or "unattributed"

    @staticmethod
    def _mapping_value(value: Mapping[str, Any], camel_case: str, snake_case: str) -> Any:
        return value.get(camel_case) if camel_case in value else value.get(snake_case)

    def _ensure_usage_generation_locked(self) -> None:
        if self._usage_generation != self._generation:
            self._reset_usage_locked()

    def _reset_usage_locked(self) -> None:
        self._usage_generation = self._generation
        self._turn_usage.clear()

    def _bind_turn_locked(
        self,
        scope: TurnEventScope,
        turn_id: object,
        *,
        thread_id: object = "",
    ) -> None:
        normalized_turn_id = str(turn_id or "").strip()
        if not normalized_turn_id:
            raise ValueError("turn_id is required for Codex usage tracking")
        normalized_thread_id = str(thread_id or "").strip()
        if scope.turn_id and scope.turn_id != normalized_turn_id:
            raise RuntimeError("turn event scope is already bound to another turn")
        phase = self._phase_name(scope.phase)
        existing = self._turn_usage.get(normalized_turn_id)
        if existing is not None:
            if existing.phase != phase:
                raise RuntimeError("Codex turn is already registered to another phase")
            if existing.thread_id and normalized_thread_id and existing.thread_id != normalized_thread_id:
                raise RuntimeError("Codex turn is already registered to another thread")
            if not existing.thread_id:
                existing.thread_id = normalized_thread_id
        else:
            self._turn_usage[normalized_turn_id] = _TurnUsageState(
                turn_id=normalized_turn_id,
                phase=phase,
                thread_id=normalized_thread_id,
            )
        scope.turn_id = normalized_turn_id
        scope.thread_id = normalized_thread_id or (existing.thread_id if existing is not None else "")

    def _record_usage_locked(
        self,
        scope: TurnEventScope,
        method: object,
        params: object,
    ) -> None:
        if str(method or "") != "thread/tokenUsage/updated":
            return
        if not scope.turn_id or not isinstance(params, Mapping):
            return
        state = self._turn_usage.get(scope.turn_id)
        if state is None:
            return
        event_turn_id = str(self._mapping_value(params, "turnId", "turn_id") or "").strip()
        if event_turn_id != scope.turn_id:
            return
        event_thread_id = str(self._mapping_value(params, "threadId", "thread_id") or "").strip()
        if scope.thread_id and event_thread_id and event_thread_id != scope.thread_id:
            return
        if event_thread_id and not state.thread_id:
            state.thread_id = event_thread_id
            scope.thread_id = event_thread_id
        raw_usage = self._mapping_value(params, "tokenUsage", "token_usage")
        if not isinstance(raw_usage, Mapping):
            return
        total = CodexTokenUsage.from_payload(raw_usage.get("total"))
        last = CodexTokenUsage.from_payload(raw_usage.get("last"))
        if total is None or last is None:
            return
        if state.baseline is None:
            baseline = total.minus(last)
            if baseline is None:
                return
            state.baseline = baseline
        if state.latest_total is not None:
            if total.total_tokens <= state.latest_total.total_tokens:
                return
            if not total.is_at_least(state.latest_total):
                return
        current_usage = total.minus(state.baseline)
        if current_usage is None:
            return
        state.latest_total = total
        state.usage = current_usage


def run_bounded_call(
    call: Callable[[], Any],
    *,
    timeout_seconds: float,
    timeout_message: str,
    cancel_requested: Callable[[], bool] | None = None,
    cancelled_error: Callable[[], BaseException] | None = None,
    late_result: Callable[[Any], None] | None = None,
) -> Any:
    """Run a blocking SDK call without letting it block the worker caller."""

    completed = threading.Event()
    state_lock = threading.Lock()
    state: dict[str, Any] = {"abandoned": False}

    def invoke() -> None:
        try:
            result = call()
            with state_lock:
                state["result"] = result
                abandoned = bool(state["abandoned"])
            if abandoned and late_result is not None:
                try:
                    late_result(result)
                except Exception:
                    pass
        except BaseException as exc:  # noqa: BLE001 - caller must receive SDK failures unchanged.
            with state_lock:
                state["error"] = exc
        finally:
            completed.set()

    threading.Thread(target=invoke, name="pullwise-codex-bounded-rpc", daemon=True).start()
    deadline = time.monotonic() + max(0.001, float(timeout_seconds))
    while not completed.is_set():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            with state_lock:
                if "result" not in state and "error" not in state:
                    state["abandoned"] = True
                    raise TimeoutError(timeout_message)
            break
        if completed.wait(min(0.1, remaining)):
            break
        if cancel_requested is not None and cancel_requested():
            with state_lock:
                if "result" not in state and "error" not in state:
                    state["abandoned"] = True
                    if cancelled_error is not None:
                        raise cancelled_error()
                    raise RuntimeError("cancel requested")
            break

    with state_lock:
        error = state.get("error")
        result = state.get("result")
    if isinstance(error, BaseException):
        raise error
    return result


def require_identifier(value: object, *, label: str) -> str:
    identifier = str(value or "").strip()
    if not identifier:
        raise RuntimeError(f"Codex SDK returned no {label}")
    return identifier
