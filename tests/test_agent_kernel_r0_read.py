from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tests.agent_kernel_capture_fakes import FakeCaptureProvider
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
    SourceEntry,
    SourceSelectionPolicy,
    SourceTreeSnapshot,
    snapshot_source_tree,
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
        self.capture_provider = self.new_capture_provider()
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
            authority_digest="a" * 64,
            package_content_sha256="b" * 64,
            package_root_sha256="c" * 64,
            grant_digest="d" * 64,
            task_id="task-" + "3" * 32,
            attempt_id="attempt-" + "4" * 32,
            owner_id="owner-" + "5" * 32,
            session_id="session-" + "5" * 32,
            lease_id="lease-" + "6" * 32,
            task_version=1,
            deletion_version=0,
            owner_epoch=1,
            native_epoch=1,
            transport_epoch=1,
            tool_key="internal.read_source",
            tool_input=ReadSourceFileInput(relative_path),
        )

    def preparer(self, **values: object) -> R0ReadPreparer:
        provider = values.pop("capture_provider", self.capture_provider)
        return R0ReadPreparer(
            capture_provider=provider,
            base_revision=BASE_REVISION,
            max_bytes=1024,
            **values,
        )

    def new_capture_provider(self) -> FakeCaptureProvider:
        return FakeCaptureProvider(
            self.root, self.policy, base_revision=BASE_REVISION
        )

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
            provider = self.new_capture_provider()
            with self.subTest(relative=relative), mock.patch(
                "pullwise_worker.agent_kernel_r0_read._open_verified",
                side_effect=AssertionError("unsafe path was opened"),
            ) as open_verified:
                with self.assertRaisesRegex(R0ReadError, "READ_PATH_UNSAFE"):
                    self.preparer(capture_provider=provider).prepare(
                        object(), self.call(relative), self.descriptor
                    )
                open_verified.assert_not_called()
                self.assertTrue(provider.latest_session.closed)

    def test_gitlink_ancestor_is_rejected_before_any_leaf_open(self) -> None:
        gitlink = self.root / "submodule"
        gitlink.mkdir()
        (gitlink / "secret.txt").write_text("secret", encoding="utf-8")
        scanned = snapshot_source_tree(
            self.root, policy=self.policy, base_revision=BASE_REVISION
        )
        entries = tuple(
            sorted(
                (
                    SourceEntry.gitlink(
                        "submodule", commit_sha="b" * 40
                    ),
                    *(entry for entry in scanned.entries if not entry.path.startswith("submodule/")),
                ),
                key=lambda entry: entry.path.encode("utf-8"),
            )
        )
        provider = self.new_capture_provider()
        provider.before_snapshot = SourceTreeSnapshot(
            BASE_REVISION, self.policy.digest, entries
        )

        with mock.patch(
            "pullwise_worker.agent_kernel_r0_read._open_verified",
            side_effect=AssertionError("gitlink descendant was opened"),
        ) as open_verified, self.assertRaisesRegex(
            R0ReadError, "READ_PATH_UNSAFE"
        ):
            self.preparer(capture_provider=provider).prepare(
                object(), self.call("submodule/secret.txt"), self.descriptor
            )

        open_verified.assert_not_called()
        self.assertTrue(provider.latest_session.closed)

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

    def test_descriptor_reads_are_always_explicitly_bounded(self) -> None:
        read_sizes: list[int] = []
        real_fdopen = os.fdopen

        class GuardedFile:
            def __init__(self, handle: object) -> None:
                self.handle = handle

            def __enter__(self) -> "GuardedFile":
                return self

            def __exit__(self, *args: object) -> object:
                return self.handle.__exit__(*args)

            def fileno(self) -> int:
                return self.handle.fileno()

            def read(self, size: int = -1) -> bytes:
                if size < 0:
                    raise AssertionError("unbounded descriptor read")
                read_sizes.append(size)
                return self.handle.read(size)

        def guarded_fdopen(*args: object, **kwargs: object) -> GuardedFile:
            return GuardedFile(real_fdopen(*args, **kwargs))

        with mock.patch(
            "pullwise_worker.agent_kernel_r0_read.os.fdopen",
            side_effect=guarded_fdopen,
        ):
            preparer = self.preparer()
            prepared = preparer.prepare(
                object(), self.call("nested/README.md"), self.descriptor
            )
            receipt = R0ReadDispatcher().dispatch(object(), prepared)
            preparer.capture_after(prepared)

        self.assertEqual(self.payload, receipt.payload)
        self.assertTrue(read_sizes)

    def test_in_place_growth_after_prepare_is_bounded_and_rejected(self) -> None:
        preparer = self.preparer()
        prepared = preparer.prepare(
            object(), self.call("nested/README.md"), self.descriptor
        )
        self.target.write_bytes(b"x" * 4096)

        with self.assertRaisesRegex(R0ReadError, "READ_SOURCE_ENTRY_CHANGED"):
            R0ReadDispatcher().dispatch(object(), prepared)

        self.assertFalse(prepared.dispatch_handle.closed)
        preparer.discard(prepared)
        self.assertTrue(prepared.dispatch_handle.closed)

    def test_unselected_paths_are_rejected_before_any_leaf_open(self) -> None:
        for relative, code in (
            (".git/config", "READ_PATH_EXCLUDED"),
            ("missing.txt", "READ_SOURCE_ENTRY_CHANGED"),
        ):
            provider = self.new_capture_provider()
            with self.subTest(relative=relative), mock.patch(
                "pullwise_worker.agent_kernel_r0_read._open_verified",
                side_effect=AssertionError("unselected path was opened"),
            ) as open_verified:
                with self.assertRaisesRegex(R0ReadError, code):
                    self.preparer(capture_provider=provider).prepare(
                        object(), self.call(relative), self.descriptor
                    )
                open_verified.assert_not_called()
                if provider.sessions:
                    self.assertTrue(provider.latest_session.closed)

    def test_source_change_between_snapshot_and_open_is_rejected(self) -> None:
        provider = self.new_capture_provider()
        provider.after_begin = lambda session: self.target.write_bytes(
            b"different"
        )

        with self.assertRaisesRegex(R0ReadError, "READ_SOURCE_ENTRY_CHANGED"):
            self.preparer(capture_provider=provider).prepare(
                object(), self.call("nested/README.md"), self.descriptor
            )
        self.assertTrue(provider.latest_session.closed)

    def test_replacing_path_after_prepare_cannot_redirect_held_descriptor(self) -> None:
        preparer = self.preparer()
        prepared = preparer.prepare(
            object(), self.call("nested/README.md"), self.descriptor
        )
        replacement = self.root / "replacement"
        replacement.write_bytes(b"replacement")
        try:
            self.target.unlink()
            replacement.rename(self.target)
        except OSError as exc:
            preparer.discard(prepared)
            self.skipTest(f"host does not permit replacing an open file: {exc}")

        receipt = R0ReadDispatcher().dispatch(object(), prepared)
        preparer.capture_after(prepared)

        self.assertEqual(self.payload, receipt.payload)

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
            R0ReadDispatcher().dispatch(object(), forged)

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
        self.reservation_plan = object()
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

    def plan_reservation(self, *args: object) -> object:
        return self.reservation_plan

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
