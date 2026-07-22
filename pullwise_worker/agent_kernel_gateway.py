"""Fixed-order execution flow for current-only tool invocations."""

from __future__ import annotations

from .agent_kernel_gateway_contracts import (
    BudgetPlanner,
    CheckedInvocation,
    CurrentAuthority,
    DispatchDecision,
    Dispatcher,
    DispatchJournal,
    ExecutionCommitter,
    GatewayError,
    InvocationCodec,
    InvocationPreparer,
    PolicyAuthority,
    PreparedDispatch,
    ReplayState,
    ToolCatalog,
    ToolDescriptor,
)
from .agent_kernel_source_state import SourceTreeSnapshot, diff_source_trees


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
    """Compose fixed-order checks around journal-owned begin and settlement."""

    def __init__(
        self,
        *,
        codec: InvocationCodec,
        journal: DispatchJournal,
        authority: CurrentAuthority,
        catalog: ToolCatalog,
        policy: PolicyAuthority,
        preparer: InvocationPreparer,
        budget: BudgetPlanner,
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
            self.journal.probe(
                call.task_id,
                call.idempotency_key,
                call.invocation_digest,
            )
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
                authority_ticket,
                call,
                descriptor,
                prepared,
            )
            reservation_plan = self.budget.plan_reservation(
                authority_ticket,
                call,
                descriptor,
            )
        except BaseException:
            self.preparer.discard(prepared)
            raise

        try:
            decision = self.journal.begin(
                authority_ticket,
                call,
                descriptor,
                prepared,
                reservation_plan,
            )
        except BaseException:
            self.preparer.discard(prepared)
            raise
        if decision.kind != "WINNER":
            self.preparer.discard(prepared)
            replayed = _replay_value(ReplayState(decision.kind, decision.result))
            if replayed is not _NOT_REPLAY:
                return replayed
            raise GatewayError("JOURNAL_STATE_INVALID")
        if decision.dispatch_capability is None:
            self.preparer.discard(prepared)
            raise GatewayError("DISPATCH_CAPABILITY_MISSING")

        try:
            receipt = self.dispatcher.dispatch(
                decision.dispatch_capability,
                prepared,
            )
        except BaseException as exc:
            self.preparer.discard(prepared)
            if not isinstance(exc, Exception):
                raise
            return self.committer.commit_dispatch_failure(
                decision.dispatch_capability,
                call,
                prepared,
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
                exc,
            )
        if not changes.is_empty:
            return self.committer.commit_source_violation(
                decision.dispatch_capability,
                call,
                prepared,
                receipt,
                source_after,
                changes,
            )
        return self.committer.commit(
            decision.dispatch_capability,
            call,
            prepared,
            receipt,
            source_after,
        )


__all__ = [
    "AgentKernelGateway",
    "CheckedInvocation",
    "DispatchDecision",
    "GatewayError",
    "PreparedDispatch",
    "ReplayState",
    "ToolDescriptor",
]
