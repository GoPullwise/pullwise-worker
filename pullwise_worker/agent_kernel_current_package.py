"""Stable public facade for current-package tool invocation behavior."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Callable, Mapping

from . import _generated_agent_task_contract as _contract
from .agent_kernel_current_contract import (
    ABANDON_RESPONSE_SCHEMA_ID,
    AUTHORITY_SCHEMA_ID,
    CURRENT_PACKAGE,
    GRANT_SCHEMA_ID,
    INVOCATION_SCHEMA_ID,
    PACKAGE_SCHEMA_ID,
    REQUEST_SCHEMA_ID,
    TOOL_CATALOG_SCHEMA_ID,
    CurrentPackage,
    _decode_exact_canonical,
    _detached_document,
    _raise_invalid,
    canonical_current_document_bytes,
    canonical_validated_current_bytes,
    seal_current_document,
    validate_current_document,
    verify_current_document_digest,
    verify_current_package,
)
from .agent_kernel_current_package_authority import (
    AgentClaimAbandonResponse,
    ServerAuthorityEnvelope,
    ServerDispatchGrant,
)
from .agent_kernel_gateway_contracts import (
    CheckedInvocation,
    GatewayError,
    ToolDescriptor,
)
from .agent_kernel_r0_read import ReadSourceFileInput


@dataclass(frozen=True, init=False)
class CurrentToolCatalog:
    canonical_bytes: bytes
    catalog_digest: str
    _descriptors: Mapping[str, ToolDescriptor] = field(repr=False)

    def __init__(self, document: object) -> None:
        complete = verify_current_document_digest(
            TOOL_CATALOG_SCHEMA_ID,
            document,
        )
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
HistoricalAuthorityResolver = Callable[[str, str], ServerAuthorityEnvelope | None]


def _reparse_authority(value: object, code: str) -> ServerAuthorityEnvelope:
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
            current_authority,
            "CURRENT_AUTHORITY_INVALID",
        )
        if historical_resolver is not None and not callable(historical_resolver):
            raise GatewayError("HISTORICAL_AUTHORITY_RESOLVER_INVALID")
        self.historical_resolver = historical_resolver

    def _authority_for(self, idempotency_key: str) -> ServerAuthorityEnvelope:
        if self.historical_resolver is None:
            return self.current_authority
        try:
            found = self.historical_resolver(self.current_authority.task_id, idempotency_key)
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
