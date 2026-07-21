from __future__ import annotations

import hashlib
import os
from pathlib import Path
import tempfile
import unittest

from pullwise_worker.agent_kernel_gateway import (
    AgentKernelGateway,
    CheckedInvocation,
    DispatchDecision,
    PreparedDispatch,
    ReplayState,
    ToolDescriptor,
)
from pullwise_worker.agent_kernel_r0_read import (
    R0ReadDispatcher,
    R0ReadError,
    R0ReadPreparer,
    ReadSourceFileInput,
)
from pullwise_worker.agent_kernel_source_state import (
    SourceSelectionPolicy,
    diff_source_trees,
)


BASE_REVISION = "a" * 40


class AgentKernelR0ReadTest(unittest.TestCase):
    def setUp(self) -> None:
        self.scratch = tempfile.TemporaryDirectory(prefix="agent-kernel-r0-")
        self.root = Path(self.scratch.name) / "repository"
        self.root.mkdir()
        (self.root / "nested").mkdir()
        self.target = self.root / "nested" / "README.md"
        self.payload = b"raw\r\nbytes\x00"
        self.target.write_bytes(self.payload)
        self.policy = SourceSelectionPolicy.pullwise_full_scan(
            root_identity="repository:r0-test"
        )
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

    def tearDown(self) -> None:
        self.scratch.cleanup()

    def call(self, relative_path: str) -> CheckedInvocation:
        return CheckedInvocation(
            idempotency_key="idem-" + "1" * 32,
            invocation_digest="2" * 64,
            task_id="task-" + "3" * 32,
            attempt_id="attempt-" + "4" * 32,
            session_id="session-" + "5" * 32,
            owner_epoch=1,
            native_epoch=1,
            tool_key="internal.read_source",
            tool_input=ReadSourceFileInput(relative_path),
        )

    def preparer(self, **values: object) -> R0ReadPreparer:
        return R0ReadPreparer(
            root=self.root,
            policy=self.policy,
            base_revision=BASE_REVISION,
            max_bytes=1024,
            **values,
        )

    def test_prepared_dispatch_holds_only_verified_descriptor(self) -> None:
        prepared = self.preparer().prepare(
            object(), self.call("nested/README.md"), self.descriptor
        )

        self.assertFalse(hasattr(prepared, "relative_path"))
        self.assertFalse(hasattr(prepared.dispatch_handle, "relative_path"))
        receipt = R0ReadDispatcher().dispatch(prepared)
        self.assertEqual(self.payload, receipt.payload)
        self.assertEqual(hashlib.sha256(self.payload).hexdigest(), receipt.sha256)
        self.assertEqual(len(self.payload), receipt.size_bytes)
        self.assertTrue(prepared.dispatch_handle.closed)

    def test_after_snapshot_is_a_fresh_source_identity(self) -> None:
        preparer = self.preparer()
        prepared = preparer.prepare(
            object(), self.call("nested/README.md"), self.descriptor
        )
        R0ReadDispatcher().dispatch(prepared)

        after = preparer.capture_after(prepared)

        self.assertTrue(diff_source_trees(prepared.source_before, after).is_empty)

    def test_path_grammar_fails_before_source_open(self) -> None:
        invalid = (
            "/absolute",
            "../escape",
            "nested/../escape",
            "nested" + chr(92) + "README.md",
            "bad\x00name",
            "e\u0301.txt",
        )
        for value in invalid:
            with self.subTest(value=value):
                with self.assertRaises(R0ReadError):
                    ReadSourceFileInput(value)

    def test_symlink_component_and_leaf_are_never_followed(self) -> None:
        outside = Path(self.scratch.name) / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("secret", encoding="utf-8")
        component = self.root / "component"
        leaf = self.root / "leaf"
        try:
            component.symlink_to(outside, target_is_directory=True)
            leaf.symlink_to(outside / "secret.txt")
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlink creation unavailable: {exc}")

        for relative in ("component/secret.txt", "leaf"):
            with self.subTest(relative=relative):
                with self.assertRaisesRegex(R0ReadError, "READ_PATH_UNSAFE"):
                    self.preparer().prepare(
                        object(), self.call(relative), self.descriptor
                    )

    def test_directory_and_oversized_leaf_are_rejected(self) -> None:
        with self.assertRaisesRegex(R0ReadError, "READ_LEAF_NOT_REGULAR"):
            self.preparer().prepare(
                object(), self.call("nested"), self.descriptor
            )
        self.target.write_bytes(b"x" * 1025)
        with self.assertRaisesRegex(R0ReadError, "READ_SIZE_LIMIT"):
            self.preparer().prepare(
                object(), self.call("nested/README.md"), self.descriptor
            )

    def test_source_change_between_snapshot_and_open_is_rejected(self) -> None:
        changed = False

        def mutate(stage: str, path: Path) -> None:
            nonlocal changed
            if stage == "after_source_before" and not changed:
                changed = True
                self.target.write_bytes(b"different")

        with self.assertRaisesRegex(R0ReadError, "READ_SOURCE_ENTRY_CHANGED"):
            self.preparer(stage_hook=mutate).prepare(
                object(), self.call("nested/README.md"), self.descriptor
            )

    def test_replacing_path_after_prepare_cannot_redirect_held_descriptor(self) -> None:
        prepared = self.preparer().prepare(
            object(), self.call("nested/README.md"), self.descriptor
        )
        replacement = self.root / "replacement"
        replacement.write_bytes(b"replacement")
        try:
            self.target.unlink()
            replacement.rename(self.target)
        except OSError as exc:
            prepared.dispatch_handle.discard()
            self.skipTest(f"host does not permit replacing an open file: {exc}")

        receipt = R0ReadDispatcher().dispatch(prepared)

        self.assertEqual(self.payload, receipt.payload)

    def test_discard_is_idempotent_and_prevents_dispatch(self) -> None:
        preparer = self.preparer()
        prepared = preparer.prepare(
            object(), self.call("nested/README.md"), self.descriptor
        )

        preparer.discard(prepared)
        preparer.discard(prepared)

        self.assertTrue(prepared.dispatch_handle.closed)
        with self.assertRaisesRegex(R0ReadError, "PREPARED_READ_CLOSED"):
            R0ReadDispatcher().dispatch(prepared)

    def test_prepare_hook_failure_does_not_leak_the_leaf_descriptor(self) -> None:
        def fail(stage: str, path: Path) -> None:
            if stage == "after_file_prepared":
                raise RuntimeError("injected hook failure")

        with self.assertRaisesRegex(RuntimeError, "injected hook failure"):
            self.preparer(stage_hook=fail).prepare(
                object(), self.call("nested/README.md"), self.descriptor
            )

        self.target.unlink()

    def test_dispatcher_rejects_untrusted_handle_shape(self) -> None:
        preparer = self.preparer()
        valid = preparer.prepare(
            object(), self.call("nested/README.md"), self.descriptor
        )
        before = valid.source_before
        preparer.discard(valid)
        forged = PreparedDispatch(
            tool_key="internal.read_source",
            tool_version="test",
            source_before=before,
            dispatch_handle=object(),
        )

        with self.assertRaisesRegex(R0ReadError, "PREPARED_READ_INVALID"):
            R0ReadDispatcher().dispatch(forged)

    def test_real_r0_reader_composes_through_the_gateway(self) -> None:
        call = self.call("nested/README.md")
        tracer = _TracerAuthorities(call)
        preparer = self.preparer()
        gateway = AgentKernelGateway(
            codec=tracer,
            journal=tracer,
            authority=tracer,
            catalog=tracer,
            policy=tracer,
            preparer=preparer,
            budget=tracer,
            dispatcher=R0ReadDispatcher(),
            committer=tracer,
        )

        result = gateway.invoke(b"package-validated-request")

        self.assertEqual(self.payload, result)
        self.assertEqual(1, tracer.commit_count)


class _TracerAuthorities:
    def __init__(self, call: CheckedInvocation) -> None:
        self.call = call
        self.reservation = object()
        self.commit_count = 0

    def validate(self, raw: bytes) -> CheckedInvocation:
        return self.call

    def probe(self, key: str, digest: str) -> ReplayState:
        return ReplayState.new()

    def assert_actor_current(self, call: CheckedInvocation) -> object:
        return object()

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
        raise AssertionError("winning dispatch must not release before dispatch")

    def begin(self, *args: object) -> DispatchDecision:
        return DispatchDecision.winner(object())

    def commit(self, *args: object) -> bytes:
        receipt = args[3]
        self.commit_count += 1
        return receipt.payload

    def commit_source_violation(self, *args: object) -> object:
        raise AssertionError("unchanged source cannot be a violation")

    def commit_source_unavailable(self, *args: object) -> object:
        raise AssertionError("source identity must remain available")

    def commit_dispatch_failure(self, *args: object) -> object:
        raise AssertionError("descriptor dispatch must succeed")


if __name__ == "__main__":
    unittest.main()
