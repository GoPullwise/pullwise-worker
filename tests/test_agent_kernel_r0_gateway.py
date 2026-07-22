from __future__ import annotations

from dataclasses import fields, replace
import hashlib
from pathlib import Path
import tempfile
import unittest

from tests.agent_kernel_capture_fakes import FakeCaptureProvider
from pullwise_worker.agent_kernel_gateway import (
    AgentKernelGateway,
    CheckedInvocation,
    DispatchDecision,
    GatewayError,
    ReplayState,
    ToolDescriptor,
)
from pullwise_worker.agent_kernel_r0_read import (
    R0ReadDispatcher,
    R0ReadPreparer,
    ReadSourceFileInput,
)
from pullwise_worker.agent_kernel_source_state import (
    SourceSelectionPolicy,
    SourceStateError,
)


BASE_REVISION = "a" * 40


class _Authorities:
    def __init__(self, call: CheckedInvocation) -> None:
        self.call = call
        self.ticket = object()
        self.capability = object()
        self.reservation_plan = object()
        self.result = object()
        self.settlements = {
            "success": 0,
            "violation": 0,
            "unavailable": 0,
            "dispatch_failure": 0,
        }

    def validate(self, raw: bytes) -> CheckedInvocation:
        return self.call

    def probe(self, task_id: str, key: str, digest: str) -> ReplayState:
        return ReplayState.new()

    def assert_actor_current(self, call: CheckedInvocation) -> object:
        return self.ticket

    def assert_lease_current(self, ticket: object, call: CheckedInvocation) -> None:
        return None

    def assert_runnable(self, ticket: object, call: CheckedInvocation) -> None:
        return None

    def resolve(self, tool_key: str) -> ToolDescriptor:
        return ToolDescriptor(
            tool_key="internal.read_source",
            tool_version="test",
            risk="R0",
            capability="source.read",
            uses_command=False,
            uses_network=False,
            uses_secret=False,
            requests_approval=False,
        )

    def assert_capability(self, *args: object) -> None:
        return None

    def assert_execution_controls(self, *args: object) -> None:
        return None

    def plan_reservation(self, *args: object) -> object:
        return self.reservation_plan

    def begin(self, ticket: object, *args: object) -> DispatchDecision:
        if ticket is not self.ticket:
            raise AssertionError("stale authority ticket")
        return DispatchDecision.winner(self.capability)

    def _settle(self, kind: str) -> object:
        self.settlements[kind] += 1
        return self.result

    def commit(self, *args: object) -> object:
        return self._settle("success")

    def commit_source_violation(self, *args: object) -> object:
        return self._settle("violation")

    def commit_source_unavailable(self, *args: object) -> object:
        return self._settle("unavailable")

    def commit_dispatch_failure(self, *args: object) -> object:
        return self._settle("dispatch_failure")


class _CapturingDispatcher:
    def __init__(
        self,
        provider: FakeCaptureProvider,
        mutate: Path | None = None,
    ) -> None:
        self.provider = provider
        self.mutate = mutate
        self.handle: object | None = None

    def dispatch(self, capability: object, prepared: object) -> object:
        self.handle = prepared.dispatch_handle
        if self.provider.latest_session.closed:
            raise AssertionError("capture lease released before dispatch")
        receipt = R0ReadDispatcher().dispatch(capability, prepared)
        if self.provider.latest_session.closed:
            raise AssertionError("capture lease released during dispatch")
        if self.mutate is not None:
            self.mutate.write_bytes(b"changed after read")
        return receipt


class _FailingDispatcher:
    def __init__(self, error: BaseException) -> None:
        self.error = error
        self.handle: object | None = None

    def dispatch(self, capability: object, prepared: object) -> object:
        del capability
        self.handle = prepared.dispatch_handle
        raise self.error


class _ChangedSourceDispatcher:
    def __init__(self, target: Path) -> None:
        self.target = target
        self.handle: object | None = None

    def dispatch(self, capability: object, prepared: object) -> object:
        self.handle = prepared.dispatch_handle
        self.target.write_bytes(b"changed before descriptor read")
        return R0ReadDispatcher().dispatch(capability, prepared)


