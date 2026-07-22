"""Authority-authenticated restart recovery for undispatched current intents."""

from __future__ import annotations

from .agent_kernel_current_authority import CurrentAuthorityProjectionError
from .agent_kernel_current_budget import CurrentBudgetError
from .agent_kernel_current_journal_types import (
    CurrentJournalError,
    translate_contract_error,
)
from .agent_kernel_current_records import (
    CurrentRecordError,
    abandon_recovery_intent,
)


def recover_abandon_current_intent(
    journal: object,
    *,
    task_id: str,
    idempotency_key: str,
    invocation_digest: str,
    fenced_head: object,
    reason: str,
) -> bytes:
    if reason != "recovery_timeout":
        raise CurrentJournalError("RECOVERY_REASON_INVALID")
    ambiguous = False
    try:
        with journal.database.transaction() as connection:
            intent = connection.execute(
                "SELECT * FROM dispatch_intents "
                "WHERE task_id = ? AND idempotency_key = ?",
                (task_id, idempotency_key),
            ).fetchone()
            if intent is None:
                raise CurrentJournalError("INVOCATION_NOT_FOUND")
            if intent["invocation_digest"] != invocation_digest:
                raise CurrentJournalError("IDEMPOTENCY_CONFLICT")
            if (
                getattr(fenced_head, "task_id", None) != task_id
                or getattr(fenced_head, "superseded_authority_digest", None)
                != intent["authority_digest"]
            ):
                raise CurrentJournalError("FENCED_AUTHORITY_INVOCATION_CONFLICT")
            journal.authority.apply_fenced(
                connection,
                fenced_head,
                expected_previous_digest=intent["authority_digest"],
            )
            if intent["state"] == "DISPATCHED":
                ambiguous = True
                replay = b""
            else:
                replay = abandon_recovery_intent(
                    connection,
                    task_id=task_id,
                    idempotency_key=idempotency_key,
                    invocation_digest=invocation_digest,
                    reason=reason,
                    abandoned_at=journal.clock(),
                    fault_hook=journal.fault_hook,
                )
        if ambiguous:
            raise CurrentJournalError("DISPATCH_AMBIGUOUS")
        return replay
    except CurrentJournalError:
        raise
    except (
        CurrentAuthorityProjectionError,
        CurrentBudgetError,
        CurrentRecordError,
    ) as exc:
        raise CurrentJournalError(exc.code) from exc
    except Exception as exc:
        translate_contract_error(exc)
        raise


__all__ = ["recover_abandon_current_intent"]
