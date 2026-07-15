from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


@dataclass
class TurnEventScope:
    path: Path
    generation: int
    active: bool = True


class CodexRuntimeResources:
    """Owns run-scoped event sinks and transient SDK thread references."""

    def __init__(self, events_path: Path) -> None:
        self.events_path = Path(events_path)
        self.events_path.parent.mkdir(parents=True, exist_ok=True)
        self.threads: dict[str, Any] = {}
        self.events_lock = threading.Lock()
        self.threads_lock = threading.Lock()
        self._generation = 0

    def switch_run(self, events_path: Path) -> None:
        next_path = Path(events_path)
        next_path.parent.mkdir(parents=True, exist_ok=True)
        # Serializing the generation change with event writes guarantees that
        # no prior-run consumer can pass validation and write after this call.
        with self.events_lock:
            self.events_path = next_path
            self._generation += 1
        with self.threads_lock:
            self.threads.clear()

    def begin_turn(self) -> TurnEventScope:
        with self.events_lock:
            return TurnEventScope(path=self.events_path, generation=self._generation)

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
            return True

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
            self._generation += 1
        with self.threads_lock:
            self.threads.clear()


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
