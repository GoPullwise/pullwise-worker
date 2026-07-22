"""Concrete current-only durable dispatch journal."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import secrets
import sqlite3
from typing import Callable

from .agent_kernel_current_authority import (
    AuthorityProjection,
    CurrentAuthorityProjection,
    CurrentAuthorityProjectionError,
)
from .agent_kernel_current_admission import assert_prepared_dispatch
from .agent_kernel_current_budget import (
    CurrentBudgetError,
    ReservationPlan,
    plan_reservation,
    reserve_budget,
)
from .agent_kernel_current_database import CurrentAgentKernelDatabase
from .agent_kernel_current_journal_execution import (
    abandon_current_intent,
    commit_current_execution,
    leave_dispatch_pending,
    publish_current_payload,
)
from .agent_kernel_current_journal_types import (
    CurrentJournalError,
    DispatchCapability,
    capability_sha256,
    translate_contract_error,
)
from .agent_kernel_current_objects import CurrentObjectStore, PublishedCurrentPayload
from .agent_kernel_current_package import (
    ServerAuthorityEnvelope,
    canonical_validated_current_bytes,
    seal_current_document,
)
from .agent_kernel_current_replay import probe_current_replay, replay_state
from .agent_kernel_current_recovery import recover_abandon_current_intent
from .agent_kernel_gateway import (
    CheckedInvocation,
    DispatchDecision,
    PreparedDispatch,
    ReplayState,
    ToolDescriptor,
)
from .agent_kernel_source_state import SourceDiff, SourceTreeSnapshot


class CurrentDispatchJournal:
    def __init__(
        self,
        database: CurrentAgentKernelDatabase,
        *,
        object_store: CurrentObjectStore | None = None,
        clock: Callable[[], str] | None = None,
        fault_hook: Callable[[str], None] | None = None,
    ) -> None:
        if not isinstance(database, CurrentAgentKernelDatabase):
            raise CurrentJournalError("CURRENT_DATABASE_INVALID")
        self.database = database
        self.object_store = object_store or CurrentObjectStore(
            database.root / "content"
        )
        self.clock = clock or _utc_now
        self.fault_hook = fault_hook or (lambda _stage: None)
        self.authority = CurrentAuthorityProjection(database)

    def record_authority(
        self,
        envelope: AuthorityProjection,
        *,
        expected_previous_digest: str | None = None,
    ) -> AuthorityProjection:
        try:
            return self.authority.record_projection(
                envelope,
                expected_previous_digest=expected_previous_digest,
            )
        except (CurrentAuthorityProjectionError, CurrentBudgetError) as exc:
            raise CurrentJournalError(exc.code) from exc

    def resolve_authority(
        self, task_id: str, idempotency_key: str
    ) -> ServerAuthorityEnvelope | None:
        try:
            return self.authority.resolve_intent(task_id, idempotency_key)
        except CurrentAuthorityProjectionError as exc:
            raise CurrentJournalError(exc.code) from exc

    def assert_actor_current(self, call: CheckedInvocation) -> ServerAuthorityEnvelope:
        try:
            return self.authority.current_for_call(call)
        except CurrentAuthorityProjectionError as exc:
            raise CurrentJournalError(exc.code) from exc

    def assert_lease_current(
        self, ticket: object, call: CheckedInvocation
    ) -> None:
        try:
            persisted = self.authority.current_for_call(call)
            self.authority.assert_ticket(persisted, ticket, call)
        except CurrentAuthorityProjectionError as exc:
            raise CurrentJournalError(exc.code) from exc

    def assert_runnable(self, ticket: object, call: CheckedInvocation) -> None:
        try:
            persisted = self.authority.current_for_call(call)
            self.authority.assert_ticket(persisted, ticket, call)
            self.authority.assert_runnable(persisted)
        except CurrentAuthorityProjectionError as exc:
            raise CurrentJournalError(exc.code) from exc

    def plan_reservation(
        self,
        ticket: object,
        call: CheckedInvocation,
        descriptor: ToolDescriptor,
    ) -> ReservationPlan:
        try:
            persisted = self.authority.current_for_call(call)
            self.authority.assert_ticket(persisted, ticket, call)
            self.authority.assert_runnable(persisted)
            self.authority.assert_descriptor(persisted, descriptor)
            with self.database.connect() as connection:
                return plan_reservation(
                    connection,
                    envelope=persisted,
                    call=call,
                    started_at=self.clock(),
                )
        except (CurrentAuthorityProjectionError, CurrentBudgetError) as exc:
            raise CurrentJournalError(exc.code) from exc

    def probe(
        self,
        task_id: str,
        idempotency_key: str,
        invocation_digest: str,
    ) -> ReplayState:
        return probe_current_replay(
            self.database,
            task_id=task_id,
            idempotency_key=idempotency_key,
            invocation_digest=invocation_digest,
        )

    def begin(
        self,
        authority_ticket: object,
        call: CheckedInvocation,
        descriptor: ToolDescriptor,
        prepared: PreparedDispatch,
        reservation_plan: ReservationPlan,
    ) -> DispatchDecision:
        try:
            with self.database.transaction() as connection:
                existing = connection.execute(
                    "SELECT intent_id, invocation_digest, state FROM dispatch_intents "
                    "WHERE task_id = ? AND idempotency_key = ?",
                    (call.task_id, call.idempotency_key),
                ).fetchone()
                if existing is not None:
                    if existing["invocation_digest"] != call.invocation_digest:
                        raise CurrentJournalError("IDEMPOTENCY_CONFLICT")
                    replay = replay_state(
                        connection, existing["intent_id"], existing["state"]
                    )
                    if replay.kind == "COMPLETED":
                        return DispatchDecision.completed(replay.result)
                    return DispatchDecision.pending()

                persisted = self.authority.load_head(connection, call.task_id)
                self.authority.assert_ticket(persisted, authority_ticket, call)
                self.authority.assert_runnable(persisted)
                self.authority.assert_descriptor(persisted, descriptor)
                relative_path = assert_prepared_dispatch(call, descriptor, prepared)
                reserve_budget(
                    connection,
                    envelope=persisted,
                    call=call,
                    plan=reservation_plan,
                )
                secret = secrets.token_bytes(32)
                capability_sha256 = hashlib.sha256(secret).hexdigest()
                intent_id = _identifier(
                    "intent", call.task_id, call.idempotency_key,
                    call.invocation_digest,
                )
                intent = seal_current_document(
                    "tool-dispatch-intent/v1",
                    {
                        "schema_id": "tool-dispatch-intent/v1",
                        "package": persisted.package.as_document(),
                        "intent_id": intent_id,
                        "authority_digest": persisted.digest,
                        "grant_digest": persisted.grant_digest,
                        "invocation_digest": call.invocation_digest,
                        "task_id": call.task_id,
                        "idempotency_key": call.idempotency_key,
                        "tool_key": call.tool_key,
                        "tool_input": {"relative_path": relative_path},
                        "reservation_id": reservation_plan.reservation_id,
                        "capability_digest": capability_sha256,
                        "state": "INTENT",
                        "created_at": reservation_plan.started_at,
                    },
                )
                intent_bytes = canonical_validated_current_bytes(
                    "tool-dispatch-intent/v1", intent
                )
                connection.execute(
                    "INSERT INTO dispatch_intents VALUES "
                    "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        call.idempotency_key,
                        call.invocation_digest,
                        intent_id,
                        call.task_id,
                        persisted.digest,
                        persisted.grant_digest,
                        call.tool_key,
                        relative_path,
                        reservation_plan.reservation_id,
                        reservation_plan.reserved_ms,
                        reservation_plan.canonical_bytes,
                        reservation_plan.digest,
                        intent_bytes,
                        intent["intent_digest"],
                        capability_sha256,
                        "INTENT",
                        reservation_plan.started_at,
                    ),
                )
                self.fault_hook("before_begin_commit")
                return DispatchDecision.winner(DispatchCapability(secret))
        except CurrentJournalError:
            raise
        except (
            CurrentAuthorityProjectionError,
            CurrentBudgetError,
            sqlite3.Error,
        ) as exc:
            raise CurrentJournalError(getattr(exc, "code", "CURRENT_JOURNAL_WRITE_FAILED")) from exc
        except Exception as exc:
            translate_contract_error(exc)
            raise

    def consume_capability(self, capability: object) -> None:
        digest = self._capability_sha256(capability)
        try:
            with self.database.transaction() as connection:
                intent = self._intent_by_capability(connection, digest)
                if intent["state"] != "INTENT":
                    if intent["state"] in {"DISPATCHED", "SETTLED"}:
                        raise CurrentJournalError("CAPABILITY_ALREADY_CONSUMED")
                    raise CurrentJournalError("DISPATCH_ABANDONED")
                current = self.authority.load_head(connection, intent["task_id"])
                if current.digest != intent["authority_digest"]:
                    raise CurrentJournalError("AUTHORITY_FENCED")
                self.authority.assert_runnable(current)
                cursor = connection.execute(
                    "UPDATE dispatch_intents SET state = 'DISPATCHED' "
                    "WHERE intent_id = ? AND state = 'INTENT'",
                    (intent["intent_id"],),
                )
                if cursor.rowcount != 1:
                    raise CurrentJournalError("CAPABILITY_ALREADY_CONSUMED")
        except CurrentAuthorityProjectionError as exc:
            raise CurrentJournalError(exc.code) from exc

    def publish_payload(
        self, capability: object, raw: object
    ) -> PublishedCurrentPayload:
        return publish_current_payload(self, capability, raw)

    def commit(
        self,
        capability: object,
        call: CheckedInvocation,
        prepared: PreparedDispatch,
        receipt: object,
        source_after: SourceTreeSnapshot,
    ) -> bytes:
        return commit_current_execution(
            self,
            capability,
            call,
            prepared,
            receipt,
            source_after,
            changes=None,
        )

    def commit_source_violation(
        self,
        capability: object,
        call: CheckedInvocation,
        prepared: PreparedDispatch,
        receipt: object,
        source_after: SourceTreeSnapshot,
        changes: SourceDiff,
    ) -> bytes:
        return commit_current_execution(
            self,
            capability,
            call,
            prepared,
            receipt,
            source_after,
            changes=changes,
        )

    def commit_source_unavailable(
        self,
        capability: object,
        call: CheckedInvocation,
        prepared: PreparedDispatch,
        receipt: object,
        error: Exception,
    ) -> bytes:
        del call, prepared, receipt, error
        return leave_dispatch_pending(
            capability, "SOURCE_STATE_UNAVAILABLE_PENDING"
        )

    def commit_dispatch_failure(
        self,
        capability: object,
        call: CheckedInvocation,
        prepared: PreparedDispatch,
        error: Exception,
    ) -> bytes:
        del call, prepared, error
        return leave_dispatch_pending(capability, "DISPATCH_FAILURE_PENDING")

    def abandon_intent(self, capability: object, reason: str) -> bytes:
        return abandon_current_intent(self, capability, reason)

    def recover_abandon(
        self,
        task_id: str,
        idempotency_key: str,
        invocation_digest: str,
        fenced_head: object,
        reason: str = "recovery_timeout",
    ) -> bytes:
        return recover_abandon_current_intent(
            self,
            task_id=task_id,
            idempotency_key=idempotency_key,
            invocation_digest=invocation_digest,
            fenced_head=fenced_head,
            reason=reason,
        )

    @staticmethod
    def _capability_sha256(capability: object) -> str:
        return capability_sha256(capability)

    @staticmethod
    def _identifier(prefix: str, *parts: str) -> str:
        return _identifier(prefix, *parts)

    @staticmethod
    def _intent_by_capability(
        connection: sqlite3.Connection, capability_sha256: str
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM dispatch_intents WHERE capability_sha256 = ?",
            (capability_sha256,),
        ).fetchone()
        if row is None:
            raise CurrentJournalError("DISPATCH_CAPABILITY_INVALID")
        return row

def _identifier(prefix: str, *parts: str) -> str:
    payload = b"pullwise:current-journal:v1"
    for part in parts:
        encoded = part.encode("utf-8", errors="strict")
        payload += len(encoded).to_bytes(8, "big") + encoded
    return f"{prefix}_{hashlib.sha256(payload).hexdigest()[:32]}"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(
        timespec="microseconds"
    ).replace("+00:00", "Z")


__all__ = [
    "CurrentDispatchJournal",
    "CurrentJournalError",
    "DispatchCapability",
    "ReservationPlan",
]
