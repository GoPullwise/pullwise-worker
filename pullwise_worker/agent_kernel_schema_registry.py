"""Digest-bound registry for Agent Task v1 JSON Schemas."""

from __future__ import annotations

from pathlib import Path, PurePosixPath
import re
import stat
import sysconfig

from .agent_kernel_canonical import (
    CanonicalizationError,
    canonical_bytes,
    canonical_sha256,
    load_strict_json,
)
from .agent_kernel_contract_semantics import validate_contract_semantics
from .agent_kernel_schema_validation import (
    SchemaValidationError,
    validate_instance,
    validate_schema_definition,
)


DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")
SCHEMA_ID_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*/v[1-9][0-9]*$")
SCHEMA_DRAFT = "https://json-schema.org/draft/2020-12/schema"


class SchemaRegistryError(RuntimeError):
    pass


def _default_contract_root() -> Path:
    candidates = (
        Path(__file__).resolve().parents[1] / "contracts" / "agent-task" / "v1",
        Path(sysconfig.get_path("data"))
        / "share"
        / "pullwise-worker"
        / "contracts"
        / "agent-task"
        / "v1",
    )
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    raise SchemaRegistryError("default_schema_root_unavailable")


def _read_regular(path: Path) -> bytes:
    try:
        metadata = path.lstat()
    except FileNotFoundError as exc:
        raise SchemaRegistryError(f"schema_file_missing: {path.name}") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise SchemaRegistryError(f"schema_file_not_regular: {path.name}")
    try:
        return path.read_bytes()
    except OSError as exc:
        raise SchemaRegistryError(f"schema_file_unreadable: {path.name}") from exc


class SchemaRegistry:
    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root) if root is not None else _default_contract_root()
        try:
            root_metadata = self.root.lstat()
        except FileNotFoundError as exc:
            raise SchemaRegistryError("schema_root_missing") from exc
        if stat.S_ISLNK(root_metadata.st_mode) or not stat.S_ISDIR(root_metadata.st_mode):
            raise SchemaRegistryError("schema_root_invalid")
        self._schemas = self._load()
        self.schema_ids = tuple(sorted(self._schemas))

    def validate(self, schema_id: str, instance: object) -> None:
        schema = self._resolve(schema_id)
        try:
            canonical_bytes(instance)
            validate_instance(instance, schema, resolve=self._resolve)
            validate_contract_semantics(schema_id, instance)
        except CanonicalizationError as exc:
            raise SchemaValidationError(exc.code, detail=exc.detail) from exc

    def schema(self, schema_id: str) -> dict[str, object]:
        return dict(self._resolve(schema_id))

    def _resolve(self, schema_id: str) -> dict[str, object]:
        try:
            return self._schemas[schema_id]
        except KeyError as exc:
            raise SchemaRegistryError(f"schema_unknown: {schema_id}") from exc

    def _load(self) -> dict[str, dict[str, object]]:
        try:
            manifest = load_strict_json(_read_regular(self.root / "schema-registry.json"))
        except CanonicalizationError as exc:
            raise SchemaRegistryError(f"schema_registry_invalid: {exc}") from exc
        if not isinstance(manifest, dict) or manifest.get("schema_id") != (
            "agent-task-schema-registry/v1"
        ):
            raise SchemaRegistryError("schema_registry_identity_invalid")
        if set(manifest) != {"schema_id", "schemas"}:
            raise SchemaRegistryError("schema_registry_fields_invalid")
        entries = manifest.get("schemas")
        if not isinstance(entries, list) or not entries:
            raise SchemaRegistryError("schema_registry_entries_invalid")
        schemas: dict[str, dict[str, object]] = {}
        order: list[str] = []
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict) or set(entry) != {
                "schema_id",
                "path",
                "sha256",
            }:
                raise SchemaRegistryError(f"schema_registry_entry_invalid: {index}")
            schema_id = entry.get("schema_id")
            relative = entry.get("path")
            expected_digest = entry.get("sha256")
            if not isinstance(schema_id, str) or not SCHEMA_ID_PATTERN.fullmatch(schema_id):
                raise SchemaRegistryError(f"schema_id_invalid: {index}")
            if schema_id in schemas:
                raise SchemaRegistryError(f"schema_id_duplicate: {schema_id}")
            if not isinstance(relative, str) or not self._canonical_filename(relative):
                raise SchemaRegistryError(f"schema_path_invalid: {index}")
            if not isinstance(expected_digest, str) or not DIGEST_PATTERN.fullmatch(
                expected_digest
            ):
                raise SchemaRegistryError(f"schema_digest_invalid: {index}")
            try:
                schema = load_strict_json(_read_regular(self.root / relative))
            except CanonicalizationError as exc:
                raise SchemaRegistryError(f"schema_json_invalid: {relative}: {exc}") from exc
            if not isinstance(schema, dict):
                raise SchemaRegistryError(f"schema_not_object: {relative}")
            if schema.get("$schema") != SCHEMA_DRAFT or schema.get("$id") != schema_id:
                raise SchemaRegistryError(f"schema_identity_mismatch: {relative}")
            if canonical_sha256(schema) != expected_digest:
                raise SchemaRegistryError(f"schema_digest_mismatch: {relative}")
            try:
                validate_schema_definition(schema)
            except SchemaValidationError as exc:
                raise SchemaRegistryError(f"schema_definition_invalid: {relative}: {exc}") from exc
            schemas[schema_id] = schema
            order.append(schema_id)
        if order != sorted(order):
            raise SchemaRegistryError("schema_registry_order_invalid")
        for schema_id, schema in schemas.items():
            for reference in self._references(schema):
                if reference not in schemas:
                    raise SchemaRegistryError(
                        f"schema_reference_unknown: {schema_id}: {reference}"
                    )
        return schemas

    @staticmethod
    def _canonical_filename(value: str) -> bool:
        path = PurePosixPath(value)
        return (
            len(path.parts) == 1
            and path.name == value
            and value.endswith(".schema.json")
            and value not in {".", ".."}
        )

    @classmethod
    def _references(cls, value: object) -> set[str]:
        if isinstance(value, dict):
            references = {
                reference
                for key, item in value.items()
                if key == "$ref" and isinstance((reference := item), str)
            }
            for item in value.values():
                references.update(cls._references(item))
            return references
        if isinstance(value, list):
            references: set[str] = set()
            for item in value:
                references.update(cls._references(item))
            return references
        return set()


__all__ = [
    "SchemaRegistry",
    "SchemaRegistryError",
    "SchemaValidationError",
]
