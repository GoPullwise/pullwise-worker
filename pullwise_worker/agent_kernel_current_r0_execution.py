"""Journal-aware execution adapter for the current R0 read tool."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
from typing import Callable

from .agent_kernel_current_journal_types import (
    CurrentJournalError,
    capability_sha256,
)
from .agent_kernel_current_objects import PublishedCurrentPayload
from .agent_kernel_current_package import seal_current_document
from .agent_kernel_gateway import CheckedInvocation, PreparedDispatch
from .agent_kernel_r0_read import R0ReadDispatcher, R0ReadReceipt
from .agent_kernel_source_state import SourceDiff, SourceTreeSnapshot


@dataclass(frozen=True)
class CurrentR0DispatchOutcome:
    capability_sha256: str
    tool_key: str
    tool_version: str
    raw: R0ReadReceipt
    payload: PublishedCurrentPayload
    started_at: str
    completed_at: str
    elapsed_ms: int


@dataclass(frozen=True)
class _CurrentR0SettlementOutcome:
    raw: R0ReadReceipt
    payload: PublishedCurrentPayload
    receipt: dict[str, object]


class CurrentR0ExecutionAdapter:
    """Consume the journal capability before dispatch and settle its real output."""

    def __init__(
        self,
        journal: object,
        *,
        dispatcher: object | None = None,
        clock: Callable[[], str] | None = None,
    ) -> None:
        required = (
            "consume_capability",
            "publish_payload",
            "commit",
            "commit_source_violation",
            "commit_source_unavailable",
            "commit_dispatch_failure",
        )
        if any(not callable(getattr(journal, name, None)) for name in required):
            raise CurrentJournalError("CURRENT_JOURNAL_ADAPTER_INVALID")
        self.journal = journal
        self.dispatcher = dispatcher or R0ReadDispatcher()
        self.clock = clock or _utc_now

    def dispatch(
        self, capability: object, prepared: PreparedDispatch
    ) -> CurrentR0DispatchOutcome:
        digest = capability_sha256(capability)
        self.journal.consume_capability(capability)
        started_at = self.clock()
        raw = self.dispatcher.dispatch(capability, prepared)
        completed_at = self.clock()
        if not isinstance(raw, R0ReadReceipt):
            raise CurrentJournalError("R0_READ_RECEIPT_INVALID")
        elapsed_ms = _elapsed_ms(started_at, completed_at)
        payload = self.journal.publish_payload(capability, raw)
        return CurrentR0DispatchOutcome(
            capability_sha256=digest,
            tool_key=prepared.tool_key,
            tool_version=prepared.tool_version,
            raw=raw,
            payload=payload,
            started_at=started_at,
            completed_at=completed_at,
            elapsed_ms=elapsed_ms,
        )

    def commit(
        self,
        capability: object,
        call: CheckedInvocation,
        prepared: PreparedDispatch,
        outcome: object,
        source_after: SourceTreeSnapshot,
    ) -> bytes:
        return self.journal.commit(
            capability,
            call,
            prepared,
            self._settlement(capability, call, prepared, outcome),
            source_after,
        )

    def commit_source_violation(
        self,
        capability: object,
        call: CheckedInvocation,
        prepared: PreparedDispatch,
        outcome: object,
        source_after: SourceTreeSnapshot,
        changes: SourceDiff,
    ) -> bytes:
        return self.journal.commit_source_violation(
            capability,
            call,
            prepared,
            self._settlement(capability, call, prepared, outcome),
            source_after,
            changes,
        )

    def commit_source_unavailable(
        self,
        capability: object,
        call: CheckedInvocation,
        prepared: PreparedDispatch,
        outcome: object,
        error: Exception,
    ) -> bytes:
        self._assert_outcome(capability, call, prepared, outcome)
        return self.journal.commit_source_unavailable(
            capability, call, prepared, outcome, error
        )

    def commit_dispatch_failure(
        self,
        capability: object,
        call: CheckedInvocation,
        prepared: PreparedDispatch,
        error: Exception,
    ) -> bytes:
        if (
            isinstance(error, CurrentJournalError)
            and error.code == "AUTHORITY_FENCED"
        ):
            raise error
        return self.journal.commit_dispatch_failure(
            capability, call, prepared, error
        )

    def _settlement(
        self,
        capability: object,
        call: CheckedInvocation,
        prepared: PreparedDispatch,
        outcome: object,
    ) -> _CurrentR0SettlementOutcome:
        selected = self._assert_outcome(capability, call, prepared, outcome)
        receipt = seal_current_document(
            "local-tool-receipt/v1",
            {
                "schema_id": "local-tool-receipt/v1",
                "receipt_kind": "local_tool",
                "tool_key": call.tool_key,
                "invocation_digest": call.invocation_digest,
                "status": "succeeded",
                "payload_ref": selected.payload.content_ref,
                "started_at": selected.started_at,
                "completed_at": selected.completed_at,
                "elapsed_ms": selected.elapsed_ms,
            },
        )
        return _CurrentR0SettlementOutcome(
            raw=selected.raw,
            payload=selected.payload,
            receipt=receipt,
        )

    @staticmethod
    def _assert_outcome(
        capability: object,
        call: CheckedInvocation,
        prepared: PreparedDispatch,
        outcome: object,
    ) -> CurrentR0DispatchOutcome:
        if not isinstance(outcome, CurrentR0DispatchOutcome):
            raise CurrentJournalError("CURRENT_R0_OUTCOME_INVALID")
        if (
            outcome.capability_sha256 != capability_sha256(capability)
            or outcome.tool_key != call.tool_key
            or outcome.tool_key != prepared.tool_key
            or outcome.tool_version != prepared.tool_version
        ):
            raise CurrentJournalError("CURRENT_R0_OUTCOME_BINDING_INVALID")
        return outcome


def _elapsed_ms(started_at: str, completed_at: str) -> int:
    try:
        started = _timestamp(started_at)
        completed = _timestamp(completed_at)
    except (TypeError, ValueError) as exc:
        raise CurrentJournalError("LOCAL_RECEIPT_TIMING_INVALID") from exc
    elapsed = int((completed - started).total_seconds() * 1_000)
    if elapsed < 0:
        raise CurrentJournalError("LOCAL_RECEIPT_TIMING_INVALID")
    return elapsed


def _timestamp(value: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError(value)
    return datetime.fromisoformat(value[:-1] + "+00:00")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


__all__ = ["CurrentR0DispatchOutcome", "CurrentR0ExecutionAdapter"]
