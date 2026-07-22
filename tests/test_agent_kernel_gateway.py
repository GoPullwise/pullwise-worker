from __future__ import annotations

from dataclasses import replace
import hashlib
import unittest

from pullwise_worker.agent_kernel_gateway import (
    AgentKernelGateway,
    CheckedInvocation,
    DispatchDecision,
    GatewayError,
    PreparedDispatch,
    ReplayState,
    ToolDescriptor,
)
from pullwise_worker.agent_kernel_source_state import (
    SourceEntry,
    SourceSelectionPolicy,
    SourceTreeSnapshot,
)


BASE_REVISION = "a" * 40


class _InjectedCancellation(BaseException):
    pass


class GatewayRig:
    def __init__(self) -> None:
        self.stages: list[str] = []
        self.failures: set[str] = set()
        self.cancel_at: str | None = None
        self.replay = ReplayState.new()
        self.authority_ticket = object()
        self.dispatch_capability = object()
        self.begin_decision = DispatchDecision.winner(self.dispatch_capability)
        self.fence_after_plan = False
        self.authority_fenced = False
        self.descriptor = ToolDescriptor(
            tool_key="internal.read_source",
            tool_version="test",
            risk="R0",
            capability="source.read",
            uses_command=False,
            uses_network=False,
            uses_secret=False,
            requests_approval=False,
        )
        policy = SourceSelectionPolicy.pullwise_full_scan(
            root_identity="repository:gateway-test"
        )
        self.before = SourceTreeSnapshot(
            base_revision=BASE_REVISION,
            selection_policy_digest=policy.digest,
            entries=(),
        )
        self.after = self.before
        self.receipt = object()
        self.result = object()
        self.reservation_plan = object()
        self.discarded: list[PreparedDispatch] = []

    def _stage(self, name: str) -> None:
        self.stages.append(name)
        if name == self.cancel_at:
            raise _InjectedCancellation(name)
        if name in self.failures:
            raise GatewayError(name.upper())

    def validate(self, raw: bytes) -> CheckedInvocation:
        self._stage("codec")
        return CheckedInvocation(
            idempotency_key="idem-" + "1" * 32,
            invocation_digest=hashlib.sha256(raw).hexdigest(),
            authority_digest="a" * 64,
            package_content_sha256="b" * 64,
            package_root_sha256="c" * 64,
            grant_digest="d" * 64,
            task_id="task-" + "2" * 32,
            attempt_id="attempt-" + "3" * 32,
            owner_id="owner-" + "4" * 32,
            session_id="session-" + "4" * 32,
            lease_id="lease-" + "5" * 32,
            task_version=13,
            deletion_version=0,
            owner_epoch=7,
            native_epoch=11,
            transport_epoch=5,
            tool_key="internal.read_source",
        )

    def probe(self, key: str, digest: str) -> ReplayState:
        self._stage("probe")
        return self.replay

    def assert_actor_current(self, call: CheckedInvocation) -> object:
        self._stage("actor")
        return self.authority_ticket

    def assert_lease_current(self, ticket: object, call: CheckedInvocation) -> None:
        self._stage("lease")

    def assert_runnable(self, ticket: object, call: CheckedInvocation) -> None:
        self._stage("runnable")

    def resolve(self, tool_key: str) -> ToolDescriptor:
        self._stage("catalog")
        return self.descriptor

    def assert_capability(
        self, ticket: object, call: CheckedInvocation, descriptor: ToolDescriptor
    ) -> None:
        self._stage("capability")
        if descriptor.risk != "R0":
            raise GatewayError("CAPABILITY_NOT_IMPLEMENTED")

    def prepare(
        self, ticket: object, call: CheckedInvocation, descriptor: ToolDescriptor
    ) -> PreparedDispatch:
        self._stage("prepare")
        return PreparedDispatch(
            tool_key=descriptor.tool_key,
            tool_version=descriptor.tool_version,
            source_before=self.before,
            dispatch_handle=object(),
        )

    def assert_execution_controls(
        self,
        ticket: object,
        call: CheckedInvocation,
        descriptor: ToolDescriptor,
        prepared: PreparedDispatch,
    ) -> None:
        self._stage("controls")

    def plan_reservation(
        self, ticket: object, call: CheckedInvocation, descriptor: ToolDescriptor
    ) -> object:
        self._stage("plan_reservation")
        if self.fence_after_plan:
            self.authority_fenced = True
        return self.reservation_plan

    def begin(
        self,
        authority_ticket: object,
        call: CheckedInvocation,
        descriptor: ToolDescriptor,
        prepared: PreparedDispatch,
        reservation_plan: object,
    ) -> DispatchDecision:
        self._stage("begin")
        if authority_ticket is not self.authority_ticket:
            raise GatewayError("AUTHORITY_TICKET_MISMATCH")
        if reservation_plan is not self.reservation_plan:
            raise GatewayError("RESERVATION_PLAN_MISMATCH")
        if self.authority_fenced:
            raise GatewayError("AUTHORITY_FENCED")
        return self.begin_decision

    def dispatch(
        self, dispatch_capability: object, prepared: PreparedDispatch
    ) -> object:
        self._stage("dispatch")
        if dispatch_capability is not self.dispatch_capability:
            raise GatewayError("DISPATCH_CAPABILITY_MISMATCH")
        self.assertFalseRawInvocation(prepared)
        return self.receipt

    def assertFalseRawInvocation(self, prepared: PreparedDispatch) -> None:
        if hasattr(prepared, "raw") or hasattr(prepared, "relative_path"):
            raise AssertionError("dispatcher received unresolved invocation data")

    def capture_after(
        self, prepared: PreparedDispatch
    ) -> SourceTreeSnapshot:
        self._stage("after")
        return self.after

    def discard(self, prepared: PreparedDispatch) -> None:
        self.stages.append("discard")
        self.discarded.append(prepared)

    def commit(
        self,
        dispatch_capability: object,
        call: CheckedInvocation,
        prepared: PreparedDispatch,
        receipt: object,
        source_after: SourceTreeSnapshot,
    ) -> object:
        self._stage("commit")
        return self.result

    def commit_source_violation(self, *args: object) -> object:
        self._stage("violation")
        return self.result

    def commit_source_unavailable(self, *args: object) -> object:
        self.stages.append("unavailable")
        return self.result

    def commit_dispatch_failure(self, *args: object) -> object:
        self.stages.append("dispatch_failure")
        return self.result

    def gateway(self) -> AgentKernelGateway:
        return AgentKernelGateway(
            codec=self,
            journal=self,
            authority=self,
            catalog=self,
            policy=self,
            preparer=self,
            budget=self,
            dispatcher=self,
            committer=self,
        )


