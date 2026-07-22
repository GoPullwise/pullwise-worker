from __future__ import annotations

import hashlib
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from pullwise_worker.agent_kernel_current_database import CurrentAgentKernelDatabase
from pullwise_worker.agent_kernel_current_objects import CurrentObjectStore
from pullwise_worker.agent_kernel_current_package import (
    AgentClaimAbandonResponse,
    CURRENT_PACKAGE,
    CURRENT_TOOL_CATALOG,
    ServerAuthorityEnvelope,
    ServerDispatchGrant,
    canonical_validated_current_bytes,
    seal_current_document,
)
from pullwise_worker.agent_kernel_dispatch_journal import CurrentDispatchJournal
from pullwise_worker.agent_kernel_gateway import CheckedInvocation, PreparedDispatch
from pullwise_worker.agent_kernel_r0_read import R0ReadReceipt
from pullwise_worker.agent_kernel_source_state import SourceTreeSnapshot


class CurrentJournalTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.scratch = tempfile.TemporaryDirectory(prefix="current-journal-")
        self.root = Path(self.scratch.name) / "current"
        self.database = CurrentAgentKernelDatabase.open(self.root, CURRENT_PACKAGE)
        self.objects = CurrentObjectStore(self.root / "content")
        self.journal = CurrentDispatchJournal(
            self.database,
            object_store=self.objects,
            clock=lambda: "2026-07-22T12:34:56.000Z",
        )
        self.authority = self.make_authority()
        self.journal.record_authority(self.authority)
        self.call = self.make_call(self.authority)
        self.descriptor = CURRENT_TOOL_CATALOG.resolve("internal.read_source")
        self.before = SourceTreeSnapshot("a" * 40, "b" * 64, ())
        self.prepared = PreparedDispatch(
            tool_key=self.descriptor.tool_key,
            tool_version=self.descriptor.tool_version,
            source_before=self.before,
            dispatch_handle=object(),
        )

    def tearDown(self) -> None:
        self.scratch.cleanup()

    @staticmethod
    def make_authority(
        *,
        identity_char: str = "1",
        grant_char: str = "c",
        elapsed_limit_ms: int = 60_000,
        tool_call_limit: int = 2,
    ) -> ServerAuthorityEnvelope:
        grant_document = seal_current_document(
            "agent-worker-grant/v1",
            {
                "schema_id": "agent-worker-grant/v1",
                "package": CURRENT_PACKAGE.as_document(),
                "grant_id": f"grant_{grant_char * 32}",
                "task_id": f"task_{identity_char * 32}",
                "attempt_id": f"attempt_{identity_char * 32}",
                "session_id": f"sess_{identity_char * 32}",
                "owner_id": f"owner_{identity_char * 32}",
                "lease_id": f"lease_{identity_char * 32}",
                "task_version": 1,
                "deletion_version": 0,
                "owner_epoch": 2,
                "native_epoch": 3,
                "transport_epoch": 4,
                "policy_digest": "6" * 64,
                "capability_ids": ["source.read"],
                "tool_keys": ["internal.read_source"],
                "elapsed_limit_ms": elapsed_limit_ms,
                "tool_call_limit": tool_call_limit,
            },
        )
        grant = ServerDispatchGrant.from_document(grant_document)
        envelope_document = seal_current_document(
            "server-authority-envelope/v1",
            {
                "schema_id": "server-authority-envelope/v1",
                "package": CURRENT_PACKAGE.as_document(),
                "task_id": grant.task_id,
                "attempt_id": grant.attempt_id,
                "session_id": grant.session_id,
                "owner_id": grant.owner_id,
                "lease_id": grant.lease_id,
                "task_version": grant.task_version,
                "deletion_version": grant.deletion_version,
                "owner_epoch": grant.owner_epoch,
                "native_epoch": grant.native_epoch,
                "transport_epoch": grant.transport_epoch,
                "lifecycle": "ACTIVE",
                "desired_state": "RUN",
                "grant": grant.as_document(),
            },
        )
        encoded = canonical_validated_current_bytes(
            "server-authority-envelope/v1", envelope_document
        )
        return ServerAuthorityEnvelope.from_canonical_bytes(encoded)

    @staticmethod
    def make_call(
        authority: ServerAuthorityEnvelope,
        *,
        key: str = "idem-current-r0",
        digest_char: str = "7",
    ) -> CheckedInvocation:
        return CheckedInvocation(
            idempotency_key=key,
            invocation_digest=digest_char * 64,
            authority_digest=authority.digest,
            package_content_sha256=authority.package.content_sha256,
            package_root_sha256=authority.package.root_sha256,
            grant_digest=authority.grant_digest,
            task_id=authority.task_id,
            attempt_id=authority.attempt_id,
            owner_id=authority.owner_id,
            session_id=authority.session_id,
            lease_id=authority.lease_id,
            task_version=authority.task_version,
            deletion_version=authority.deletion_version,
            owner_epoch=authority.owner_epoch,
            native_epoch=authority.native_epoch,
            transport_epoch=authority.transport_epoch,
            tool_key="internal.read_source",
            tool_input={"relative_path": "README.md"},
        )

    @staticmethod
    def make_fenced_head(
        authority: ServerAuthorityEnvelope,
        *,
        reason: str = "authority_revoked",
    ) -> AgentClaimAbandonResponse:
        document = seal_current_document(
            "agent-claim-abandon-response/v1",
            {
                "schema_id": "agent-claim-abandon-response/v1",
                "package": authority.package.as_document(),
                "task_id": authority.task_id,
                "attempt_id": authority.attempt_id,
                "session_id": authority.session_id,
                "owner_id": authority.owner_id,
                "grant_id": authority.grant.grant_id,
                "lease_id": authority.lease_id,
                "previous_task_version": authority.task_version,
                "task_version": authority.task_version + 1,
                "deletion_version": authority.deletion_version,
                "owner_epoch": authority.owner_epoch,
                "native_epoch": authority.native_epoch,
                "transport_epoch": authority.transport_epoch,
                "state": "FENCED",
                "grant": authority.grant.as_document(),
                "superseded_authority_digest": authority.digest,
                "reason": reason,
                "abandoned_at": "2026-07-22T12:34:56.000Z",
            },
        )
        encoded = canonical_validated_current_bytes(
            "agent-claim-abandon-response/v1", document
        )
        return AgentClaimAbandonResponse.from_canonical_bytes(encoded)

    def begin(self, call: CheckedInvocation | None = None):
        selected = call or self.call
        plan = self.journal.plan_reservation(
            self.authority,
            selected,
            self.descriptor,
        )
        return self.journal.begin(
            self.authority,
            selected,
            self.descriptor,
            self.prepared,
            plan,
        )

    def outcome(
        self,
        capability: object,
        *,
        call: CheckedInvocation | None = None,
        elapsed_ms: int = 1_000,
        payload_bytes: bytes = b"current payload",
    ) -> SimpleNamespace:
        selected = call or self.call
        raw = R0ReadReceipt(
            payload=payload_bytes,
            sha256=hashlib.sha256(payload_bytes).hexdigest(),
            size_bytes=len(payload_bytes),
        )
        payload = self.journal.publish_payload(capability, raw)
        receipt = seal_current_document(
            "local-tool-receipt/v1",
            {
                "schema_id": "local-tool-receipt/v1",
                "receipt_kind": "local_tool",
                "tool_key": selected.tool_key,
                "invocation_digest": selected.invocation_digest,
                "status": "succeeded",
                "payload_ref": payload.content_ref,
                "started_at": "2026-07-22T12:34:55.000Z",
                "completed_at": "2026-07-22T12:34:56.000Z",
                "elapsed_ms": elapsed_ms,
            },
        )
        return SimpleNamespace(raw=raw, payload=payload, receipt=receipt)

    def budget(self) -> tuple[int, int, int, int]:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT consumed_ms, reserved_ms, calls_consumed, calls_reserved "
                "FROM dispatch_budgets WHERE task_id = ? AND grant_digest = ?",
                (self.authority.task_id, self.authority.grant_digest),
            ).fetchone()
        return tuple(row)
