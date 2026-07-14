from __future__ import annotations

import math
import statistics
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable


ESTIMATE_BASIS = "current_run_work_graph"


@dataclass
class ResourcePool:
    configured_concurrency: int
    effective_concurrency: int


@dataclass
class WorkUnit:
    unit_id: str
    kind: str
    resource_pool: str
    order: int = 0
    weight: float = 1.0
    state: str = "pending"
    duration_seconds: float | None = None
    started_at_monotonic: float | None = None


class CurrentRunEstimator:
    """Estimate unfinished work using observations from this run only."""

    def __init__(
        self,
        *,
        monotonic_clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self._monotonic_clock = monotonic_clock
        self._wall_clock = wall_clock
        self._pools: dict[str, ResourcePool] = {}
        self._units: dict[str, WorkUnit] = {}
        self._plan_ready = False

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
        self._units[normalized_id] = WorkUnit(
            unit_id=normalized_id,
            kind=str(kind),
            resource_pool=str(resource_pool),
            order=int(order),
            weight=normalized_weight,
            state=str(state),
            duration_seconds=normalized_duration,
        )

    def start_work_unit(
        self,
        unit_id: str,
        *,
        started_at_monotonic: float | None = None,
    ) -> None:
        unit = self._units[str(unit_id)]
        if unit.state not in {'pending', 'retrying'}:
            raise ValueError(f'work unit cannot start from state {unit.state}')
        started_at = (
            self._monotonic_clock()
            if started_at_monotonic is None
            else float(started_at_monotonic)
        )
        if not math.isfinite(started_at):
            raise ValueError('started_at_monotonic must be finite')
        unit.state = 'active'
        unit.started_at_monotonic = started_at

    def mark_plan_ready(self) -> None:
        self._plan_ready = True

    def snapshot(self) -> dict[str, object]:
        updated_at = datetime.fromtimestamp(
            self._wall_clock(),
            tz=timezone.utc,
        ).isoformat().replace("+00:00", "Z")
        parallel = self._parallel_snapshot()
        if not self._plan_ready:
            return {
                "state": "estimating",
                "basis": ESTIMATE_BASIS,
                "updatedAt": updated_at,
                "parallel": parallel,
            }

        remaining_by_pool: dict[str, float] = {}
        sample_counts: list[int] = []
        for pool_id, pool in self._pools.items():
            pending = sorted(
                (
                    unit
                    for unit in self._units.values()
                    if unit.resource_pool == pool_id and unit.state in {"pending", "retrying"}
                ),
                key=lambda unit: (unit.order, unit.unit_id),
            )
            active = [
                unit
                for unit in self._units.values()
                if unit.resource_pool == pool_id and unit.state == 'active'
            ]
            if not pending and not active:
                continue
            if pool.effective_concurrency < 1:
                return {
                    "state": "unavailable",
                    "basis": ESTIMATE_BASIS,
                    "updatedAt": updated_at,
                    "parallel": parallel,
                }
            rates_by_kind = self._sample_rates()
            lanes = [0.0] * pool.effective_concurrency
            active_residuals = []
            for unit in active:
                rates = rates_by_kind.get(unit.kind, [])
                if not rates:
                    return {
                        'state': 'estimating',
                        'basis': ESTIMATE_BASIS,
                        'updatedAt': updated_at,
                        'parallel': parallel,
                    }
                sample_counts.append(len(rates))
                predicted = statistics.median(rates) * unit.weight
                elapsed = max(
                    0.0,
                    self._monotonic_clock() - float(unit.started_at_monotonic or 0.0),
                )
                if elapsed < predicted:
                    residual = predicted - elapsed
                else:
                    residual = max(1.0, predicted * 0.25, elapsed * 0.5)
                active_residuals.append(residual)
            active_residuals.sort()
            if len(active_residuals) > len(lanes):
                active_residuals = active_residuals[-len(lanes):]
            for index, residual in enumerate(active_residuals):
                lanes[index] = residual
            for unit in pending:
                rates = rates_by_kind.get(unit.kind, [])
                if not rates:
                    return {
                        "state": "estimating",
                        "basis": ESTIMATE_BASIS,
                        "updatedAt": updated_at,
                        "parallel": parallel,
                    }
                sample_counts.append(len(rates))
                predicted = statistics.median(rates) * unit.weight
                lane_index = min(range(len(lanes)), key=lambda index: (lanes[index], index))
                lanes[lane_index] += predicted
            remaining_by_pool[pool_id] = max(lanes, default=0.0)

        remaining = max(remaining_by_pool.values(), default=0.0)
        confidence = self._confidence(sample_counts)
        lower_factor, upper_factor = {
            "low": (0.6, 1.6),
            "medium": (0.8, 1.3),
            "high": (0.9, 1.15),
        }[confidence]
        remaining_seconds = max(0, math.ceil(remaining))
        return {
            "state": "available",
            "basis": ESTIMATE_BASIS,
            "remainingSeconds": remaining_seconds,
            "lowerSeconds": max(0, math.floor(remaining * lower_factor)),
            "upperSeconds": max(remaining_seconds, math.ceil(remaining * upper_factor)),
            "confidence": confidence,
            "updatedAt": updated_at,
            "parallel": parallel,
        }

    def _sample_rates(self) -> dict[str, list[float]]:
        samples: dict[str, list[float]] = {}
        for unit in self._units.values():
            if unit.state not in {"completed", "failed"}:
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
    def _confidence(sample_counts: list[int]) -> str:
        minimum = min(sample_counts, default=1)
        if minimum >= 5:
            return "high"
        if minimum >= 3:
            return "medium"
        return "low"