class AgentKernelGatewayTest(unittest.TestCase):
    def test_success_path_has_one_fixed_order(self) -> None:
        rig = GatewayRig()

        result = rig.gateway().invoke(b"request")

        self.assertIs(rig.result, result)
        self.assertEqual(
            [
                "codec",
                "probe",
                "actor",
                "lease",
                "runnable",
                "catalog",
                "capability",
                "prepare",
                "controls",
                "plan_reservation",
                "begin",
                "dispatch",
                "after",
                "commit",
            ],
            rig.stages,
        )

    def test_completed_replay_skips_all_authority_and_dispatch(self) -> None:
        rig = GatewayRig()
        replayed = object()
        rig.replay = ReplayState.completed(replayed)

        self.assertIs(replayed, rig.gateway().invoke(b"request"))
        self.assertEqual(["codec", "probe"], rig.stages)

    def test_pending_replay_never_dispatches(self) -> None:
        rig = GatewayRig()
        rig.replay = ReplayState.pending()

        with self.assertRaisesRegex(GatewayError, "INVOCATION_PENDING"):
            rig.gateway().invoke(b"request")
        self.assertEqual(["codec", "probe"], rig.stages)

    def test_pre_dispatch_failure_precedence_is_stable(self) -> None:
        ordered = (
            "actor",
            "lease",
            "runnable",
            "catalog",
            "capability",
            "prepare",
            "controls",
            "plan_reservation",
        )
        for expected_index, expected in enumerate(ordered):
            with self.subTest(expected=expected):
                rig = GatewayRig()
                rig.failures = set(ordered[expected_index:])
                with self.assertRaisesRegex(GatewayError, expected.upper()):
                    rig.gateway().invoke(b"request")
                self.assertIn(expected, rig.stages)
                self.assertNotIn("begin", rig.stages)
                self.assertNotIn("dispatch", rig.stages)

    def test_non_r0_descriptor_is_denied_before_prepare_or_budget(self) -> None:
        rig = GatewayRig()
        rig.descriptor = replace(
            rig.descriptor,
            risk="R2",
            capability="network.read",
            uses_network=True,
        )

        with self.assertRaisesRegex(GatewayError, "CAPABILITY_NOT_IMPLEMENTED"):
            rig.gateway().invoke(b"request")
        self.assertNotIn("prepare", rig.stages)
        self.assertNotIn("plan_reservation", rig.stages)

    def test_lost_intent_race_discards_only_and_does_not_dispatch(self) -> None:
        replayed = object()
        decisions = (DispatchDecision.pending(), DispatchDecision.completed(replayed))
        for decision in decisions:
            with self.subTest(decision=decision.kind):
                rig = GatewayRig()
                rig.begin_decision = decision
                if decision.kind == "PENDING":
                    with self.assertRaisesRegex(GatewayError, "INVOCATION_PENDING"):
                        rig.gateway().invoke(b"request")
                else:
                    self.assertIs(replayed, rig.gateway().invoke(b"request"))
                self.assertEqual(1, len(rig.discarded))
                self.assertNotIn("dispatch", rig.stages)
                self.assertFalse(hasattr(rig, "release_before_dispatch"))

    def test_fenced_authority_cannot_win_intent_or_dispatch(self) -> None:
        rig = GatewayRig()
        rig.fence_after_plan = True

        with self.assertRaisesRegex(GatewayError, "AUTHORITY_FENCED"):
            rig.gateway().invoke(b"request")

        self.assertEqual(1, len(rig.discarded))
        self.assertEqual(["begin", "discard"], rig.stages[-2:])
        self.assertNotIn("dispatch", rig.stages)

    def test_intent_failure_discards_only_before_propagating(self) -> None:
        rig = GatewayRig()
        rig.failures = {"begin"}

        with self.assertRaisesRegex(GatewayError, "BEGIN"):
            rig.gateway().invoke(b"request")
        self.assertEqual(1, len(rig.discarded))
        self.assertFalse(hasattr(rig, "release_before_dispatch"))
        self.assertNotIn("dispatch", rig.stages)

    def test_cancellation_discards_prepared_state_without_false_settlement(self) -> None:
        for stage in ("controls", "plan_reservation", "begin", "dispatch"):
            with self.subTest(stage=stage):
                rig = GatewayRig()
                rig.cancel_at = stage
                with self.assertRaises(_InjectedCancellation):
                    rig.gateway().invoke(b"request")
                self.assertEqual(1, len(rig.discarded))
                self.assertFalse(hasattr(rig, "release_before_dispatch"))
                self.assertTrue(
                    {"commit", "violation", "unavailable", "dispatch_failure"}
                    .isdisjoint(rig.stages)
                )

    def test_dispatch_failure_is_committed_without_exposing_raw_error(self) -> None:
        rig = GatewayRig()
        rig.failures = {"dispatch"}

        self.assertIs(rig.result, rig.gateway().invoke(b"request"))
        self.assertEqual(["discard", "dispatch_failure"], rig.stages[-2:])
        self.assertEqual(1, len(rig.discarded))
        self.assertNotIn("commit", rig.stages)

    def test_source_mutation_withholds_receipt_and_commits_violation(self) -> None:
        rig = GatewayRig()
        changed = SourceEntry.file(
            "changed.txt",
            size_bytes=1,
            sha256=hashlib.sha256(b"x").hexdigest(),
        )
        rig.after = SourceTreeSnapshot(
            base_revision=rig.before.base_revision,
            selection_policy_digest=rig.before.selection_policy_digest,
            entries=(changed,),
        )

        self.assertIs(rig.result, rig.gateway().invoke(b"request"))
        self.assertEqual("violation", rig.stages[-1])
        self.assertNotIn("commit", rig.stages)

    def test_post_snapshot_failure_is_committed_as_unavailable(self) -> None:
        rig = GatewayRig()
        rig.failures = {"after"}

        self.assertIs(rig.result, rig.gateway().invoke(b"request"))
        self.assertEqual("unavailable", rig.stages[-1])
        self.assertNotIn("commit", rig.stages)

if __name__ == "__main__":
    unittest.main()