class AgentKernelR0GatewayIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.scratch = tempfile.TemporaryDirectory(prefix="agent-kernel-r0-gateway-")
        self.root = Path(self.scratch.name) / "repository"
        self.root.mkdir()
        self.target = self.root / "README.md"
        self.payload = b"raw result bytes"
        self.target.write_bytes(self.payload)
        self.call = CheckedInvocation(
            idempotency_key="idem-" + "1" * 32,
            invocation_digest=hashlib.sha256(b"request").hexdigest(),
            authority_digest="a" * 64,
            package_content_sha256="b" * 64,
            package_root_sha256="c" * 64,
            grant_digest="d" * 64,
            task_id="task-" + "2" * 32,
            attempt_id="attempt-" + "3" * 32,
            owner_id="owner-" + "4" * 32,
            session_id="session-" + "4" * 32,
            lease_id="lease-" + "5" * 32,
            task_version=1,
            deletion_version=0,
            owner_epoch=1,
            native_epoch=1,
            transport_epoch=1,
            tool_key="internal.read_source",
            tool_input=ReadSourceFileInput("README.md"),
        )
        self.policy = SourceSelectionPolicy.pullwise_full_scan(
            root_identity="repository:r0-gateway-test"
        )
        self.capture_provider = FakeCaptureProvider(
            self.root, self.policy, base_revision=BASE_REVISION
        )

    def tearDown(self) -> None:
        self.scratch.cleanup()

    def _preparer(self) -> R0ReadPreparer:
        return R0ReadPreparer(
            capture_provider=self.capture_provider,
            base_revision=BASE_REVISION,
            max_bytes=1024,
        )

    def _gateway(
        self, authorities: _Authorities, preparer: object, dispatcher: object
    ) -> AgentKernelGateway:
        return AgentKernelGateway(
            codec=authorities,
            journal=authorities,
            authority=authorities,
            catalog=authorities,
            policy=authorities,
            preparer=preparer,
            budget=authorities,
            dispatcher=dispatcher,
            committer=authorities,
        )

    def test_checked_invocation_carries_codec_derived_d30_facts(self) -> None:
        names = {item.name for item in fields(CheckedInvocation)}
        trusted_facts = {
            "authority_digest", "package_content_sha256", "package_root_sha256",
            "grant_digest", "owner_id", "lease_id", "task_version",
            "deletion_version", "transport_epoch",
        }
        self.assertTrue(trusted_facts.issubset(names))

    def test_checked_invocation_rejects_invalid_d30_facts(self) -> None:
        cases = (
            ("invocation_digest", "A" * 64, "INVOCATION_DIGEST_INVALID"),
            ("authority_digest", "A" * 64, "AUTHORITY_DIGEST_INVALID"),
            ("package_content_sha256", "A" * 64, "PACKAGE_DIGEST_INVALID"),
            ("package_root_sha256", "A" * 64, "PACKAGE_DIGEST_INVALID"),
            ("grant_digest", "A" * 64, "GRANT_DIGEST_INVALID"),
            ("owner_id", "", "INVOCATION_FACTS_INVALID"),
            ("lease_id", "", "INVOCATION_FACTS_INVALID"),
            ("task_version", 0, "TASK_VERSION_INVALID"),
            ("task_version", True, "TASK_VERSION_INVALID"),
            ("deletion_version", -1, "DELETION_VERSION_INVALID"),
            ("deletion_version", True, "DELETION_VERSION_INVALID"),
            ("transport_epoch", 0, "INVOCATION_EPOCH_INVALID"),
            ("transport_epoch", True, "INVOCATION_EPOCH_INVALID"),
        )
        for name, value, code in cases:
            with self.subTest(name=name, value=value):
                with self.assertRaisesRegex(GatewayError, code):
                    replace(self.call, **{name: value})

    def test_real_read_mutation_withholds_payload_and_settles_once(self) -> None:
        authorities = _Authorities(self.call)
        dispatcher = _CapturingDispatcher(self.capture_provider, self.target)

        result = self._gateway(
            authorities, self._preparer(), dispatcher
        ).invoke(b"request")

        self.assertIs(authorities.result, result)
        self.assertNotEqual(self.payload, result)
        self.assertEqual(1, authorities.settlements["violation"])
        self.assertEqual(1, sum(authorities.settlements.values()))
        self.assertTrue(dispatcher.handle.closed)
        self.assertTrue(self.capture_provider.latest_session.closed)

    def test_real_read_after_snapshot_failure_settles_once(self) -> None:
        authorities = _Authorities(self.call)
        dispatcher = _CapturingDispatcher(self.capture_provider)
        self.capture_provider.after_error = SourceStateError(
            "SOURCE_AFTER_UNAVAILABLE"
        )

        result = self._gateway(
            authorities, self._preparer(), dispatcher
        ).invoke(b"request")

        self.assertIs(authorities.result, result)
        self.assertNotEqual(self.payload, result)
        self.assertEqual(1, authorities.settlements["unavailable"])
        self.assertEqual(1, sum(authorities.settlements.values()))
        self.assertTrue(dispatcher.handle.closed)
        self.assertTrue(self.capture_provider.latest_session.closed)

    def test_dispatch_failure_closes_descriptor_and_capture_session(self) -> None:
        authorities = _Authorities(self.call)
        dispatcher = _FailingDispatcher(RuntimeError("dispatch failed"))

        result = self._gateway(
            authorities, self._preparer(), dispatcher
        ).invoke(b"request")

        self.assertIs(authorities.result, result)
        self.assertEqual(1, authorities.settlements["dispatch_failure"])
        self.assertTrue(dispatcher.handle.closed)
        self.assertTrue(self.capture_provider.latest_session.closed)

    def test_failure_after_descriptor_take_still_releases_capture(self) -> None:
        authorities = _Authorities(self.call)
        dispatcher = _ChangedSourceDispatcher(self.target)

        result = self._gateway(
            authorities, self._preparer(), dispatcher
        ).invoke(b"request")

        self.assertIs(authorities.result, result)
        self.assertEqual(1, authorities.settlements["dispatch_failure"])
        self.assertTrue(dispatcher.handle.closed)
        self.assertTrue(self.capture_provider.latest_session.closed)

    def test_cancellation_closes_descriptor_and_capture_session(self) -> None:
        authorities = _Authorities(self.call)
        dispatcher = _FailingDispatcher(KeyboardInterrupt("cancelled"))

        with self.assertRaisesRegex(KeyboardInterrupt, "cancelled"):
            self._gateway(
                authorities, self._preparer(), dispatcher
            ).invoke(b"request")

        self.assertEqual(0, sum(authorities.settlements.values()))
        self.assertTrue(dispatcher.handle.closed)
        self.assertTrue(self.capture_provider.latest_session.closed)


if __name__ == "__main__":
    unittest.main()
