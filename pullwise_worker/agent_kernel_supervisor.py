"""One-slot shadow projection over the legacy active marker and terminal outbox."""

from __future__ import annotations

from dataclasses import dataclass
import threading
from typing import Mapping


LEGACY_ACTIVE_STATES = frozenset(
    {"leased", "busy", "cancelling", "failure_handling", "finishing"}
)
LEGACY_OUTBOX_STATES = frozenset({"ready", "submitting", "pending", "blocked"})
IDENTITY_KEYS = ("job_id", "run_id", "lease_id", "attempt_id")


class SupervisorProjectionError(RuntimeError):
    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


@dataclass(frozen=True)
class SupervisorSlotProjection:
    slot_state: str
    task_lifecycle: str | None
    desired_state: str | None
    job_id: str | None
    run_id: str | None
    lease_id: str | None
    attempt_id: str | None
    active_jobs: int
    available_job_slots: int
    maintains_local_queue: bool
    local_queue_depth: int
    terminal_outbox_state: str | None
    terminal_authority: str


def _identity(record: Mapping[str, object], label: str) -> dict[str, str]:
    result = {key: str(record.get(key) or "").strip() for key in IDENTITY_KEYS}
    missing = [key for key, value in result.items() if not value]
    if missing:
        raise SupervisorProjectionError(
            "TRANSPORT_IDENTITY_MISMATCH", f"{label} missing {','.join(missing)}"
        )
    return result


def project_legacy_slot(
    active_marker: Mapping[str, object] | None,
    terminal_outbox: Mapping[str, object] | None,
) -> SupervisorSlotProjection:
    if not active_marker:
        if terminal_outbox:
            raise SupervisorProjectionError(
                "TRANSPORT_IDENTITY_MISMATCH", "outbox has no active marker"
            )
        return SupervisorSlotProjection(
            "IDLE", None, None, None, None, None, None,
            0, 1, False, 0, None, "legacy_v1",
        )

    identity = _identity(active_marker, "active marker")
    state = str(active_marker.get("state") or "").strip().lower()
    if state not in LEGACY_ACTIVE_STATES:
        raise SupervisorProjectionError(
            "STATE_TRANSITION_INVALID", f"legacy active state {state!r}"
        )
    outbox_state = None
    outbox_result_status = None
    if terminal_outbox:
        if terminal_outbox.get("schema_version") != "terminal-result-outbox/v1":
            raise SupervisorProjectionError(
                "TRANSPORT_IDENTITY_MISMATCH", "outbox schema"
            )
        outbox_identity = _identity(terminal_outbox, "terminal outbox")
        if outbox_identity != identity:
            raise SupervisorProjectionError(
                "TRANSPORT_IDENTITY_MISMATCH", "active marker/outbox binding"
            )
        outbox_state = str(terminal_outbox.get("state") or "").strip().lower()
        if outbox_state not in LEGACY_OUTBOX_STATES:
            raise SupervisorProjectionError(
                "STATE_TRANSITION_INVALID", f"legacy outbox state {outbox_state!r}"
            )
        outbox_result_status = str(
            terminal_outbox.get("result_status") or ""
        ).strip().lower()
        if outbox_result_status not in {
            "done", "failed", "cancelled", "partial_completed"
        }:
            raise SupervisorProjectionError(
                "STATE_TRANSITION_INVALID", "legacy outbox result status"
            )

    prepared = active_marker.get("terminal_result_prepared") is True
    if prepared or outbox_state is not None or state in {"failure_handling", "finishing"}:
        lifecycle, desired = "FINALIZING", (
            "CANCEL"
            if state == "cancelling" or outbox_result_status == "cancelled"
            else "RUN"
        )
    elif state == "cancelling":
        lifecycle, desired = "ACTIVE", "CANCEL"
    else:
        lifecycle, desired = "ACTIVE", "RUN"
    return SupervisorSlotProjection(
        "ACTIVE", lifecycle, desired,
        identity["job_id"], identity["run_id"], identity["lease_id"],
        identity["attempt_id"], 1, 0, False, 0, outbox_state, "legacy_v1",
    )


class LegacySlotMirror:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._projection = project_legacy_slot(None, None)
        self.last_error: str | None = None

    def observe(
        self,
        active_marker: Mapping[str, object] | None,
        terminal_outbox: Mapping[str, object] | None,
    ) -> SupervisorSlotProjection:
        projected = project_legacy_slot(active_marker, terminal_outbox)
        with self._lock:
            current = self._projection
            if (
                current.slot_state == "ACTIVE"
                and projected.slot_state == "ACTIVE"
                and current.run_id != projected.run_id
            ):
                raise SupervisorProjectionError(
                    "STATE_TRANSITION_INVALID", "second active slot binding"
                )
            self._projection = projected
            self.last_error = None
            return projected

    def record_error(self, error: BaseException) -> None:
        with self._lock:
            self.last_error = str(error)

    def snapshot(self) -> SupervisorSlotProjection:
        with self._lock:
            return self._projection
