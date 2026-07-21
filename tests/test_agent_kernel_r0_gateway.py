from __future__ import annotations

import hashlib
from pathlib import Path
import tempfile
import unittest

from pullwise_worker.agent_kernel_gateway import (
    AgentKernelGateway,
    CheckedInvocation,
    DispatchDecision,
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
        self.reservation = object()
        self.result = object()
        self.settlements = {
            "success": 0,
            "violation": 0,
            "unavailable": 0,
            "dispatch_failure": 0,
        }

    def validate(self, raw: bytes) -> CheckedInvocation:
        return self.call

    def probe(self, key: str, digest: str) -> ReplayState:
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

    def reserve(self, *args: object) -> object:
        return self.reservation

    def release_before_dispatch(self, reservation: object) -> None:
        raise AssertionError("winner must not release before dispatch")

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
    def __init__(self, mutate: Path | None = None) -> None:
        self.mutate = mutate
        self.handle: object | None = None

    def dispatch(self, capability: object, prepared: object) -> object:
        self.handle = prepared.dispatch_handle
        receipt = R0ReadDispatcher().dispatch(capability, prepared)
        if self.mutate is not None:
            self.mutate.write_bytes(b"changed after read")
        return receipt


class _UnavailableAfterPreparer:
    def __init__(self, delegate: R0ReadPreparer) -> None:
        self.delegate = delegate
        self.handle: object | None = None

    def prepare(self, *args: object) -> object:
        prepared = self.delegate.prepare(*args)
        self.handle = prepared.dispatch_handle
        return prepared

    def capture_after(self, prepared: object) -> object:
        raise SourceStateError("SOURCE_AFTER_UNAVAILABLE")

    def discard(self, prepared: object) -> None:
        self.delegate.discard(prepared)


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
            task_id="task-" + "2" * 32,
            attempt_id="attempt-" + "3" * 32,
            session_id="session-" + "4" * 32,
            owner_epoch=1,
            native_epoch=1,
            tool_key="internal.read_source",
            tool_input=ReadSourceFileInput("README.md"),
        )
        self.policy = SourceSelectionPolicy.pullwise_full_scan(
            root_identity="repository:r0-gateway-test"
        )

    def tearDown(self) -> None:
        self.scratch.cleanup()

    def _preparer(self) -> R0ReadPreparer:
        return R0ReadPreparer(
            root=self.root,
            policy=self.policy,
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

    def test_real_read_mutation_withholds_payload_and_settles_once(self) -> None:
        authorities = _Authorities(self.call)
        dispatcher = _CapturingDispatcher(self.target)

        result = self._gateway(
            authorities, self._preparer(), dispatcher
        ).invoke(b"request")

        self.assertIs(authorities.result, result)
        self.assertNotEqual(self.payload, result)
        self.assertEqual(1, authorities.settlements["violation"])
        self.assertEqual(1, sum(authorities.settlements.values()))
        self.assertTrue(dispatcher.handle.closed)

    def test_real_read_after_snapshot_failure_settles_once(self) -> None:
        authorities = _Authorities(self.call)
        dispatcher = _CapturingDispatcher()
        preparer = _UnavailableAfterPreparer(self._preparer())

        result = self._gateway(
            authorities, preparer, dispatcher
        ).invoke(b"request")

        self.assertIs(authorities.result, result)
        self.assertNotEqual(self.payload, result)
        self.assertEqual(1, authorities.settlements["unavailable"])
        self.assertEqual(1, sum(authorities.settlements.values()))
        self.assertTrue(preparer.handle.closed)


if __name__ == "__main__":
    unittest.main()
