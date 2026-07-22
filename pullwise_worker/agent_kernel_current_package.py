"""Worker-owned typed facade over the Server-generated current contract."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from types import MappingProxyType
from typing import Callable, Mapping

from . import _generated_agent_task_contract as _contract
from .agent_kernel_gateway_contracts import (
    CheckedInvocation,
    GatewayError,
    ToolDescriptor,
)
from .agent_kernel_r0_read import ReadSourceFileInput


PACKAGE_SCHEMA_ID = "current-package-ref/v1"
GRANT_SCHEMA_ID = "agent-worker-grant/v1"
AUTHORITY_SCHEMA_ID = "server-authority-envelope/v1"
ABANDON_RESPONSE_SCHEMA_ID = "agent-claim-abandon-response/v1"
REQUEST_SCHEMA_ID = "agent-tool-request/v1"
INVOCATION_SCHEMA_ID = "tool-invocation/v1"
TOOL_CATALOG_SCHEMA_ID = "tool-catalog/v1"


def canonical_current_document_bytes(value: object) -> bytes:
    return _contract.canonical_document_bytes(value)


def validate_current_document(
    schema_id: str, value: object
) -> dict[str, object]:
    return _contract.validate_document(schema_id, value)


def canonical_validated_current_bytes(schema_id: str, value: object) -> bytes:
    return _contract.canonical_validated_bytes(schema_id, value)


def seal_current_document(
    schema_id: str, unsigned_value: object
) -> dict[str, object]:
    return _contract.seal_document(schema_id, unsigned_value)


def verify_current_document_digest(
    schema_id: str, complete_value: object
) -> dict[str, object]:
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
        or any(not isinstance(item, str) or not item for item in package_tuple)
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


@dataclass(frozen=True)
class ServerDispatchGrant:
    package: CurrentPackage
    grant_id: str
    task_id: str
    attempt_id: str
    session_id: str
    owner_id: str
    lease_id: str
    task_version: int
    deletion_version: int
    owner_epoch: int
    native_epoch: int
    transport_epoch: int
    policy_digest: str
    capability_ids: tuple[str, ...]
    tool_keys: tuple[str, ...]
    elapsed_limit_ms: int
    tool_call_limit: int
    grant_digest: str
    canonical_bytes: bytes

    @property
    def digest(self) -> str:
        return self.grant_digest

    def as_document(self) -> dict[str, object]:
        return _detached_document(self.canonical_bytes)

    @classmethod
    def from_document(cls, value: object) -> "ServerDispatchGrant":
        complete = verify_current_document_digest(GRANT_SCHEMA_ID, value)
        package = _require_current_package(complete["package"])
        canonical = canonical_validated_current_bytes(GRANT_SCHEMA_ID, complete)
        return cls(
            package=package,
            grant_id=complete["grant_id"],
            task_id=complete["task_id"],
            attempt_id=complete["attempt_id"],
            session_id=complete["session_id"],
            owner_id=complete["owner_id"],
            lease_id=complete["lease_id"],
            task_version=complete["task_version"],
            deletion_version=complete["deletion_version"],
            owner_epoch=complete["owner_epoch"],
            native_epoch=complete["native_epoch"],
            transport_epoch=complete["transport_epoch"],
            policy_digest=complete["policy_digest"],
            capability_ids=tuple(complete["capability_ids"]),
            tool_keys=tuple(complete["tool_keys"]),
            elapsed_limit_ms=complete["elapsed_limit_ms"],
            tool_call_limit=complete["tool_call_limit"],
            grant_digest=complete["grant_digest"],
            canonical_bytes=canonical,
        )


_GRANT_BOUND_FIELDS = (
    "task_id",
    "attempt_id",
    "session_id",
    "owner_id",
    "lease_id",
    "task_version",
    "deletion_version",
    "owner_epoch",
    "native_epoch",
    "transport_epoch",
)


def _grant_matches_authority(
    document: dict[str, object], grant: ServerDispatchGrant
) -> bool:
    if document.get("package") != grant.package.as_document():
        return False
    return all(document.get(name) == getattr(grant, name) for name in _GRANT_BOUND_FIELDS)


@dataclass(frozen=True)
class ServerAuthorityEnvelope:
    package: CurrentPackage
    grant: ServerDispatchGrant
    task_id: str
    attempt_id: str
    session_id: str
    owner_id: str
    lease_id: str
    task_version: int
    deletion_version: int
    owner_epoch: int
    native_epoch: int
    transport_epoch: int
    lifecycle: str
    desired_state: str
    authority_digest: str
    canonical_bytes: bytes

    @property
    def digest(self) -> str:
        return self.authority_digest

    @property
    def grant_digest(self) -> str:
        return self.grant.grant_digest

    def as_document(self) -> dict[str, object]:
        return _detached_document(self.canonical_bytes)

    @classmethod
    def from_canonical_bytes(cls, raw: bytes) -> "ServerAuthorityEnvelope":
        document = _decode_exact_canonical(
            raw,
            noncanonical_code="AUTHORITY_ENVELOPE_NONCANONICAL",
            invalid_code="AUTHORITY_ENVELOPE_INVALID",
        )
        try:
            complete = verify_current_document_digest(AUTHORITY_SCHEMA_ID, document)
            package = _require_current_package(complete["package"])
            grant = ServerDispatchGrant.from_document(complete["grant"])
        except Exception as exc:
            _raise_invalid("AUTHORITY_ENVELOPE_INVALID", exc)
        if not _grant_matches_authority(complete, grant):
            raise GatewayError("AUTHORITY_GRANT_BINDING_MISMATCH")
        return cls(
            package=package,
            grant=grant,
            task_id=complete["task_id"],
            attempt_id=complete["attempt_id"],
            session_id=complete["session_id"],
            owner_id=complete["owner_id"],
            lease_id=complete["lease_id"],
            task_version=complete["task_version"],
            deletion_version=complete["deletion_version"],
            owner_epoch=complete["owner_epoch"],
            native_epoch=complete["native_epoch"],
            transport_epoch=complete["transport_epoch"],
            lifecycle=complete["lifecycle"],
            desired_state=complete["desired_state"],
            authority_digest=complete["authority_digest"],
            canonical_bytes=raw,
        )


_ABANDON_BINDING_FIELDS = (
    "task_id",
    "attempt_id",
    "session_id",
    "owner_id",
    "grant_id",
    "lease_id",
    "deletion_version",
    "owner_epoch",
    "native_epoch",
    "transport_epoch",
)


def _grant_matches_abandonment(
    document: dict[str, object], grant: ServerDispatchGrant
) -> bool:
    exact = document.get("package") == grant.package.as_document()
    exact = exact and all(
        document.get(name) == getattr(grant, name)
        for name in _ABANDON_BINDING_FIELDS
    )
    return bool(
        exact
        and document.get("previous_task_version") == grant.task_version
        and document.get("task_version") == grant.task_version + 1
    )


@dataclass(frozen=True)
class AgentClaimAbandonResponse:
    package: CurrentPackage
    grant: ServerDispatchGrant
    superseded_authority_digest: str
    task_id: str
    attempt_id: str
    session_id: str
    owner_id: str
    grant_id: str
    lease_id: str
    previous_task_version: int
    task_version: int
    deletion_version: int
    owner_epoch: int
    native_epoch: int
    transport_epoch: int
    state: str
    reason: str
    abandoned_at: str
    response_digest: str
    canonical_bytes: bytes

    @property
    def digest(self) -> str:
        return self.response_digest

    def as_document(self) -> dict[str, object]:
        return _detached_document(self.canonical_bytes)

    @classmethod
    def from_canonical_bytes(cls, raw: bytes) -> "AgentClaimAbandonResponse":
        document = _decode_exact_canonical(
            raw,
            noncanonical_code="ABANDON_RESPONSE_NONCANONICAL",
            invalid_code="ABANDON_RESPONSE_INVALID",
        )
        try:
            package = _require_current_package(document["package"])
            grant = ServerDispatchGrant.from_document(document["grant"])
        except Exception as exc:
            _raise_invalid("ABANDON_RESPONSE_INVALID", exc)
        if not _grant_matches_abandonment(document, grant):
            raise GatewayError("ABANDON_RESPONSE_GRANT_BINDING_MISMATCH")
        try:
            complete = verify_current_document_digest(
                ABANDON_RESPONSE_SCHEMA_ID, document
            )
        except Exception as exc:
            _raise_invalid("ABANDON_RESPONSE_INVALID", exc)
        return cls(
            package=package,
            grant=grant,
            superseded_authority_digest=complete["superseded_authority_digest"],
            task_id=complete["task_id"],
            attempt_id=complete["attempt_id"],
            session_id=complete["session_id"],
            owner_id=complete["owner_id"],
            grant_id=complete["grant_id"],
            lease_id=complete["lease_id"],
            previous_task_version=complete["previous_task_version"],
            task_version=complete["task_version"],
            deletion_version=complete["deletion_version"],
            owner_epoch=complete["owner_epoch"],
            native_epoch=complete["native_epoch"],
            transport_epoch=complete["transport_epoch"],
            state=complete["state"],
            reason=complete["reason"],
            abandoned_at=complete["abandoned_at"],
            response_digest=complete["response_digest"],
            canonical_bytes=raw,
        )


@dataclass(frozen=True, init=False)
class CurrentToolCatalog:
    canonical_bytes: bytes
    catalog_digest: str
    _descriptors: Mapping[str, ToolDescriptor] = field(repr=False)

    def __init__(self, document: object) -> None:
        complete = verify_current_document_digest(TOOL_CATALOG_SCHEMA_ID, document)
        object.__setattr__(
            self,
            "canonical_bytes",
            canonical_validated_current_bytes(TOOL_CATALOG_SCHEMA_ID, complete),
        )
        object.__setattr__(self, "catalog_digest", complete["catalog_digest"])
        descriptors: dict[str, ToolDescriptor] = {}
        for item in complete["tools"]:
            descriptor = ToolDescriptor(
                tool_key=item["tool_key"],
                tool_version=item["tool_version"],
                risk=item["risk"],
                capability=item["capability_id"],
                uses_command=item["uses_command"],
                uses_network=item["uses_network"],
                uses_secret=item["uses_secret"],
                requests_approval=item["requests_approval"],
            )
            if descriptor.tool_key in descriptors:
                raise GatewayError("TOOL_CATALOG_INVALID")
            descriptors[descriptor.tool_key] = descriptor
        object.__setattr__(self, "_descriptors", MappingProxyType(descriptors))

    def as_document(self) -> dict[str, object]:
        return _detached_document(self.canonical_bytes)

    def resolve(self, tool_key: str) -> ToolDescriptor:
        try:
            return self._descriptors[tool_key]
        except (KeyError, TypeError) as exc:
            raise GatewayError("TOOL_NOT_FOUND") from exc


CURRENT_TOOL_CATALOG = CurrentToolCatalog(_contract.tool_catalog())


HistoricalAuthorityResolver = Callable[
    [str, str], ServerAuthorityEnvelope | None
]


def _reparse_authority(
    value: object, code: str
) -> ServerAuthorityEnvelope:
    if not isinstance(value, ServerAuthorityEnvelope):
        raise GatewayError(code)
    try:
        parsed = ServerAuthorityEnvelope.from_canonical_bytes(value.canonical_bytes)
    except Exception as exc:
        _raise_invalid(code, exc)
    if parsed != value:
        raise GatewayError(code)
    return parsed


class CurrentInvocationCodec:
    def __init__(
        self,
        current_authority: ServerAuthorityEnvelope,
        historical_resolver: HistoricalAuthorityResolver | None = None,
    ) -> None:
        self.current_authority = _reparse_authority(
            current_authority, "CURRENT_AUTHORITY_INVALID"
        )
        if historical_resolver is not None and not callable(historical_resolver):
            raise GatewayError("HISTORICAL_AUTHORITY_RESOLVER_INVALID")
        self.historical_resolver = historical_resolver

    def _authority_for(self, idempotency_key: str) -> ServerAuthorityEnvelope:
        if self.historical_resolver is None:
            return self.current_authority
        try:
            found = self.historical_resolver(
                self.current_authority.task_id, idempotency_key
            )
        except Exception as exc:
            _raise_invalid("HISTORICAL_AUTHORITY_INVALID", exc)
        if found is None:
            return self.current_authority
        parsed = _reparse_authority(found, "HISTORICAL_AUTHORITY_INVALID")
        if parsed.task_id != self.current_authority.task_id:
            raise GatewayError("HISTORICAL_AUTHORITY_INVALID")
        return parsed

    def validate(self, raw: bytes) -> CheckedInvocation:
        request = _decode_exact_canonical(
            raw,
            noncanonical_code="AGENT_TOOL_REQUEST_NONCANONICAL",
            invalid_code="AGENT_TOOL_REQUEST_INVALID",
        )
        try:
            request = validate_current_document(REQUEST_SCHEMA_ID, request)
        except Exception as exc:
            _raise_invalid("AGENT_TOOL_REQUEST_INVALID", exc)
        authority = self._authority_for(request["idempotency_key"])
        descriptor = CURRENT_TOOL_CATALOG.resolve(request["tool_key"])
        if descriptor.tool_key != request["tool_key"]:
            raise GatewayError("TOOL_DESCRIPTOR_IDENTITY_MISMATCH")
        try:
            tool_input = ReadSourceFileInput(request["tool_input"]["relative_path"])
        except Exception as exc:
            _raise_invalid("AGENT_TOOL_REQUEST_INVALID", exc)
        invocation = {
            "schema_id": INVOCATION_SCHEMA_ID,
            "package": CURRENT_PACKAGE.as_document(),
            "authority_digest": authority.authority_digest,
            "grant_digest": authority.grant_digest,
            "task_id": authority.task_id,
            "attempt_id": authority.attempt_id,
            "session_id": authority.session_id,
            "owner_id": authority.owner_id,
            "lease_id": authority.lease_id,
            "task_version": authority.task_version,
            "deletion_version": authority.deletion_version,
            "owner_epoch": authority.owner_epoch,
            "native_epoch": authority.native_epoch,
            "transport_epoch": authority.transport_epoch,
            "idempotency_key": request["idempotency_key"],
            "tool_key": request["tool_key"],
            "tool_input": request["tool_input"],
        }
        try:
            sealed = seal_current_document(INVOCATION_SCHEMA_ID, invocation)
            verify_current_document_digest(INVOCATION_SCHEMA_ID, sealed)
        except Exception as exc:
            _raise_invalid("INVOCATION_DERIVATION_INVALID", exc)
        return CheckedInvocation(
            idempotency_key=request["idempotency_key"],
            invocation_digest=sealed["invocation_digest"],
            authority_digest=authority.authority_digest,
            package_content_sha256=authority.package.content_sha256,
            package_root_sha256=authority.package.root_sha256,
            grant_digest=authority.grant_digest,
            task_id=authority.task_id,
            attempt_id=authority.attempt_id,
            owner_id=authority.owner_id,
            session_id=authority.session_id,
            lease_id=authority.lease_id,
            task_version=authority.task_version,
            deletion_version=authority.deletion_version,
            owner_epoch=authority.owner_epoch,
            native_epoch=authority.native_epoch,
            transport_epoch=authority.transport_epoch,
            tool_key=request["tool_key"],
            tool_input=tool_input,
        )


__all__ = [
    "AgentClaimAbandonResponse",
    "CURRENT_PACKAGE",
    "CURRENT_TOOL_CATALOG",
    "CurrentInvocationCodec",
    "CurrentPackage",
    "CurrentToolCatalog",
    "ServerAuthorityEnvelope",
    "ServerDispatchGrant",
    "canonical_current_document_bytes",
    "canonical_validated_current_bytes",
    "seal_current_document",
    "validate_current_document",
    "verify_current_document_digest",
    "verify_current_package",
]
