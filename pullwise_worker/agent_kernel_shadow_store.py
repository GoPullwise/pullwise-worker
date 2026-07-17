"""Schema-validated shadow persistence that does not own terminal publication."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import threading

from .agent_kernel_canonical import canonical_bytes, load_strict_json
from .agent_kernel_database import AgentKernelDatabase
from .agent_kernel_object_store import (
    CasCorruptError,
    ContentRefConflictError,
    ObjectStore,
)
from .agent_kernel_schema_registry import SchemaRegistry
from .agent_kernel_schema_validation import SchemaValidationError


METRIC_NAMES = (
    "agent_kernel_shadow_contract_writes_total",
    "agent_kernel_shadow_contract_write_bytes_total",
    "agent_kernel_shadow_contract_reads_total",
    "agent_kernel_shadow_contract_read_bytes_total",
    "agent_kernel_shadow_contract_validation_failures_total",
    "agent_kernel_shadow_cas_conflicts_total",
    "agent_kernel_shadow_cas_corruption_total",
)


class AgentKernelShadowMetrics:
    def __init__(self) -> None:
        self._values = {name: 0 for name in METRIC_NAMES}
        self._lock = threading.Lock()

    def add(self, name: str, amount: int = 1) -> None:
        if name not in self._values or amount < 0:
            raise ValueError("shadow_metric_invalid")
        with self._lock:
            self._values[name] += amount

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return dict(self._values)


@dataclass(frozen=True)
class AgentKernelShadowStore:
    database: AgentKernelDatabase
    objects: ObjectStore
    schemas: SchemaRegistry
    metrics: AgentKernelShadowMetrics

    @classmethod
    def open(
        cls, worker_root: Path, *, contract_root: Path | None = None
    ) -> "AgentKernelShadowStore":
        database = AgentKernelDatabase(Path(worker_root))
        database.initialize()
        return cls(
            database=database,
            objects=ObjectStore(database),
            schemas=SchemaRegistry(contract_root),
            metrics=AgentKernelShadowMetrics(),
        )

    def put_contract(
        self,
        *,
        task_id: str,
        artifact_id: str,
        schema_id: str,
        instance: object,
        max_bytes: int = 64 * 1024 * 1024,
    ) -> dict[str, object]:
        try:
            self.schemas.validate(schema_id, instance)
            payload = canonical_bytes(instance)
            ref = self.objects.put_bytes(
                payload,
                task_id=task_id,
                artifact_id=artifact_id,
                media_type="application/json",
                content_schema_id=schema_id,
                encoding="utf-8",
                max_bytes=max_bytes,
            )
        except SchemaValidationError:
            self.metrics.add("agent_kernel_shadow_contract_validation_failures_total")
            raise
        except ContentRefConflictError:
            self.metrics.add("agent_kernel_shadow_cas_conflicts_total")
            raise
        except CasCorruptError:
            self.metrics.add("agent_kernel_shadow_cas_corruption_total")
            raise
        self.metrics.add("agent_kernel_shadow_contract_writes_total")
        self.metrics.add("agent_kernel_shadow_contract_write_bytes_total", len(payload))
        return ref

    def read_contract(self, ref: dict[str, object]) -> object:
        try:
            self.schemas.validate("content-ref/v1", ref)
            schema_id = ref.get("content_schema_id")
            assert isinstance(schema_id, str)
            payload = self.objects.read_verified(ref)
            instance = load_strict_json(payload)
            if canonical_bytes(instance) != payload:
                raise SchemaValidationError("stored_contract_not_canonical")
            self.schemas.validate(schema_id, instance)
        except SchemaValidationError:
            self.metrics.add("agent_kernel_shadow_contract_validation_failures_total")
            raise
        except CasCorruptError:
            self.metrics.add("agent_kernel_shadow_cas_corruption_total")
            raise
        self.metrics.add("agent_kernel_shadow_contract_reads_total")
        self.metrics.add("agent_kernel_shadow_contract_read_bytes_total", len(payload))
        return instance
