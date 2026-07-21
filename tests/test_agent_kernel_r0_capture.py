from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from tests.agent_kernel_capture_fakes import FakeCaptureProvider
from pullwise_worker.agent_kernel_checkout_window import (
    CheckoutCaptureCoordinator,
)
from pullwise_worker.agent_kernel_gateway import (
    CheckedInvocation,
    PreparedDispatch,
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


class AgentKernelR0CaptureTest(unittest.TestCase):
    def setUp(self) -> None:
        self.scratch = tempfile.TemporaryDirectory(prefix="agent-kernel-r0-capture-")
        self.root = Path(self.scratch.name) / "repository"
        self.root.mkdir()
        (self.root / "nested").mkdir()
        self.target = self.root / "nested" / "README.md"
        self.payload = b"raw\r\nbytes\x00"
        self.target.write_bytes(self.payload)
        self.policy = SourceSelectionPolicy.pullwise_full_scan(
            root_identity="repository:r0-capture-test"
        )
        self.provider = FakeCaptureProvider(
            self.root, self.policy, base_revision=BASE_REVISION
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

    def call(self) -> CheckedInvocation:
        return CheckedInvocation(
            idempotency_key="idem-" + "1" * 32,
            invocation_digest="2" * 64,
            task_id="task-" + "3" * 32,
            attempt_id="attempt-" + "4" * 32,
            session_id="session-" + "5" * 32,
            owner_epoch=1,
            native_epoch=1,
            tool_key="internal.read_source",
            tool_input=ReadSourceFileInput("nested/README.md"),
        )

    def preparer(self) -> R0ReadPreparer:
        return R0ReadPreparer(
            capture_provider=self.provider,
            base_revision=BASE_REVISION,
            max_bytes=1024,
        )

    def prepare(self) -> tuple[R0ReadPreparer, PreparedDispatch]:
        preparer = self.preparer()
        prepared = preparer.prepare(object(), self.call(), self.descriptor)
        return preparer, prepared

    def test_dispatch_consumes_only_descriptor_until_after_capture(self) -> None:
        preparer, prepared = self.prepare()

        self.assertFalse(hasattr(prepared, "relative_path"))
        self.assertFalse(hasattr(prepared.dispatch_handle, "relative_path"))
        receipt = R0ReadDispatcher().dispatch(object(), prepared)

        self.assertEqual(self.payload, receipt.payload)
        self.assertEqual(hashlib.sha256(self.payload).hexdigest(), receipt.sha256)
        self.assertFalse(prepared.dispatch_handle.closed)
        self.assertFalse(self.provider.latest_session.closed)
        preparer.capture_after(prepared)
        self.assertTrue(prepared.dispatch_handle.closed)

    def test_after_capture_is_fresh_and_releases_session(self) -> None:
        preparer, prepared = self.prepare()
        R0ReadDispatcher().dispatch(object(), prepared)

        after = preparer.capture_after(prepared)

        self.assertTrue(diff_source_trees(prepared.source_before, after).is_empty)
        self.assertEqual(1, self.provider.begin_calls)
        self.assertEqual(1, self.provider.capture_after_calls)
        self.assertTrue(self.provider.latest_session.closed)

    def test_discard_is_idempotent_and_closes_both_resources(self) -> None:
        preparer, prepared = self.prepare()

        preparer.discard(prepared)
        preparer.discard(prepared)

        self.assertTrue(prepared.dispatch_handle.closed)
        self.assertEqual(1, self.provider.latest_session.close_calls)
        with self.assertRaisesRegex(R0ReadError, "PREPARED_READ_CLOSED"):
            R0ReadDispatcher().dispatch(object(), prepared)

    def test_after_capture_before_dispatch_fails_closed(self) -> None:
        preparer, prepared = self.prepare()

        with self.assertRaisesRegex(
            R0ReadError, "PREPARED_READ_NOT_DISPATCHED"
        ):
            preparer.capture_after(prepared)

        self.assertTrue(prepared.dispatch_handle.closed)
        self.assertTrue(self.provider.latest_session.closed)
        self.assertEqual(0, self.provider.capture_after_calls)

    def test_prepare_failure_closes_leaf_and_capture_session(self) -> None:
        with mock.patch(
            "pullwise_worker.agent_kernel_r0_read._assert_entry_matches",
            side_effect=RuntimeError("injected prepare failure"),
        ), self.assertRaisesRegex(RuntimeError, "injected prepare failure"):
            self.preparer().prepare(object(), self.call(), self.descriptor)

        self.assertTrue(self.provider.latest_session.closed)
        self.target.unlink()

    @unittest.skipUnless(os.name == "posix", "POSIX capture provider required")
    def test_real_checkout_coordinator_composes_and_releases_window(self) -> None:
        executable = shutil.which("git")
        if executable is None:
            self.skipTest("Git is unavailable")
        version = subprocess.run(
            [executable, "--version"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        match = re.search(r"(\d+)\.(\d+)", version)
        if match is None or tuple(map(int, match.groups())) < (2, 45):
            self.skipTest("Git 2.45 or newer is required")
        for command in (
            ("init",),
            ("config", "user.name", "Pullwise Test"),
            ("config", "user.email", "pullwise@example.invalid"),
            ("add", "nested/README.md"),
            ("commit", "-m", "capture fixture"),
        ):
            subprocess.run(
                [executable, "-C", os.fspath(self.root), *command],
                check=True,
                capture_output=True,
            )
        revision = subprocess.run(
            [executable, "-C", os.fspath(self.root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        control_root = Path(self.scratch.name) / "control"
        control_root.mkdir(mode=0o700)
        provider = CheckoutCaptureCoordinator(
            checkout_root=self.root,
            control_root=control_root,
            policy=self.policy,
            git_executable=Path(executable).resolve(),
        )
        preparer = R0ReadPreparer(
            capture_provider=provider,
            base_revision=revision,
            max_bytes=1024,
        )

        prepared = preparer.prepare(object(), self.call(), self.descriptor)
        receipt = R0ReadDispatcher().dispatch(object(), prepared)
        after = preparer.capture_after(prepared)

        self.assertEqual(self.payload, receipt.payload)
        self.assertTrue(diff_source_trees(prepared.source_before, after).is_empty)
        with provider.writer():
            pass


if __name__ == "__main__":
    unittest.main()
