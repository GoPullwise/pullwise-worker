"""Typed seams shared by the current-only Agent Kernel gateway."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Protocol

from .agent_kernel_source_state import SourceDiff, SourceTreeSnapshot


DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")
RISK_LEVELS = frozenset({"R0", "R1", "R2", "R3", "R4"})


def _valid_int(value: object, minimum: int) -> bool:
    return not isinstance(value, bool) and isinstance(value, int) and value >= minimum


class GatewayError(RuntimeError):
    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


@dataclass(frozen=True)
class CheckedInvocation:
    """Package-codec output whose digest covers all canonical invocation facts."""

    idempotency_key: str
    invocation_digest: str
    authority_digest: str
    package_content_sha256: str
    package_root_sha256: str
    grant_digest: str
    task_id: str
    attempt_id: str
    owner_id: str
    session_id: str
    lease_id: str
    task_version: int
    deletion_version: int
    owner_epoch: int
    native_epoch: int
    transport_epoch: int
    tool_key: str
    tool_input: object = None

    def __post_init__(self) -> None:
        text = (
            self.idempotency_key,
            self.task_id,
            self.attempt_id,
            self.owner_id,
            self.session_id,
            self.lease_id,
            self.tool_key,
        )
        if any(not isinstance(value, str) or not value for value in text):
            raise GatewayError("INVOCATION_FACTS_INVALID")
        digests = (
            (self.invocation_digest, "INVOCATION_DIGEST_INVALID"),
            (self.authority_digest, "AUTHORITY_DIGEST_INVALID"),
            (self.package_content_sha256, "PACKAGE_DIGEST_INVALID"),
            (self.package_root_sha256, "PACKAGE_DIGEST_INVALID"),
            (self.grant_digest, "GRANT_DIGEST_INVALID"),
        )
        for value, code in digests:
            if not isinstance(value, str) or not DIGEST_PATTERN.fullmatch(value):
                raise GatewayError(code)
        if not _valid_int(self.task_version, 1):
            raise GatewayError("TASK_VERSION_INVALID")
        if not _valid_int(self.deletion_version, 0):
            raise GatewayError("DELETION_VERSION_INVALID")
        epochs = (self.owner_epoch, self.native_epoch, self.transport_epoch)
        if any(not _valid_int(value, 1) for value in epochs):
            raise GatewayError("INVOCATION_EPOCH_INVALID")


@dataclass(frozen=True)
class ToolDescriptor:
    tool_key: str
    tool_version: str
    risk: str
    capability: str
    uses_command: bool
    uses_network: bool
    uses_secret: bool
    requests_approval: bool

    def __post_init__(self) -> None:
        if not all((self.tool_key, self.tool_version, self.capability)) or (
            self.risk not in RISK_LEVELS
        ):
            raise GatewayError("TOOL_DESCRIPTOR_INVALID")
        controls = (
            self.uses_command,
            self.uses_network,
            self.uses_secret,
            self.requests_approval,
        )
        if any(not isinstance(value, bool) for value in controls):
            raise GatewayError("TOOL_DESCRIPTOR_INVALID")


@dataclass(frozen=True)
class PreparedDispatch:
    """Opaque dispatcher input; unresolved Agent input is deliberately absent."""

    tool_key: str
    tool_version: str
    source_before: SourceTreeSnapshot
    dispatch_handle: object


@dataclass(frozen=True)
class ReplayState:
    kind: str
    result: object | None = None

    @classmethod
    def new(cls) -> "ReplayState":
        return cls("NEW")

    @classmethod
    def pending(cls) -> "ReplayState":
        return cls("PENDING")

    @classmethod
    def completed(cls, result: object) -> "ReplayState":
        return cls("COMPLETED", result)


@dataclass(frozen=True)
class DispatchDecision:
    kind: str
    dispatch_capability: object | None = None
    result: object | None = None

    @classmethod
    def winner(cls, capability: object) -> "DispatchDecision":
        return cls("WINNER", dispatch_capability=capability)

    @classmethod
    def pending(cls) -> "DispatchDecision":
        return cls("PENDING")

    @classmethod
    def completed(cls, result: object) -> "DispatchDecision":
        return cls("COMPLETED", result=result)


class InvocationCodec(Protocol):
    """Validate package bytes and derive trusted facts, never from tool input."""

    def validate(self, raw: bytes) -> CheckedInvocation: ...


class DispatchJournal(Protocol):
    """Sole durable begin owner: revalidate, reserve, persist, and bind."""

    def probe(self, key: str, digest: str) -> ReplayState: ...

    def begin(
        self,
        authority_ticket: object,
        call: CheckedInvocation,
        descriptor: ToolDescriptor,
        prepared: PreparedDispatch,
        reservation_plan: object,
    ) -> DispatchDecision: ...


class CurrentAuthority(Protocol):
    """Preflight checks only; journal begin must durably revalidate all facts."""

    def assert_actor_current(self, call: CheckedInvocation) -> object: ...

    def assert_lease_current(self, ticket: object, call: CheckedInvocation) -> None: ...

    def assert_runnable(self, ticket: object, call: CheckedInvocation) -> None: ...


class ToolCatalog(Protocol):
    def resolve(self, tool_key: str) -> ToolDescriptor: ...


class PolicyAuthority(Protocol):
    """Pure preflight policy checks; this seam owns no durable transaction."""

    def assert_capability(
        self,
        ticket: object,
        call: CheckedInvocation,
        descriptor: ToolDescriptor,
    ) -> None: ...

    def assert_execution_controls(
        self,
        ticket: object,
        call: CheckedInvocation,
        descriptor: ToolDescriptor,
        prepared: PreparedDispatch,
    ) -> None: ...


class InvocationPreparer(Protocol):
    def prepare(
        self,
        ticket: object,
        call: CheckedInvocation,
        descriptor: ToolDescriptor,
    ) -> PreparedDispatch: ...

    def capture_after(self, prepared: PreparedDispatch) -> SourceTreeSnapshot: ...

    def discard(self, prepared: PreparedDispatch) -> None: ...


class BudgetPlanner(Protocol):
    """Pure proposal builder; it never durably reserves or releases budget."""

    def plan_reservation(
        self,
        ticket: object,
        call: CheckedInvocation,
        descriptor: ToolDescriptor,
    ) -> object: ...


class Dispatcher(Protocol):
    def dispatch(self, capability: object, prepared: PreparedDispatch) -> object: ...


class ExecutionCommitter(Protocol):
    """Settle only the winner identified by a journal-issued capability."""

    def commit(
        self,
        dispatch_capability: object,
        call: CheckedInvocation,
        prepared: PreparedDispatch,
        receipt: object,
        source_after: SourceTreeSnapshot,
    ) -> object: ...

    def commit_source_violation(
        self,
        dispatch_capability: object,
        call: CheckedInvocation,
        prepared: PreparedDispatch,
        receipt: object,
        source_after: SourceTreeSnapshot,
        changes: SourceDiff,
    ) -> object: ...

    def commit_source_unavailable(
        self,
        dispatch_capability: object,
        call: CheckedInvocation,
        prepared: PreparedDispatch,
        receipt: object,
        error: Exception,
    ) -> object: ...

    def commit_dispatch_failure(
        self,
        dispatch_capability: object,
        call: CheckedInvocation,
        prepared: PreparedDispatch,
        error: Exception,
    ) -> object: ...


__all__ = [
    "BudgetPlanner",
    "CheckedInvocation",
    "CurrentAuthority",
    "DispatchDecision",
    "Dispatcher",
    "DispatchJournal",
    "ExecutionCommitter",
    "GatewayError",
    "InvocationCodec",
    "InvocationPreparer",
    "PolicyAuthority",
    "PreparedDispatch",
    "ReplayState",
    "ToolCatalog",
    "ToolDescriptor",
]
