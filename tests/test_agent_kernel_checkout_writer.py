from __future__ import annotations

import errno
import os
from pathlib import Path
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


@unittest.skipUnless(os.name == "posix", "checkout writer is POSIX-only")
class CheckoutWriterCoordinatorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.scratch = tempfile.TemporaryDirectory(prefix="checkout-writer-")
        self.base = Path(self.scratch.name)
        self.control = self.base / "control"
        self.control.mkdir(mode=0o700)

    def tearDown(self) -> None:
        self.scratch.cleanup()

    @staticmethod
    def bounds(
        *,
        seconds: float = 5.0,
        cancellation_requested=lambda: False,
    ) -> CheckoutAcquisitionBounds:
        return CheckoutAcquisitionBounds(
            deadline_monotonic=time.monotonic() + seconds,
            cancellation_requested=cancellation_requested,
        )

    def writer(
        self,
        bounds: CheckoutAcquisitionBounds,
        *,
        control_root: Path | None = None,
        lock_timeout_seconds: int = 30,
    ) -> CheckoutWriterCoordinator:
        return CheckoutWriterCoordinator(
            control_root=control_root or self.control,
            acquisition_bounds=bounds,
            lock_timeout_seconds=lock_timeout_seconds,
        )

    def assert_later_writer_succeeds(self) -> None:
        entered = False
        with self.writer(self.bounds()).writer():
            entered = True
        self.assertTrue(entered)

    def test_cancelled_or_expired_call_never_creates_lock_or_enters_body(
        self,
    ) -> None:
        cases = (
            (
                "cancelled",
                CheckoutAcquisitionBounds(
                    deadline_monotonic=time.monotonic() + 5,
                    cancellation_requested=lambda: True,
                ),
            ),
            (
                "expired",
                CheckoutAcquisitionBounds(
                    deadline_monotonic=time.monotonic() - 1,
                    cancellation_requested=lambda: False,
                ),
            ),
        )

        for name, bounds in cases:
            with self.subTest(name=name):
                case_control = self.base / f"control-{name}"
                case_control.mkdir(mode=0o700)
                entered = False
                with self.assertRaises(SourceStateError):
                    with self.writer(
                        bounds, control_root=case_control
                    ).writer():
                        entered = True
                self.assertFalse(entered)
                self.assertFalse(
                    (case_control / "agent-kernel-checkout.lock").exists()
                )

    def test_writer_can_create_a_checkout_root_and_yields_exact_bounds(
        self,
    ) -> None:
        checkout_root = self.base / "not-materialized-yet"
        bounds = self.bounds()

        with self.writer(bounds).writer() as yielded:
            self.assertIs(bounds, yielded)
            self.assertFalse(checkout_root.exists())
            checkout_root.mkdir(mode=0o700)
            (checkout_root / "README.md").write_text(
                "materialized", encoding="utf-8"
            )

        self.assertEqual(
            "materialized",
            (checkout_root / "README.md").read_text(encoding="utf-8"),
        )

    def test_body_error_wins_over_cancellation_and_lock_is_released(
        self,
    ) -> None:
        cancelled = threading.Event()
        coordinator = self.writer(
            self.bounds(cancellation_requested=cancelled.is_set)
        )

        with self.assertRaisesRegex(RuntimeError, "writer body failed"):
            with coordinator.writer():
                cancelled.set()
                raise RuntimeError("writer body failed")

        self.assert_later_writer_succeeds()

    def test_cancellation_after_successful_body_is_reported_and_cleans_up(
        self,
    ) -> None:
        cancelled = threading.Event()

        with self.assertRaises(SourceStateError):
            with self.writer(
                self.bounds(cancellation_requested=cancelled.is_set)
            ).writer():
                cancelled.set()

        self.assert_later_writer_succeeds()

    def test_capture_and_writer_share_the_control_root_lock_domain(self) -> None:
        checkout_root = self.base / "repository"
        checkout_root.mkdir(mode=0o700)
        git = self.base / "git"
        git.write_bytes(b"test executable")
        git.chmod(0o700)
        policy = SourceSelectionPolicy.pullwise_full_scan(
            root_identity="repository:writer-domain"
        )
        snapshot = SourceTreeSnapshot(
            base_revision=BASE_REVISION,
            selection_policy_digest=policy.digest,
            entries=(),
        )
        capture = CheckoutCaptureCoordinator(
            checkout_root=checkout_root,
            control_root=self.control,
            policy=policy,
            git_executable=git,
            acquisition_bounds=self.bounds(),
        )
        entered = threading.Event()

        def write() -> None:
            with self.writer(self.bounds()).writer():
                entered.set()

        with mock.patch(
            "pullwise_worker.agent_kernel_checkout_window.inspect_gitlinks",
            return_value=object(),
        ), mock.patch(
            "pullwise_worker.agent_kernel_checkout_window.snapshot_source_tree",
            return_value=snapshot,
        ):
            session = capture.begin_capture(base_revision=BASE_REVISION)
            thread = threading.Thread(target=write, daemon=True)
            thread.start()
            self.assertFalse(entered.wait(0.1))
            session.close()

        self.assertTrue(entered.wait(1.0))
        thread.join(timeout=1.0)
        self.assertFalse(thread.is_alive())

    def test_symlink_alias_shares_the_canonical_lock_domain(self) -> None:
        alias = self.base / "control-alias"
        alias.symlink_to(self.control, target_is_directory=True)
        errors: list[BaseException] = []

        def contend() -> None:
            try:
                with self.writer(
                    self.bounds(seconds=0.15), control_root=alias
                ).writer():
                    self.fail("alias writer must not enter")
            except BaseException as exc:
                errors.append(exc)

        with self.writer(self.bounds()).writer():
            thread = threading.Thread(target=contend, daemon=True)
            thread.start()
            thread.join(timeout=1.0)

        self.assertFalse(thread.is_alive())
        self.assertEqual(1, len(errors))
        self.assertIsInstance(errors[0], SourceStateError)

    def test_mutex_wait_is_clamped_by_external_deadline(self) -> None:
        errors: list[BaseException] = []
        entered = threading.Event()

        def contend() -> None:
            try:
                with self.writer(self.bounds(seconds=0.15)).writer():
                    entered.set()
            except BaseException as exc:
                errors.append(exc)

        with self.writer(self.bounds()).writer():
            thread = threading.Thread(target=contend, daemon=True)
            thread.start()
            thread.join(timeout=1.0)

        self.assertFalse(thread.is_alive())
        self.assertFalse(entered.is_set())
        self.assertEqual(1, len(errors))
        self.assertIsInstance(errors[0], SourceStateError)
        self.assert_later_writer_succeeds()

    def test_mutex_wait_polls_cancellation(self) -> None:
        cancelled = threading.Event()
        cancellation_checked = threading.Event()
        errors: list[BaseException] = []
        entered = threading.Event()

        def cancellation_requested() -> bool:
            cancellation_checked.set()
            return cancelled.is_set()

        def contend() -> None:
            try:
                with self.writer(
                    self.bounds(cancellation_requested=cancellation_requested)
                ).writer():
                    entered.set()
            except BaseException as exc:
                errors.append(exc)

        with self.writer(self.bounds()).writer():
            thread = threading.Thread(target=contend, daemon=True)
            thread.start()
            self.assertTrue(cancellation_checked.wait(1.0))
            cancelled.set()
            thread.join(timeout=1.0)

        self.assertFalse(thread.is_alive())
        self.assertFalse(entered.is_set())
        self.assertEqual(1, len(errors))
        self.assertIsInstance(errors[0], SourceStateError)
        self.assert_later_writer_succeeds()

    def test_flock_wait_is_clamped_by_external_deadline(self) -> None:
        import fcntl

        attempts = 0

        def blocked(_descriptor: int, operation: int) -> None:
            nonlocal attempts
            if operation == fcntl.LOCK_EX | fcntl.LOCK_NB:
                attempts += 1
                raise BlockingIOError(errno.EWOULDBLOCK, "busy")

        with mock.patch(
            "pullwise_worker.agent_kernel_checkout_lock._fcntl.flock",
            side_effect=blocked,
        ):
            with self.assertRaises(SourceStateError):
                with self.writer(self.bounds(seconds=0.15)).writer():
                    self.fail("writer body must not run")

        self.assertGreaterEqual(attempts, 1)
        self.assert_later_writer_succeeds()

    def test_flock_wait_polls_cancellation(self) -> None:
        import fcntl

        attempted = threading.Event()
        cancelled = threading.Event()
        errors: list[BaseException] = []

        def blocked(_descriptor: int, operation: int) -> None:
            if operation == fcntl.LOCK_EX | fcntl.LOCK_NB:
                attempted.set()
                raise BlockingIOError(errno.EWOULDBLOCK, "busy")

        def contend() -> None:
            try:
                with self.writer(
                    self.bounds(cancellation_requested=cancelled.is_set)
                ).writer():
                    self.fail("writer body must not run")
            except BaseException as exc:
                errors.append(exc)

        with mock.patch(
            "pullwise_worker.agent_kernel_checkout_lock._fcntl.flock",
            side_effect=blocked,
        ):
            thread = threading.Thread(target=contend, daemon=True)
            thread.start()
            self.assertTrue(attempted.wait(1.0))
            cancelled.set()
            thread.join(timeout=1.0)

        self.assertFalse(thread.is_alive())
        self.assertEqual(1, len(errors))
        self.assertIsInstance(errors[0], SourceStateError)
        self.assert_later_writer_succeeds()

    def test_cancellation_after_flock_acquire_never_returns_lease(self) -> None:
        import fcntl

        cancelled = threading.Event()
        entered = False

        def acquire_then_cancel(_descriptor: int, operation: int) -> None:
            if operation == fcntl.LOCK_EX | fcntl.LOCK_NB:
                cancelled.set()

        with mock.patch(
            "pullwise_worker.agent_kernel_checkout_lock._fcntl.flock",
            side_effect=acquire_then_cancel,
        ):
            with self.assertRaises(SourceStateError):
                with self.writer(
                    self.bounds(cancellation_requested=cancelled.is_set)
                ).writer():
                    entered = True

        self.assertFalse(entered)
        self.assert_later_writer_succeeds()


if __name__ == "__main__":
    unittest.main()
