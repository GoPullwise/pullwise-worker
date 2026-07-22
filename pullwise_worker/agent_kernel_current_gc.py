"""Idle, age-bounded reclamation for current journal CAS orphans."""

from __future__ import annotations

from .agent_kernel_current_database import CurrentAgentKernelDatabase
from .agent_kernel_current_journal_types import CurrentJournalError
from .agent_kernel_current_objects import CurrentObjectError, CurrentObjectStore


def collect_current_orphans(
    database: CurrentAgentKernelDatabase,
    object_store: CurrentObjectStore,
    *,
    min_age_seconds: int,
    now: float | None = None,
) -> tuple[str, ...]:
    if not isinstance(database, CurrentAgentKernelDatabase) or not isinstance(
        object_store, CurrentObjectStore
    ):
        raise CurrentJournalError("CURRENT_OBJECT_COLLECTION_INVALID")
    try:
        with database.transaction() as connection:
            active = connection.execute(
                "SELECT count(*) FROM dispatch_intents AS intents "
                "JOIN authority_heads AS heads ON heads.task_id = intents.task_id "
                "JOIN authority_history AS history "
                "ON history.projection_digest = heads.projection_digest "
                "WHERE intents.state IN ('INTENT', 'DISPATCHED') "
                "AND history.state = 'ACTIVE'"
            ).fetchone()[0]
            if active:
                raise CurrentJournalError("CURRENT_OBJECT_COLLECTION_NOT_IDLE")
            reachable = {
                row[0]
                for row in connection.execute(
                    "SELECT sha256 FROM content_objects"
                )
            }
            return object_store.collect_orphans(
                reachable,
                min_age_seconds=min_age_seconds,
                now=now,
            )
    except CurrentJournalError:
        raise
    except CurrentObjectError as exc:
        raise CurrentJournalError(exc.code) from exc


__all__ = ["collect_current_orphans"]
