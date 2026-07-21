"""Package-independent ordering kernel for current-only tool invocations."""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Protocol

from .agent_kernel_source_state import SourceDiff, SourceTreeSnapshot, diff_source_trees


DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")
RISK_LEVELS = frozenset({"R0", "R1", "R2", "R3", "R4"})

class GatewayError(RuntimeError):
    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


@dataclass(frozen=True)
class CheckedInvocation:
    """Facts validated by the exact-pinned package codec."""

    idempotency_key: str
    invocation_digest: str
    task_id: str
    attempt_id: str
    session_id: str
    owner_epoch: int
    native_epoch: int
    tool_key: str
    tool_input: object = None

    def __post_init__(self) -> None:
        text = (
            self.idempotency_key,
            self.task_id,
            self.attempt_id,
            self.session_id,
            self.tool_key,
        )
        if any(not isinstance(value, str) or not value for value in text):
            raise GatewayError("INVOCATION_FACTS_INVALID")
        if not DIGEST_PATTERN.fullmatch(self.invocation_digest):
            raise GatewayError("INVOCATION_DIGEST_INVALID")
        epochs = (self.owner_epoch, self.native_epoch)
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 1
            for value in epochs
        ):
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
        if (
            not self.tool_key
            or not self.tool_version
            or not self.capability
            or self.risk not in RISK_LEVELS
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
    def validate(self, raw: bytes) -> CheckedInvocation: ...


class DispatchJournal(Protocol):
    """Atomically revalidate authority and bind a winning dispatch capability."""

    def probe(self, key: str, digest: str) -> ReplayState: ...

    def begin(
        self,
        authority_ticket: object,
        call: CheckedInvocation,
        descriptor: ToolDescriptor,
        prepared: PreparedDispatch,
        reservation: object,
    ) -> DispatchDecision: ...


class CurrentAuthority(Protocol):
    def assert_actor_current(self, call: CheckedInvocation) -> object: ...

    def assert_lease_current(self, ticket: object, call: CheckedInvocation) -> None: ...

    def assert_runnable(self, ticket: object, call: CheckedInvocation) -> None: ...


class ToolCatalog(Protocol):
    def resolve(self, tool_key: str) -> ToolDescriptor: ...


class PolicyAuthority(Protocol):
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

    def capture_after(
        self, prepared: PreparedDispatch
    ) -> SourceTreeSnapshot: ...

    def discard(self, prepared: PreparedDispatch) -> None: ...


class BudgetAuthority(Protocol):
    def reserve(
        self,
        ticket: object,
        call: CheckedInvocation,
        descriptor: ToolDescriptor,
    ) -> object: ...

    def release_before_dispatch(self, reservation: object) -> None: ...


class Dispatcher(Protocol):
    def dispatch(self, capability: object, prepared: PreparedDispatch) -> object: ...


class ExecutionCommitter(Protocol):
    def commit(
        self,
        dispatch_capability: object,
        call: CheckedInvocation,
        prepared: PreparedDispatch,
        receipt: object,
        source_after: SourceTreeSnapshot,
        reservation: object,
    ) -> object: ...

    def commit_source_violation(
        self,
        dispatch_capability: object,
        call: CheckedInvocation,
        prepared: PreparedDispatch,
        receipt: object,
        source_after: SourceTreeSnapshot,
        reservation: object,
        changes: SourceDiff,
    ) -> object: ...

    def commit_source_unavailable(
        self,
        dispatch_capability: object,
        call: CheckedInvocation,
        prepared: PreparedDispatch,
        receipt: object,
        reservation: object,
        error: Exception,
    ) -> object: ...

    def commit_dispatch_failure(
        self,
        dispatch_capability: object,
        call: CheckedInvocation,
        prepared: PreparedDispatch,
        reservation: object,
        error: Exception,
    ) -> object: ...


_NOT_REPLAY = object()


def _replay_value(replay: ReplayState) -> object:
    if replay.kind == "NEW" and replay.result is None:
        return _NOT_REPLAY
    if replay.kind == "PENDING" and replay.result is None:
        raise GatewayError("INVOCATION_PENDING")
    if replay.kind == "COMPLETED":
        return replay.result
    raise GatewayError("JOURNAL_STATE_INVALID")


class AgentKernelGateway:
    def __init__(
        self,
        *,
        codec: InvocationCodec,
        journal: DispatchJournal,
        authority: CurrentAuthority,
        catalog: ToolCatalog,
        policy: PolicyAuthority,
        preparer: InvocationPreparer,
        budget: BudgetAuthority,
        dispatcher: Dispatcher,
        committer: ExecutionCommitter,
    ) -> None:
        self.codec = codec
        self.journal = journal
        self.authority = authority
        self.catalog = catalog
        self.policy = policy
        self.preparer = preparer
        self.budget = budget
        self.dispatcher = dispatcher
        self.committer = committer

    def invoke(self, raw: bytes) -> object:
        call = self.codec.validate(raw)
        replayed = _replay_value(
            self.journal.probe(call.idempotency_key, call.invocation_digest)
        )
        if replayed is not _NOT_REPLAY:
            return replayed

        authority_ticket = self.authority.assert_actor_current(call)
        self.authority.assert_lease_current(authority_ticket, call)
        self.authority.assert_runnable(authority_ticket, call)
        descriptor = self.catalog.resolve(call.tool_key)
        if descriptor.tool_key != call.tool_key:
            raise GatewayError("TOOL_DESCRIPTOR_IDENTITY_MISMATCH")
        self.policy.assert_capability(authority_ticket, call, descriptor)
        prepared = self.preparer.prepare(authority_ticket, call, descriptor)
        if (
            prepared.tool_key != descriptor.tool_key
            or prepared.tool_version != descriptor.tool_version
            or not isinstance(prepared.source_before, SourceTreeSnapshot)
        ):
            self.preparer.discard(prepared)
            raise GatewayError("PREPARED_DISPATCH_INVALID")
        try:
            self.policy.assert_execution_controls(
                authority_ticket, call, descriptor, prepared
            )
            reservation = self.budget.reserve(authority_ticket, call, descriptor)
        except BaseException:
            self.preparer.discard(prepared)
            raise

        try:
            decision = self.journal.begin(
                authority_ticket, call, descriptor, prepared, reservation
            )
        except BaseException:
            self._abandon_pre_dispatch(prepared, reservation)
            raise
        if decision.kind != "WINNER":
            self._abandon_pre_dispatch(prepared, reservation)
            replayed = _replay_value(
                ReplayState(decision.kind, decision.result)
            )
            if replayed is not _NOT_REPLAY:
                return replayed
            raise GatewayError("JOURNAL_STATE_INVALID")
        if decision.dispatch_capability is None:
            self._abandon_pre_dispatch(prepared, reservation)
            raise GatewayError("DISPATCH_CAPABILITY_MISSING")

        try:
            receipt = self.dispatcher.dispatch(decision.dispatch_capability, prepared)
        except BaseException as exc:
            self.preparer.discard(prepared)
            if not isinstance(exc, Exception):
                raise
            return self.committer.commit_dispatch_failure(
                decision.dispatch_capability,
                call,
                prepared,
                reservation,
                exc,
            )
        try:
            source_after = self.preparer.capture_after(prepared)
            changes = diff_source_trees(prepared.source_before, source_after)
        except Exception as exc:
            return self.committer.commit_source_unavailable(
                decision.dispatch_capability,
                call,
                prepared,
                receipt,
                reservation,
                exc,
            )
        if not changes.is_empty:
            return self.committer.commit_source_violation(
                decision.dispatch_capability,
                call,
                prepared,
                receipt,
                source_after,
                reservation,
                changes,
            )
        return self.committer.commit(
            decision.dispatch_capability,
            call,
            prepared,
            receipt,
            source_after,
            reservation,
        )

    def _abandon_pre_dispatch(
        self, prepared: PreparedDispatch, reservation: object
    ) -> None:
        try:
            self.preparer.discard(prepared)
        finally:
            self.budget.release_before_dispatch(reservation)


__all__ = [
    "AgentKernelGateway",
    "CheckedInvocation",
    "DispatchDecision",
    "GatewayError",
    "PreparedDispatch",
    "ReplayState",
    "ToolDescriptor",
]
