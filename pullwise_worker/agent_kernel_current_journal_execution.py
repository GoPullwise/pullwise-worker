"""CAS publication and execution completion facade for the current journal."""

from __future__ import annotations

from .agent_kernel_current_authority import CurrentAuthorityProjectionError
from .agent_kernel_current_budget import CurrentBudgetError
from .agent_kernel_current_journal_types import (
    CurrentJournalError,
    capability_sha256,
    translate_contract_error,
)
from .agent_kernel_current_objects import CurrentObjectError, PublishedCurrentPayload
from .agent_kernel_current_records import (
    CurrentRecordError,
    abandon_with_capability,
    commit_documents,
)
from .agent_kernel_current_settlement import (
    CurrentSettlementError,
    prepare_settlement,
    publish_r0_payload,
)
from .agent_kernel_source_state import SourceDiff, SourceTreeSnapshot


def publish_current_payload(
    journal: object, capability: object, raw: object
) -> PublishedCurrentPayload:
    digest = capability_sha256(capability)
    with journal.database.connect() as connection:
        intent = journal._intent_by_capability(connection, digest)
        if intent["state"] != "DISPATCHED":
            raise CurrentJournalError("CAPABILITY_NOT_CONSUMED")
        head = connection.execute(
            "SELECT heads.projection_digest, history.state "
            "FROM authority_heads AS heads "
            "JOIN authority_history AS history "
            "ON history.projection_digest = heads.projection_digest "
            "WHERE heads.task_id = ?",
            (intent["task_id"],),
        ).fetchone()
        if head is None or tuple(head) != (intent["authority_digest"], "ACTIVE"):
            raise CurrentJournalError("AUTHORITY_FENCED")
    try:
        return publish_r0_payload(
            journal.object_store,
            invocation_digest=intent["invocation_digest"],
            relative_path=intent["relative_path"],
            raw=raw,
        )
    except (CurrentSettlementError, CurrentObjectError) as exc:
        raise CurrentJournalError(exc.code) from exc
    except Exception as exc:
        translate_contract_error(exc)
        raise


def commit_current_execution(
    journal: object,
    capability: object,
    call: object,
    prepared: object,
    outcome: object,
    source_after: SourceTreeSnapshot,
    *,
    changes: SourceDiff | None,
) -> bytes:
    capability_digest = capability_sha256(capability)
    try:
        historical = journal.authority.resolve_intent(
            call.task_id, call.idempotency_key
        )
        if historical is None:
            raise CurrentJournalError("INVOCATION_NOT_FOUND")
        journal.authority.assert_call(historical, call)
        with journal.database.connect() as connection:
            intent = journal._intent_by_capability(connection, capability_digest)
            if (
                intent["task_id"] != call.task_id
                or intent["idempotency_key"] != call.idempotency_key
            ):
                raise CurrentJournalError("SETTLEMENT_INVOCATION_CONFLICT")
            observation_id = journal._identifier("obs", intent["intent_id"])
            tool_invocation_id = journal._identifier(
                "toolinv", intent["intent_id"]
            )
        documents = prepare_settlement(
            object_store=journal.object_store,
            call=call,
            prepared=prepared,
            outcome=outcome,
            source_after=source_after,
            observation_id=observation_id,
            tool_invocation_id=tool_invocation_id,
            changes=changes,
        )
        return commit_documents(
            journal.database,
            capability_sha256=capability_digest,
            call=call,
            documents=documents,
            fault_hook=journal.fault_hook,
        )
    except CurrentJournalError:
        raise
    except (
        CurrentAuthorityProjectionError,
        CurrentBudgetError,
        CurrentRecordError,
        CurrentSettlementError,
        CurrentObjectError,
    ) as exc:
        raise CurrentJournalError(exc.code) from exc
    except Exception as exc:
        translate_contract_error(exc)
        raise


def abandon_current_intent(
    journal: object, capability: object, reason: str
) -> bytes:
    try:
        return abandon_with_capability(
            journal.database,
            capability_sha256=capability_sha256(capability),
            reason=reason,
            abandoned_at=journal.clock(),
            fault_hook=journal.fault_hook,
        )
    except (CurrentRecordError, CurrentBudgetError) as exc:
        raise CurrentJournalError(exc.code) from exc
    except Exception as exc:
        translate_contract_error(exc)
        raise


def leave_dispatch_pending(capability: object, code: str) -> bytes:
    capability_sha256(capability)
    raise CurrentJournalError(code)


__all__ = [
    "abandon_current_intent",
    "commit_current_execution",
    "leave_dispatch_pending",
    "publish_current_payload",
]
