"""Rollbackable runtime seam for the Agent Kernel one-slot shadow mirror."""

from __future__ import annotations

import os
from typing import Any, TypeVar

from .agent_kernel_supervisor import (
    LegacySlotMirror,
    SupervisorProjectionError,
    SupervisorSlotProjection,
)
from .review_worker_v1 import ActiveJob, ReviewWorkerV1


LegacyWorker = TypeVar("LegacyWorker")


def _shadow_enabled(config: object) -> bool:
    configured = getattr(config, "agent_kernel_shadow_enabled", None)
    raw = configured if configured is not None else os.environ.get(
        "PULLWISE_AGENT_KERNEL_SHADOW_ENABLED", "false"
    )
    if isinstance(raw, bool):
        return raw
    normalized = str(raw or "").strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off", ""}:
        return False
    raise ValueError("PULLWISE_AGENT_KERNEL_SHADOW_ENABLED must be true or false")


class AgentKernelShadowReviewWorker(ReviewWorkerV1):
    """Observes legacy durable authority; it never publishes or queues work."""

    def __init__(self, config: Any, client: Any | None = None) -> None:
        self._agent_kernel_slot = LegacySlotMirror()
        super().__init__(config, client=client)

    def agent_kernel_slot_snapshot(self) -> SupervisorSlotProjection:
        return self._agent_kernel_slot.snapshot()

    @property
    def agent_kernel_shadow_error(self) -> str | None:
        return self._agent_kernel_slot.last_error

    def _observe_legacy_authority(self) -> None:
        try:
            marker = self.read_active_run_marker()
            if not marker:
                previous = self._agent_kernel_slot.snapshot()
                previous_run_id = previous.run_id
                if previous_run_id:
                    receipt = self._persisted_submit_marker(
                        previous_run_id, "result-submit-succeeded.json"
                    )
                    if receipt:
                        expected = {
                            "job_id": previous.job_id,
                            "run_id": previous.run_id,
                            "lease_id": previous.lease_id,
                            "attempt_id": previous.attempt_id,
                        }
                        if (
                            receipt.get("status") != "result_submit_succeeded"
                            or any(receipt.get(key) != value for key, value in expected.items())
                        ):
                            raise SupervisorProjectionError(
                                "TRANSPORT_IDENTITY_MISMATCH",
                                "terminal success receipt binding",
                            )
                        self._agent_kernel_slot.observe(None, None)
                        return
                    outbox, error = self._load_persisted_terminal_outbox(
                        previous_run_id
                    )
                    if error:
                        raise RuntimeError(error)
                    if outbox:
                        self._agent_kernel_slot.observe(None, outbox)
                self._agent_kernel_slot.observe(None, None)
                return
            run_id = str(marker.get("run_id") or "").strip()
            outbox, error = self._load_persisted_terminal_outbox(run_id)
            if error:
                raise RuntimeError(error)
            self._agent_kernel_slot.observe(marker, outbox or None)
        except Exception as exc:
            # Shadow failure must remain observable without taking legacy authority down.
            self._agent_kernel_slot.record_error(exc)

    def recover_persisted_active_job(self) -> ActiveJob | None:
        active = super().recover_persisted_active_job()
        self._observe_legacy_authority()
        return active

    def persist_active_run_marker(self, active: ActiveJob) -> None:
        super().persist_active_run_marker(active)
        self._observe_legacy_authority()

    def clear_active_run_marker(self, active: ActiveJob | None = None) -> None:
        super().clear_active_run_marker(active)
        self._observe_legacy_authority()


def build_review_worker(
    config: Any,
    *,
    client: Any | None,
    legacy_class: type[LegacyWorker] = ReviewWorkerV1,
) -> LegacyWorker | AgentKernelShadowReviewWorker:
    if not _shadow_enabled(config):
        return legacy_class(config, client=client)
    return AgentKernelShadowReviewWorker(config, client=client)


__all__ = [
    "AgentKernelShadowReviewWorker",
    "build_review_worker",
]
