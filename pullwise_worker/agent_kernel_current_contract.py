"""Current generated-contract primitives and the Worker package pin."""

from __future__ import annotations

from dataclasses import dataclass
import json

from . import _generated_agent_task_contract as _contract
from .agent_kernel_gateway_contracts import GatewayError


PACKAGE_SCHEMA_ID = "current-package-ref/v1"
GRANT_SCHEMA_ID = "agent-worker-grant/v1"
AUTHORITY_SCHEMA_ID = "server-authority-envelope/v1"
ABANDON_RESPONSE_SCHEMA_ID = "agent-claim-abandon-response/v1"
REQUEST_SCHEMA_ID = "agent-tool-request/v1"
INVOCATION_SCHEMA_ID = "tool-invocation/v1"
TOOL_CATALOG_SCHEMA_ID = "tool-catalog/v1"


def canonical_current_document_bytes(value: object) -> bytes:
    return _contract.canonical_document_bytes(value)


def validate_current_document(schema_id: str, value: object) -> dict[str, object]:
    return _contract.validate_document(schema_id, value)


def canonical_validated_current_bytes(schema_id: str, value: object) -> bytes:
    return _contract.canonical_validated_bytes(schema_id, value)


def seal_current_document(schema_id: str, unsigned_value: object) -> dict[str, object]:
    return _contract.seal_document(schema_id, unsigned_value)


def verify_current_document_digest(schema_id: str, complete_value: object) -> dict[str, object]:
    return _contract.verify_document_digest(schema_id, complete_value)


def _error_detail(error: BaseException) -> str:
    code = getattr(error, "code", None)
    path = getattr(error, "path", None)
    if isinstance(code, str) and isinstance(path, str):
        return f"{code}:{path}"
    if isinstance(code, str):
        return code
    return type(error).__name__


def _raise_invalid(code: str, error: BaseException) -> None:
    raise GatewayError(code, _error_detail(error)) from error


def _detached_document(raw: bytes) -> dict[str, object]:
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict):
        raise TypeError("document must be an object")
    return value


def _decode_exact_canonical(
    raw: bytes,
    *,
    noncanonical_code: str,
    invalid_code: str,
) -> dict[str, object]:
    if not isinstance(raw, bytes):
        raise GatewayError(invalid_code)
    try:
        document = _detached_document(raw)
        canonical = canonical_current_document_bytes(document)
    except Exception as exc:
        _raise_invalid(invalid_code, exc)
    if canonical != raw:
        raise GatewayError(noncanonical_code)
    return document


@dataclass(frozen=True)
class CurrentPackage:
    package_identity: str
    package_version: str
    content_sha256: str
    root_sha256: str

    def as_tuple(self) -> tuple[str, str, str, str]:
        return (
            self.package_identity,
            self.package_version,
            self.content_sha256,
            self.root_sha256,
        )

    def as_document(self) -> dict[str, object]:
        return {
            "schema_id": PACKAGE_SCHEMA_ID,
            "package_identity": self.package_identity,
            "package_version": self.package_version,
            "content_sha256": self.content_sha256,
            "root_sha256": self.root_sha256,
        }


def _build_current_package() -> CurrentPackage:
    package_tuple = _contract.PACKAGE_TUPLE
    if (
        not isinstance(package_tuple, tuple)
        or len(package_tuple) != 4
        or any(
            not isinstance(item, str) or not item for item in package_tuple
        )
    ):
        raise GatewayError("CURRENT_PACKAGE_PIN_INVALID")
    return CurrentPackage(*package_tuple)


CURRENT_PACKAGE = _build_current_package()


def _require_current_package(value: object) -> CurrentPackage:
    if value != CURRENT_PACKAGE.as_document():
        raise GatewayError("CURRENT_PACKAGE_PIN_MISMATCH")
    return CURRENT_PACKAGE


def verify_current_package() -> CurrentPackage:
    try:
        verified = _contract.verify_bundle()
        package_document = _contract.package_tuple()
        canonical_validated_current_bytes(PACKAGE_SCHEMA_ID, package_document)
    except Exception as exc:
        _raise_invalid("CURRENT_PACKAGE_PIN_INVALID", exc)
    if (
        verified is not True
        or _contract.PACKAGE_TUPLE != CURRENT_PACKAGE.as_tuple()
        or package_document != CURRENT_PACKAGE.as_document()
    ):
        raise GatewayError("CURRENT_PACKAGE_PIN_MISMATCH")
    return CURRENT_PACKAGE
