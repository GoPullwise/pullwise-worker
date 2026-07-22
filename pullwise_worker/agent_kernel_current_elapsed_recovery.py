"""Rebuild a process-local execution deadline from durable elapsed budget."""

from __future__ import annotations

from dataclasses import dataclass

from .agent_kernel_current_budget import CurrentBudgetError


@dataclass(frozen=True)
class RebuiltExecutionWindow:
    execution_window_ms: int
    local_monotonic_deadline_ms: int


def rebuild_execution_window(
    *,
    absolute_deadline_ms: int,
    trusted_wall_ms: int,
    durable_control_wall_ms: int,
    hard_wall_ms: int,
    durable_consumed_ms: int,
    active_reserved_ms: int,
    local_monotonic_now_ms: int,
) -> RebuiltExecutionWindow:
    if (
        isinstance(trusted_wall_ms, bool)
        or not isinstance(trusted_wall_ms, int)
        or trusted_wall_ms < 0
    ):
        raise CurrentBudgetError("CONTRACT_INVALID")

    if trusted_wall_ms < durable_control_wall_ms:
        raise CurrentBudgetError("CONTRACT_INVALID")
    absolute_remaining_ms = max(0, absolute_deadline_ms - trusted_wall_ms)
    budget_remaining_ms = max(
        0,
        hard_wall_ms - durable_consumed_ms - active_reserved_ms,
    )
    execution_window_ms = min(absolute_remaining_ms, budget_remaining_ms)
    return RebuiltExecutionWindow(
        execution_window_ms=execution_window_ms,
        local_monotonic_deadline_ms=(
            local_monotonic_now_ms + execution_window_ms
        ),
    )


__all__ = ["RebuiltExecutionWindow", "rebuild_execution_window"]
