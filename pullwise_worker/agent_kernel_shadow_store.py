"""Schema-validated shadow persistence that does not own terminal publication."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .agent_kernel_canonical import canonical_bytes, load_strict_json
from .agent_kernel_database import AgentKernelDatabase
from .agent_kernel_object_store import ObjectStore
from .agent_kernel_schema_registry import SchemaRegistry
from .agent_kernel_schema_validation import SchemaValidationError


@dataclass(frozen=True)
class AgentKernelShadowStore:
    database: AgentKernelDatabase
    objects: ObjectStore
    schemas: SchemaRegistry

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
        self.schemas.validate(schema_id, instance)
        payload = canonical_bytes(instance)
        return self.objects.put_bytes(
            payload,
            task_id=task_id,
            artifact_id=artifact_id,
            media_type="application/json",
            content_schema_id=schema_id,
            encoding="utf-8",
            max_bytes=max_bytes,
        )

    def read_contract(self, ref: dict[str, object]) -> object:
        self.schemas.validate("content-ref/v1", ref)
        schema_id = ref.get("content_schema_id")
        assert isinstance(schema_id, str)
        payload = self.objects.read_verified(ref)
        instance = load_strict_json(payload)
        if canonical_bytes(instance) != payload:
            raise SchemaValidationError("stored_contract_not_canonical")
        self.schemas.validate(schema_id, instance)
        return instance
