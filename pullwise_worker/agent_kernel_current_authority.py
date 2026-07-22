"""Immutable ACTIVE and Server-authenticated FENCED authority projections."""

from __future__ import annotations

import sqlite3

from .agent_kernel_current_budget import initialize_budget
from .agent_kernel_current_database import CurrentAgentKernelDatabase
from .agent_kernel_current_package import (
    AgentClaimAbandonResponse,
    CURRENT_TOOL_CATALOG,
    ServerAuthorityEnvelope,
)


AuthorityProjection = ServerAuthorityEnvelope | AgentClaimAbandonResponse


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
        parsed = self._validated_active(envelope)
        with self.database.transaction() as connection:
            old = self._head_projection(connection, parsed.task_id, required=False)
            if isinstance(old, AgentClaimAbandonResponse):
                self._fail("AUTHORITY_FENCED")
            if old is not None and old.digest == parsed.digest:
                self._assert_exact_active(old, parsed)
                return old
            if old is not None:
                if expected_previous_digest != old.digest:
                    self._fail("AUTHORITY_SUCCESSOR_CONFLICT")
                self._fail("ACTIVE_AUTHORITY_SUCCESSOR_FORBIDDEN")
            if expected_previous_digest is not None:
                self._fail("AUTHORITY_SUCCESSOR_CONFLICT")
            self._insert_active(connection, parsed)
            connection.execute(
                "INSERT INTO authority_heads(task_id, projection_digest) VALUES (?, ?)",
                (parsed.task_id, parsed.digest),
            )
            initialize_budget(connection, parsed)
        return parsed

    def record_projection(
        self,
        projection: AuthorityProjection,
        *,
        expected_previous_digest: str | None = None,
    ) -> AuthorityProjection:
        if isinstance(projection, ServerAuthorityEnvelope):
            return self.record(
                projection,
                expected_previous_digest=expected_previous_digest,
            )
        if isinstance(projection, AgentClaimAbandonResponse):
            if expected_previous_digest is None:
                self._fail("AUTHORITY_SUCCESSOR_CONFLICT")
            return self.record_fenced(
                projection,
                expected_previous_digest=expected_previous_digest,
            )
        self._fail("AUTHORITY_PROJECTION_INVALID")

    def record_fenced(
        self,
        response: AgentClaimAbandonResponse,
        *,
        expected_previous_digest: str,
    ) -> AgentClaimAbandonResponse:
        with self.database.transaction() as connection:
            return self.apply_fenced(
                connection,
                response,
                expected_previous_digest=expected_previous_digest,
            )

    def apply_fenced(
        self,
        connection: sqlite3.Connection,
        response: AgentClaimAbandonResponse,
        *,
        expected_previous_digest: str,
    ) -> AgentClaimAbandonResponse:
        parsed = self._validated_fenced(response)
        old = self._head_projection(connection, parsed.task_id)
        if isinstance(old, AgentClaimAbandonResponse):
            if (
                expected_previous_digest != old.superseded_authority_digest
                or old.digest != parsed.digest
                or old.canonical_bytes != parsed.canonical_bytes
                or old.grant.canonical_bytes != parsed.grant.canonical_bytes
            ):
                self._fail("FENCED_AUTHORITY_REPLAY_CONFLICT")
            return old
        if (
            expected_previous_digest != old.digest
            or parsed.superseded_authority_digest != old.digest
        ):
            self._fail("AUTHORITY_SUCCESSOR_CONFLICT")
        self._assert_fenced_successor(old, parsed)
        self._insert_fenced(connection, parsed)
        cursor = connection.execute(
            "UPDATE authority_heads SET projection_digest = ? "
            "WHERE task_id = ? AND projection_digest = ?",
            (parsed.digest, parsed.task_id, old.digest),
        )
        if cursor.rowcount != 1:
            self._fail("AUTHORITY_SUCCESSOR_CONFLICT")
        return parsed

    def resolve_intent(
        self, task_id: str, idempotency_key: str
    ) -> ServerAuthorityEnvelope | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT h.projection_kind, h.projection_bytes, h.grant_bytes "
                "FROM dispatch_intents i JOIN authority_history h "
                "ON h.projection_digest = i.authority_digest "
                "WHERE i.task_id = ? AND i.idempotency_key = ?",
                (task_id, idempotency_key),
            ).fetchone()
        if row is None:
            return None
        projection = self._parse_projection(row[0], bytes(row[1]), bytes(row[2]))
        if not isinstance(projection, ServerAuthorityEnvelope):
            self._fail("AUTHORITY_HISTORY_CORRUPT")
        return projection

    def current_projection(self, task_id: str) -> AuthorityProjection:
        with self.database.connect() as connection:
            return self._head_projection(connection, task_id)

    def current_for_call(self, call: object) -> ServerAuthorityEnvelope:
        with self.database.connect() as connection:
            envelope = self.load_head(connection, getattr(call, "task_id", ""))
        self.assert_call(envelope, call)
        return envelope

    def load_head(
        self, connection: sqlite3.Connection, task_id: str
    ) -> ServerAuthorityEnvelope:
        projection = self._head_projection(connection, task_id)
        if isinstance(projection, AgentClaimAbandonResponse):
            self._fail("AUTHORITY_FENCED")
        return projection

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

    def _validated_active(self, envelope: object) -> ServerAuthorityEnvelope:
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

    def _validated_fenced(
        self, response: object
    ) -> AgentClaimAbandonResponse:
        if not isinstance(response, AgentClaimAbandonResponse):
            self._fail("FENCED_AUTHORITY_INVALID")
        parsed = AgentClaimAbandonResponse.from_canonical_bytes(
            response.canonical_bytes
        )
        if (
            parsed.package.as_tuple() != self.database.package_tuple
            or parsed.digest != response.digest
            or parsed.canonical_bytes != response.canonical_bytes
            or parsed.grant.canonical_bytes != response.grant.canonical_bytes
        ):
            self._fail("FENCED_AUTHORITY_INVALID")
        return parsed

    def _head_projection(
        self,
        connection: sqlite3.Connection,
        task_id: str,
        *,
        required: bool = True,
    ) -> AuthorityProjection | None:
        row = connection.execute(
            "SELECT h.projection_kind, h.projection_bytes, h.grant_bytes "
            "FROM authority_heads p JOIN authority_history h "
            "ON h.projection_digest = p.projection_digest WHERE p.task_id = ?",
            (task_id,),
        ).fetchone()
        if row is None:
            if required:
                self._fail("AUTHORITY_NOT_FOUND")
            return None
        return self._parse_projection(row[0], bytes(row[1]), bytes(row[2]))

    def _parse_projection(
        self, kind: str, projection_bytes: bytes, grant_bytes: bytes
    ) -> AuthorityProjection:
        try:
            projection = (
                ServerAuthorityEnvelope.from_canonical_bytes(projection_bytes)
                if kind == "ACTIVE"
                else AgentClaimAbandonResponse.from_canonical_bytes(projection_bytes)
                if kind == "FENCED"
                else None
            )
        except Exception as exc:
            raise CurrentAuthorityProjectionError("AUTHORITY_HISTORY_CORRUPT") from exc
        if projection is None or projection.grant.canonical_bytes != grant_bytes:
            self._fail("AUTHORITY_HISTORY_CORRUPT")
        return projection

    @staticmethod
    def _assert_exact_active(
        old: ServerAuthorityEnvelope, new: ServerAuthorityEnvelope
    ) -> None:
        if (
            old.canonical_bytes != new.canonical_bytes
            or old.grant.canonical_bytes != new.grant.canonical_bytes
        ):
            raise CurrentAuthorityProjectionError("AUTHORITY_REPLAY_CONFLICT")

    @staticmethod
    def _assert_fenced_successor(
        old: ServerAuthorityEnvelope, new: AgentClaimAbandonResponse
    ) -> None:
        exact = (
            new.package.as_tuple() == old.package.as_tuple()
            and new.grant.canonical_bytes == old.grant.canonical_bytes
            and new.task_id == old.task_id
            and new.attempt_id == old.attempt_id
            and new.session_id == old.session_id
            and new.owner_id == old.owner_id
            and new.lease_id == old.lease_id
            and new.task_version == old.task_version + 1
            and new.deletion_version == old.deletion_version
            and new.owner_epoch == old.owner_epoch
            and new.native_epoch == old.native_epoch
            and new.transport_epoch == old.transport_epoch
            and new.state == "FENCED"
        )
        if not exact:
            raise CurrentAuthorityProjectionError("FENCED_AUTHORITY_SUCCESSOR_INVALID")

    @staticmethod
    def _insert_active(
        connection: sqlite3.Connection, item: ServerAuthorityEnvelope
    ) -> None:
        connection.execute(
            "INSERT INTO authority_history VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                item.digest, "ACTIVE", item.task_id, item.canonical_bytes,
                item.grant.canonical_bytes, item.grant_digest,
                item.package.package_identity, item.package.package_version,
                item.package.content_sha256, item.package.root_sha256,
                item.attempt_id, item.session_id, item.owner_id,
                item.grant.grant_id, item.lease_id, None, item.task_version,
                item.deletion_version, item.owner_epoch, item.native_epoch,
                item.transport_epoch, "ACTIVE", item.lifecycle,
                item.desired_state, None, None, item.grant.elapsed_limit_ms,
                item.grant.tool_call_limit,
            ),
        )

    @staticmethod
    def _insert_fenced(
        connection: sqlite3.Connection, item: AgentClaimAbandonResponse
    ) -> None:
        connection.execute(
            "INSERT INTO authority_history VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                item.digest, "FENCED", item.task_id, item.canonical_bytes,
                item.grant.canonical_bytes, item.grant.digest,
                item.package.package_identity, item.package.package_version,
                item.package.content_sha256, item.package.root_sha256,
                item.attempt_id, item.session_id, item.owner_id,
                item.grant.grant_id, item.lease_id, item.grant.task_version,
                item.task_version, item.deletion_version, item.owner_epoch,
                item.native_epoch, item.transport_epoch, "FENCED", None, None,
                item.superseded_authority_digest, item.reason,
                item.grant.elapsed_limit_ms, item.grant.tool_call_limit,
            ),
        )

    @staticmethod
    def _fail(code: str) -> None:
        raise CurrentAuthorityProjectionError(code)


__all__ = [
    "AuthorityProjection",
    "CurrentAuthorityProjection",
    "CurrentAuthorityProjectionError",
]
