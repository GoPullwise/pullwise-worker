"""Immutable projections of Server documents bound to the current package."""

from __future__ import annotations

from dataclasses import dataclass

from .agent_kernel_current_contract import (
    ABANDON_RESPONSE_SCHEMA_ID,
    AUTHORITY_SCHEMA_ID,
    GRANT_SCHEMA_ID,
    CurrentPackage,
    _decode_exact_canonical,
    _detached_document,
    _raise_invalid,
    _require_current_package,
    canonical_validated_current_bytes,
    verify_current_document_digest,
)
from .agent_kernel_gateway_contracts import GatewayError


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
    absolute_deadline_at: str
    terminalization_reserve_ms: int
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
            absolute_deadline_at=complete["absolute_deadline_at"],
            terminalization_reserve_ms=complete["terminalization_reserve_ms"],
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
    "absolute_deadline_at",
    "terminalization_reserve_ms",
)


def _grant_matches_authority(document: dict[str, object], grant: ServerDispatchGrant) -> bool:
    return document.get("package") == grant.package.as_document() and all(
        document.get(name) == getattr(grant, name) for name in _GRANT_BOUND_FIELDS
    )


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
    absolute_deadline_at: str
    terminalization_reserve_ms: int
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
            absolute_deadline_at=complete["absolute_deadline_at"],
            terminalization_reserve_ms=complete["terminalization_reserve_ms"],
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


def _grant_matches_abandonment(document: dict[str, object], grant: ServerDispatchGrant) -> bool:
    exact = document.get("package") == grant.package.as_document() and all(
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
            complete = verify_current_document_digest(ABANDON_RESPONSE_SCHEMA_ID, document)
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
