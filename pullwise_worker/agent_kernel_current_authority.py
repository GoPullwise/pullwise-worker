"""Immutable Server authority history and current-head checks."""

from __future__ import annotations

from .agent_kernel_current_budget import initialize_budget
from .agent_kernel_current_database import CurrentAgentKernelDatabase
from .agent_kernel_current_package import (
    CURRENT_TOOL_CATALOG,
    ServerAuthorityEnvelope,
)


class CurrentAuthorityProjectionError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class CurrentAuthorityProjection:
    def __init__(self, database: CurrentAgentKernelDatabase) -> None:
        self.database = database

    def record(
        self,
        envelope: ServerAuthorityEnvelope,
        *,
        expected_previous_digest: str | None = None,
    ) -> ServerAuthorityEnvelope:
        parsed = self._validated(envelope)
        with self.database.transaction() as connection:
            old = self._head(connection, parsed.task_id, required=False)
            if old is not None and old.digest == parsed.digest:
                if (
                    old.canonical_bytes != parsed.canonical_bytes
                    or old.grant.canonical_bytes != parsed.grant.canonical_bytes
                ):
                    self._fail("AUTHORITY_REPLAY_CONFLICT")
                return old
            if old is not None:
                if expected_previous_digest != old.digest:
                    self._fail("AUTHORITY_SUCCESSOR_CONFLICT")
                self._assert_successor(old, parsed)
            elif expected_previous_digest is not None:
                self._fail("AUTHORITY_SUCCESSOR_CONFLICT")
            self._insert_history(connection, parsed)
            if old is None:
                connection.execute(
                    "INSERT INTO authority_heads(task_id, authority_digest) VALUES (?, ?)",
                    (parsed.task_id, parsed.digest),
                )
            else:
                connection.execute(
                    "UPDATE authority_heads SET authority_digest = ? WHERE task_id = ?",
                    (parsed.digest, parsed.task_id),
                )
            initialize_budget(connection, parsed)
        return parsed

    def resolve_intent(
        self, task_id: str, idempotency_key: str
    ) -> ServerAuthorityEnvelope | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT h.authority_bytes, h.grant_bytes FROM dispatch_intents i "
                "JOIN authority_history h ON h.authority_digest = i.authority_digest "
                "WHERE i.task_id = ? AND i.idempotency_key = ?",
                (task_id, idempotency_key),
            ).fetchone()
        return None if row is None else self._parse(bytes(row[0]), bytes(row[1]))

    def current_for_call(self, call: object) -> ServerAuthorityEnvelope:
        with self.database.connect() as connection:
            envelope = self._head(connection, getattr(call, "task_id", ""))
        self.assert_call(envelope, call)
        return envelope

    def load_head(self, connection: object, task_id: str) -> ServerAuthorityEnvelope:
        return self._head(connection, task_id)

    def assert_call(self, envelope: ServerAuthorityEnvelope, call: object) -> None:
        facts = (
            (envelope.digest, getattr(call, "authority_digest", None)),
            (envelope.package.content_sha256, getattr(call, "package_content_sha256", None)),
            (envelope.package.root_sha256, getattr(call, "package_root_sha256", None)),
            (envelope.grant_digest, getattr(call, "grant_digest", None)),
            (envelope.task_id, getattr(call, "task_id", None)),
            (envelope.attempt_id, getattr(call, "attempt_id", None)),
            (envelope.owner_id, getattr(call, "owner_id", None)),
            (envelope.session_id, getattr(call, "session_id", None)),
            (envelope.lease_id, getattr(call, "lease_id", None)),
            (envelope.task_version, getattr(call, "task_version", None)),
            (envelope.deletion_version, getattr(call, "deletion_version", None)),
            (envelope.owner_epoch, getattr(call, "owner_epoch", None)),
            (envelope.native_epoch, getattr(call, "native_epoch", None)),
            (envelope.transport_epoch, getattr(call, "transport_epoch", None)),
        )
        if any(expected != actual for expected, actual in facts):
            self._fail("AUTHORITY_FENCED")

    def assert_ticket(
        self,
        persisted: ServerAuthorityEnvelope,
        ticket: object,
        call: object,
    ) -> None:
        if (
            not isinstance(ticket, ServerAuthorityEnvelope)
            or ticket.digest != persisted.digest
            or ticket.canonical_bytes != persisted.canonical_bytes
            or ticket.grant.canonical_bytes != persisted.grant.canonical_bytes
        ):
            self._fail("AUTHORITY_FENCED")
        self.assert_call(persisted, call)

    def assert_descriptor(self, envelope: ServerAuthorityEnvelope, descriptor: object) -> None:
        try:
            expected = CURRENT_TOOL_CATALOG.resolve(getattr(descriptor, "tool_key", ""))
        except Exception as exc:
            raise CurrentAuthorityProjectionError("DISPATCH_NOT_AUTHORIZED") from exc
        if (
            descriptor != expected
            or getattr(descriptor, "tool_key", None) not in envelope.grant.tool_keys
            or getattr(descriptor, "capability", None) not in envelope.grant.capability_ids
        ):
            self._fail("DISPATCH_NOT_AUTHORIZED")

    @staticmethod
    def assert_runnable(envelope: ServerAuthorityEnvelope) -> None:
        if envelope.lifecycle != "ACTIVE" or envelope.desired_state != "RUN":
            raise CurrentAuthorityProjectionError("AUTHORITY_NOT_RUNNABLE")

    def _validated(self, envelope: object) -> ServerAuthorityEnvelope:
        if not isinstance(envelope, ServerAuthorityEnvelope):
            self._fail("AUTHORITY_PROJECTION_INVALID")
        parsed = ServerAuthorityEnvelope.from_canonical_bytes(envelope.canonical_bytes)
        if (
            parsed.package.as_tuple() != self.database.package_tuple
            or parsed.digest != envelope.digest
            or parsed.canonical_bytes != envelope.canonical_bytes
            or parsed.grant.canonical_bytes != envelope.grant.canonical_bytes
        ):
            self._fail("AUTHORITY_PROJECTION_INVALID")
        return parsed

    def _head(
        self, connection: object, task_id: str, *, required: bool = True
    ) -> ServerAuthorityEnvelope | None:
        row = connection.execute(
            "SELECT h.authority_bytes, h.grant_bytes FROM authority_heads p "
            "JOIN authority_history h ON h.authority_digest = p.authority_digest "
            "WHERE p.task_id = ?",
            (task_id,),
        ).fetchone()
        if row is None:
            if required:
                self._fail("AUTHORITY_NOT_FOUND")
            return None
        return self._parse(bytes(row[0]), bytes(row[1]))

    def _parse(self, authority_bytes: bytes, grant_bytes: bytes) -> ServerAuthorityEnvelope:
        try:
            envelope = ServerAuthorityEnvelope.from_canonical_bytes(authority_bytes)
        except Exception as exc:
            raise CurrentAuthorityProjectionError("AUTHORITY_HISTORY_CORRUPT") from exc
        if envelope.grant.canonical_bytes != grant_bytes:
            self._fail("AUTHORITY_HISTORY_CORRUPT")
        return envelope

    @staticmethod
    def _assert_successor(
        old: ServerAuthorityEnvelope, new: ServerAuthorityEnvelope
    ) -> None:
        monotonic = (
            new.package.as_tuple() == old.package.as_tuple()
            and new.task_id == old.task_id
            and new.owner_id == old.owner_id
            and new.task_version > old.task_version
            and new.deletion_version >= old.deletion_version
            and new.attempt_id != old.attempt_id
            and new.session_id != old.session_id
            and new.lease_id != old.lease_id
            and new.grant.grant_id != old.grant.grant_id
            and new.owner_epoch > old.owner_epoch
            and new.native_epoch > old.native_epoch
            and new.transport_epoch > old.transport_epoch
        )
        if not monotonic:
            raise CurrentAuthorityProjectionError("AUTHORITY_SUCCESSOR_INVALID")

    @staticmethod
    def _insert_history(connection: object, item: ServerAuthorityEnvelope) -> None:
        connection.execute(
            "INSERT INTO authority_history VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                item.digest, item.task_id, item.canonical_bytes,
                item.grant.canonical_bytes, item.grant_digest,
                item.package.package_identity, item.package.package_version,
                item.package.content_sha256, item.package.root_sha256,
                item.attempt_id, item.session_id, item.owner_id, item.lease_id,
                item.task_version, item.deletion_version, item.owner_epoch,
                item.native_epoch, item.transport_epoch, item.lifecycle,
                item.desired_state, item.grant.elapsed_limit_ms,
                item.grant.tool_call_limit,
            ),
        )

    @staticmethod
    def _fail(code: str) -> None:
        raise CurrentAuthorityProjectionError(code)


__all__ = ["CurrentAuthorityProjection", "CurrentAuthorityProjectionError"]
