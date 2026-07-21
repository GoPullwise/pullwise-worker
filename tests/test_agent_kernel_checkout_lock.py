from __future__ import annotations

import errno
import os
from pathlib import Path
import stat
import tempfile
import threading
import time
import unittest
from unittest import mock

from pullwise_worker.agent_kernel_checkout_lifecycle import (
    CheckoutAcquisitionBounds,
)
from pullwise_worker.agent_kernel_checkout_window import (
    CheckoutCaptureCoordinator,
)
from pullwise_worker.agent_kernel_checkout_writer import (
    CheckoutWriterCoordinator,
)
from pullwise_worker.agent_kernel_source_state import (
    SourceSelectionPolicy,
    SourceStateError,
    SourceTreeSnapshot,
)


BASE_REVISION = "a" * 40


@unittest.skipUnless(os.name == "posix", "checkout lock is POSIX-only")
class AgentKernelCheckoutLockTest(unittest.TestCase):
    def setUp(self) -> None:
        self.scratch = tempfile.TemporaryDirectory(prefix="checkout-lock-")
        self.base = Path(self.scratch.name)
        self.root = self.base / "repository"
        self.control = self.base / "control"
        self.root.mkdir(mode=0o700)
        self.control.mkdir(mode=0o700)
        self.git = self.base / "git"
        self.git.write_bytes(b"test executable")
        self.git.chmod(0o700)
        self.policy = SourceSelectionPolicy.pullwise_full_scan(
            root_identity="repository:checkout-lock"
        )
        self.snapshot = SourceTreeSnapshot(
            base_revision=BASE_REVISION,
            selection_policy_digest=self.policy.digest,
            entries=(),
        )

    def tearDown(self) -> None:
        self.scratch.cleanup()

    def coordinator(
        self,
        *,
        acquisition_bounds: CheckoutAcquisitionBounds | None = None,
        lock_timeout_seconds: int = 30,
    ) -> CheckoutCaptureCoordinator:
        return CheckoutCaptureCoordinator(
            checkout_root=self.root,
            control_root=self.control,
            policy=self.policy,
            git_executable=self.git,
            acquisition_bounds=acquisition_bounds or self.bounds(),
            lock_timeout_seconds=lock_timeout_seconds,
        )

    @staticmethod
    def bounds() -> CheckoutAcquisitionBounds:
        return CheckoutAcquisitionBounds(
            deadline_monotonic=time.monotonic() + 30,
            cancellation_requested=lambda: False,
        )

    def writer(
        self, *, lock_timeout_seconds: int = 30
    ) -> CheckoutWriterCoordinator:
        return CheckoutWriterCoordinator(
            control_root=self.control,
            acquisition_bounds=self.bounds(),
            lock_timeout_seconds=lock_timeout_seconds,
        )

    def test_lock_file_is_regular_private_not_followed_and_flocked(self) -> None:
        with mock.patch(
            "pullwise_worker.agent_kernel_checkout_window.inspect_gitlinks",
            return_value=object(),
        ), mock.patch(
            "pullwise_worker.agent_kernel_checkout_window.snapshot_source_tree",
            return_value=self.snapshot,
        ):
            session = self.coordinator().begin_capture(
                base_revision=BASE_REVISION
            )

        lock_path = self.control / "agent-kernel-checkout.lock"
        metadata = lock_path.lstat()
        self.assertTrue(stat.S_ISREG(metadata.st_mode))
        self.assertEqual(0o600, stat.S_IMODE(metadata.st_mode))
        self.assertEqual(1, metadata.st_nlink)
        import fcntl

        competing_descriptor = os.open(lock_path, os.O_RDWR)
        try:
            with self.assertRaises(BlockingIOError):
                fcntl.flock(
                    competing_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB
                )
        finally:
            os.close(competing_descriptor)
        session.close()

        target = self.base / "outside-lock"
        target.write_text("do not touch", encoding="utf-8")
        lock_path.unlink()
        lock_path.symlink_to(target)
        with self.assertRaisesRegex(
            SourceStateError, "CHECKOUT_LOCK_INVALID"
        ):
            self.coordinator().begin_capture(base_revision=BASE_REVISION)
        self.assertEqual("do not touch", target.read_text(encoding="utf-8"))

    def test_lock_metadata_drift_fails_before_after_inspection(self) -> None:
        with mock.patch(
            "pullwise_worker.agent_kernel_checkout_window.inspect_gitlinks",
            return_value=object(),
        ) as inspect, mock.patch(
            "pullwise_worker.agent_kernel_checkout_window.snapshot_source_tree",
            return_value=self.snapshot,
        ):
            session = self.coordinator().begin_capture(
                base_revision=BASE_REVISION
            )
            lock_path = self.control / "agent-kernel-checkout.lock"
            lock_path.chmod(0o644)
            try:
                with self.assertRaisesRegex(
                    SourceStateError, "CHECKOUT_LOCK_INVALID"
                ):
                    session.capture_after()
            finally:
                lock_path.chmod(0o600)

        self.assertEqual(1, inspect.call_count)
        self.assertTrue(session.closed)
        with self.writer().writer():
            pass

    def test_process_mutex_contention_uses_one_bounded_deadline(self) -> None:
        errors: list[BaseException] = []
        entered = threading.Event()
        first = self.coordinator()
        second = self.writer(lock_timeout_seconds=1)

        def contend() -> None:
            try:
                with second.writer():
                    entered.set()
            except BaseException as exc:
                errors.append(exc)

        with mock.patch(
            "pullwise_worker.agent_kernel_checkout_window.inspect_gitlinks",
            return_value=object(),
        ), mock.patch(
            "pullwise_worker.agent_kernel_checkout_window.snapshot_source_tree",
            return_value=self.snapshot,
        ):
            session = first.begin_capture(base_revision=BASE_REVISION)
            thread = threading.Thread(target=contend, daemon=True)
            thread.start()
            thread.join(timeout=2.0)
            session.close()

        self.assertFalse(thread.is_alive())
        self.assertFalse(entered.is_set())
        self.assertEqual(1, len(errors))
        self.assertIsInstance(errors[0], SourceStateError)
        self.assertEqual("CHECKOUT_LOCK_TIMEOUT", errors[0].code)

    def test_one_effective_deadline_is_shared_across_acquisition(self) -> None:
        class RecordingBounds(CheckoutAcquisitionBounds):
            def __init__(self) -> None:
                super().__init__(
                    deadline_monotonic=time.monotonic() + 30,
                    cancellation_requested=lambda: False,
                )
                self.effective_calls = 0
                self.seen_deadlines: list[float] = []

            def _effective_deadline(self, timeout: int | float) -> float:
                self.effective_calls += 1
                return super()._effective_deadline(timeout)

            def _checkpoint(self, deadline: float) -> float:
                self.seen_deadlines.append(deadline)
                return super()._checkpoint(deadline)

            def _wait_seconds(self, deadline: float) -> float:
                self.seen_deadlines.append(deadline)
                return super()._wait_seconds(deadline)

        bounds = RecordingBounds()
        writer = CheckoutWriterCoordinator(
            control_root=self.control,
            acquisition_bounds=bounds,
        )

        with writer.writer():
            pass

        self.assertEqual(1, bounds.effective_calls)
        self.assertGreater(len(bounds.seen_deadlines), 1)
        self.assertEqual(1, len(set(bounds.seen_deadlines)))

    def test_flock_contention_uses_the_same_bounded_deadline(self) -> None:
        with mock.patch(
            "pullwise_worker.agent_kernel_checkout_lock._fcntl.flock",
            side_effect=BlockingIOError(errno.EWOULDBLOCK, "busy"),
        ) as flock:
            with self.assertRaisesRegex(
                SourceStateError, "CHECKOUT_LOCK_TIMEOUT"
            ):
                with self.writer(lock_timeout_seconds=1).writer():
                    pass

        import fcntl

        self.assertEqual(
            fcntl.LOCK_EX | fcntl.LOCK_NB, flock.call_args.args[1]
        )
        with self.writer().writer():
            pass

    def test_writer_detects_control_replacement_after_body(self) -> None:
        original = self.base / "original-control"
        replacement = self.base / "replacement-control"
        replacement.mkdir(mode=0o700)
        with self.assertRaisesRegex(
            SourceStateError, "CHECKOUT_CONTROL_ROOT_CHANGED"
        ):
            with self.writer().writer():
                self.control.rename(original)
                replacement.rename(self.control)

    def test_writer_preserves_body_error_when_control_also_changes(self) -> None:
        original = self.base / "original-control"
        replacement = self.base / "replacement-control"
        replacement.mkdir(mode=0o700)
        with self.assertRaisesRegex(RuntimeError, "writer body failed"):
            with self.writer().writer():
                self.control.rename(original)
                replacement.rename(self.control)
                raise RuntimeError("writer body failed")

    def test_acquire_cleanup_preserves_primary_and_releases_mutex(self) -> None:
        import fcntl

        original_flock = fcntl.flock

        def fail_unlock(descriptor: int, operation: int) -> None:
            if operation == fcntl.LOCK_UN:
                raise OSError("unlock failed")
            original_flock(descriptor, operation)

        with mock.patch(
            "pullwise_worker.agent_kernel_checkout_lock."
            "_HeldPosixLock.assert_current",
            side_effect=RuntimeError("acquire integrity failed"),
        ), mock.patch(
            "pullwise_worker.agent_kernel_checkout_lock._fcntl.flock",
            side_effect=fail_unlock,
        ):
            with self.assertRaisesRegex(
                RuntimeError, "acquire integrity failed"
            ):
                with self.writer(lock_timeout_seconds=1).writer():
                    self.fail("writer body must not run")

        with self.writer(lock_timeout_seconds=1).writer():
            pass

    def test_post_flock_integrity_wins_over_simultaneous_cancel(self) -> None:
        import fcntl

        cancelled = threading.Event()
        original = self.base / "original-control"
        replacement = self.base / "replacement-control"
        replacement.mkdir(mode=0o700)
        original_flock = fcntl.flock

        def acquire_then_drift(descriptor: int, operation: int) -> None:
            original_flock(descriptor, operation)
            if operation == fcntl.LOCK_EX | fcntl.LOCK_NB:
                self.control.rename(original)
                replacement.rename(self.control)
                cancelled.set()

        bounds = CheckoutAcquisitionBounds(
            deadline_monotonic=time.monotonic() + 5,
            cancellation_requested=cancelled.is_set,
        )
        writer = CheckoutWriterCoordinator(
            control_root=self.control,
            acquisition_bounds=bounds,
        )

        with mock.patch(
            "pullwise_worker.agent_kernel_checkout_lock._fcntl.flock",
            side_effect=acquire_then_drift,
        ):
            with self.assertRaisesRegex(
                SourceStateError, "CHECKOUT_CONTROL_ROOT_CHANGED"
            ):
                with writer.writer():
                    self.fail("writer body must not run")


if __name__ == "__main__":
    unittest.main()
