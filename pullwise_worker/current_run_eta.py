from __future__ import annotations

import math
import statistics
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable


ESTIMATE_BASIS = "current_run_work_graph"
FINISHED_STATES = {"completed", "failed", "cancelled"}
UNFINISHED_STATES = {"pending", "active", "retrying"}


@dataclass
class ResourcePool:
    configured_concurrency: int
    effective_concurrency: int


@dataclass
class WorkUnit:
    unit_id: str
    kind: str
    resource_pool: str
    dependencies: tuple[str, ...] = ()
    order: int = 0
    weight: float = 1.0
    state: str = "pending"
    duration_seconds: float | None = None
    started_at_monotonic: float | None = None
    completed_at_monotonic: float | None = None


@dataclass(frozen=True)
class ScheduleResult:
    state: str
    remaining_seconds: float = 0.0
    sample_counts: tuple[int, ...] = ()
    has_overrun: bool = False


class CurrentRunEstimator:
    """Estimate a resource-constrained graph using observations from one run."""

    def __init__(
        self,
        *,
        monotonic_clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        deadline_monotonic: float | None = None,
    ) -> None:
        self._monotonic_clock = monotonic_clock
        self._wall_clock = wall_clock
        self._deadline_monotonic = deadline_monotonic
        self._pools: dict[str, ResourcePool] = {}
        self._units: dict[str, WorkUnit] = {}
        self._plan_ready = False
        self._terminal = False

    def set_resource_pool(
        self,
        pool_id: str,
        *,
        configured_concurrency: int,
        effective_concurrency: int,
    ) -> None:
        configured = int(configured_concurrency)
        effective = int(effective_concurrency)
        if configured < 1:
            raise ValueError("configured_concurrency must be at least 1")
        if effective < 0:
            raise ValueError("effective_concurrency must be non-negative")
        self._pools[str(pool_id)] = ResourcePool(configured, effective)

    def add_work_unit(
        self,
        unit_id: str,
        *,
        kind: str,
        resource_pool: str,
        dependencies: tuple[str, ...] = (),
        order: int = 0,
        weight: float = 1.0,
        state: str = "pending",
        duration_seconds: float | None = None,
    ) -> None:
        normalized_id = str(unit_id)
        if normalized_id in self._units:
            raise ValueError(f"duplicate work unit: {normalized_id}")
        normalized_weight = float(weight)
        if not math.isfinite(normalized_weight) or normalized_weight <= 0:
            raise ValueError("work unit weight must be finite and positive")
        normalized_duration = None
        if duration_seconds is not None:
            normalized_duration = float(duration_seconds)
            if not math.isfinite(normalized_duration) or normalized_duration < 0:
                raise ValueError("duration_seconds must be finite and non-negative")
        normalized_state = str(state)
        if normalized_state not in FINISHED_STATES | UNFINISHED_STATES:
            raise ValueError(f"unsupported work unit state: {normalized_state}")
        self._units[normalized_id] = WorkUnit(
            unit_id=normalized_id,
            kind=str(kind),
            resource_pool=str(resource_pool),
            dependencies=tuple(str(value) for value in dependencies),
            order=int(order),
            weight=normalized_weight,
            state=normalized_state,
            duration_seconds=normalized_duration,
        )

    def has_work_unit(self, unit_id: str) -> bool:
        return str(unit_id) in self._units

    def work_unit_state(self, unit_id: str) -> str | None:
        unit = self._units.get(str(unit_id))
        return unit.state if unit is not None else None

    def replace_dependencies(self, unit_id: str, dependencies: tuple[str, ...]) -> None:
        self._units[str(unit_id)].dependencies = tuple(str(value) for value in dependencies)

    def start_work_unit(
        self,
        unit_id: str,
        *,
        started_at_monotonic: float | None = None,
    ) -> None:
        unit = self._units[str(unit_id)]
        if unit.state not in {"pending", "retrying"}:
            raise ValueError(f"work unit cannot start from state {unit.state}")
        started_at = (
            self._monotonic_clock()
            if started_at_monotonic is None
            else float(started_at_monotonic)
        )
        if not math.isfinite(started_at):
            raise ValueError("started_at_monotonic must be finite")
        unit.state = "active"
        unit.started_at_monotonic = started_at

    def finish_work_unit(
        self,
        unit_id: str,
        *,
        completed_at_monotonic: float | None = None,
        duration_seconds: float | None = None,
        state: str = 'completed',
    ) -> None:
        unit = self._units[str(unit_id)]
        if unit.state in FINISHED_STATES:
            return
        normalized_state = str(state)
        if normalized_state not in FINISHED_STATES:
            raise ValueError('finished work unit state must be terminal')
        completed_at = (
            self._monotonic_clock()
            if completed_at_monotonic is None
            else float(completed_at_monotonic)
        )
        if not math.isfinite(completed_at):
            raise ValueError('completed_at_monotonic must be finite')
        if duration_seconds is None:
            if unit.started_at_monotonic is None:
                measured_duration = 0.0
            else:
                measured_duration = completed_at - unit.started_at_monotonic
        else:
            measured_duration = float(duration_seconds)
        if not math.isfinite(measured_duration) or measured_duration < 0:
            raise ValueError('work unit duration must be finite and non-negative')
        unit.state = normalized_state
        unit.completed_at_monotonic = completed_at
        unit.duration_seconds = measured_duration

    def mark_plan_ready(self) -> None:
        self._plan_ready = True

    def mark_terminal(self) -> None:
        self._terminal = True

    def snapshot(self) -> dict[str, object] | None:
        if self._terminal:
            return None
        now_monotonic = self._monotonic_clock()
        updated_at = datetime.fromtimestamp(
            self._wall_clock(),
            tz=timezone.utc,
        ).isoformat().replace("+00:00", "Z")
        parallel = self._parallel_snapshot()
        base = {
            "basis": ESTIMATE_BASIS,
            "updatedAt": updated_at,
            "parallel": parallel,
        }
        if not self._plan_ready:
            return {"state": "estimating", **base}

        schedule = self._schedule(now_monotonic)
        if schedule.state != "available":
            return {"state": schedule.state, **base}

        remaining = schedule.remaining_seconds
        if self._deadline_monotonic is not None:
            deadline_remaining = max(0.0, self._deadline_monotonic - now_monotonic)
            remaining = min(remaining, deadline_remaining)
        confidence = self._confidence(schedule.sample_counts, schedule.has_overrun)
        lower_factor, upper_factor = {
            "low": (0.6, 1.6),
            "medium": (0.8, 1.3),
            "high": (0.9, 1.15),
        }[confidence]
        lower = remaining * lower_factor
        upper = remaining * upper_factor
        if self._deadline_monotonic is not None:
            deadline_remaining = max(0.0, self._deadline_monotonic - now_monotonic)
            lower = min(lower, deadline_remaining)
            upper = min(upper, deadline_remaining)
        remaining_seconds = max(0, math.ceil(remaining))
        return {
            "state": "available",
            **base,
            "remainingSeconds": remaining_seconds,
            "lowerSeconds": max(0, min(remaining_seconds, math.floor(lower))),
            "upperSeconds": max(remaining_seconds, math.ceil(upper)),
            "confidence": confidence,
        }

    def _schedule(self, now_monotonic: float) -> ScheduleResult:
        unfinished = [unit for unit in self._units.values() if unit.state in UNFINISHED_STATES]
        if not unfinished:
            return ScheduleResult("available")
        if any(dependency not in self._units for unit in unfinished for dependency in unit.dependencies):
            return ScheduleResult("estimating")

        pool_units: dict[str, list[WorkUnit]] = {}
        for unit in unfinished:
            pool_units.setdefault(unit.resource_pool, []).append(unit)
        for pool_id in pool_units:
            pool = self._pools.get(pool_id)
            if pool is None:
                return ScheduleResult("estimating")
            if pool.effective_concurrency < 1:
                return ScheduleResult("unavailable")

        rates_by_kind = self._sample_rates()
        predicted: dict[str, float] = {}
        sample_counts: list[int] = []
        has_overrun = False
        for unit in unfinished:
            rates = rates_by_kind.get(unit.kind, [])
            if not rates:
                return ScheduleResult("estimating")
            sample_counts.append(len(rates))
            service_time = statistics.median(rates) * unit.weight
            if unit.state == "active":
                if unit.started_at_monotonic is None:
                    return ScheduleResult("estimating")
                elapsed = max(0.0, now_monotonic - unit.started_at_monotonic)
                if elapsed < service_time:
                    service_time -= elapsed
                else:
                    has_overrun = True
                    service_time = max(1.0, service_time * 0.25, elapsed * 0.5)
            predicted[unit.unit_id] = service_time

        completion_times = {
            unit.unit_id: 0.0
            for unit in self._units.values()
            if unit.state in FINISHED_STATES
        }
        lanes_by_pool: dict[str, list[float]] = {}
        for pool_id, pool in self._pools.items():
            active_units = [unit for unit in pool_units.get(pool_id, []) if unit.state == "active"]
            active_residuals = sorted(predicted[unit.unit_id] for unit in active_units)
            if len(active_residuals) > pool.effective_concurrency:
                active_residuals = active_residuals[-pool.effective_concurrency :]
            lanes = active_residuals + [0.0] * (pool.effective_concurrency - len(active_residuals))
            lanes_by_pool[pool_id] = lanes
            for unit in active_units:
                completion_times[unit.unit_id] = predicted[unit.unit_id]

        pending = sorted(
            (unit for unit in unfinished if unit.state != "active"),
            key=lambda unit: (unit.order, unit.unit_id),
        )
        while pending:
            scheduled_any = False
            for unit in list(pending):
                if any(dependency not in completion_times for dependency in unit.dependencies):
                    continue
                dependency_ready = max(
                    (completion_times[dependency] for dependency in unit.dependencies),
                    default=0.0,
                )
                lanes = lanes_by_pool[unit.resource_pool]
                lane_index = min(
                    range(len(lanes)),
                    key=lambda index: (max(dependency_ready, lanes[index]), index),
                )
                start = max(dependency_ready, lanes[lane_index])
                finish = start + predicted[unit.unit_id]
                lanes[lane_index] = finish
                completion_times[unit.unit_id] = finish
                pending.remove(unit)
                scheduled_any = True
            if not scheduled_any:
                return ScheduleResult("unavailable")

        remaining = max(
            (completion_times.get(unit.unit_id, 0.0) for unit in unfinished),
            default=0.0,
        )
        return ScheduleResult(
            "available",
            remaining_seconds=remaining,
            sample_counts=tuple(sample_counts),
            has_overrun=has_overrun,
        )

    def _sample_rates(self) -> dict[str, list[float]]:
        samples: dict[str, list[float]] = {}
        for unit in self._units.values():
            if unit.state not in FINISHED_STATES:
                continue
            if unit.duration_seconds is None or unit.duration_seconds <= 0:
                continue
            samples.setdefault(unit.kind, []).append(unit.duration_seconds / unit.weight)
        return samples

    def _parallel_snapshot(self) -> dict[str, int]:
        pool = self._pools.get("reviewer")
        units = [unit for unit in self._units.values() if unit.resource_pool == "reviewer"]
        return {
            "configuredConcurrency": pool.configured_concurrency if pool else 0,
            "effectiveConcurrency": pool.effective_concurrency if pool else 0,
            "activeUnits": sum(unit.state == "active" for unit in units),
            "pendingUnits": sum(unit.state == "pending" for unit in units),
            "retryingUnits": sum(unit.state == "retrying" for unit in units),
        }

    @staticmethod
    def _confidence(sample_counts: tuple[int, ...], has_overrun: bool) -> str:
        if has_overrun:
            return "low"
        minimum = min(sample_counts, default=1)
        if minimum >= 5:
            return "high"
        if minimum >= 3:
            return "medium"
        return "low"
