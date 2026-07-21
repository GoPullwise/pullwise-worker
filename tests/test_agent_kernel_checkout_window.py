from __future__ import annotations

import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from pullwise_worker.agent_kernel_checkout_window import (
    CheckoutCaptureCoordinator,
)
from pullwise_worker.agent_kernel_source_state import (
    SourceSelectionPolicy,
    SourceStateError,
    SourceTreeSnapshot,
)


BASE_REVISION = "a" * 40


class AgentKernelCheckoutWindowPlatformTest(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows-only fail-closed check")
    def test_windows_explicitly_rejects_the_production_coordinator(self) -> None:
        with tempfile.TemporaryDirectory(prefix="checkout-window-") as scratch:
            root = Path(scratch)
            with self.assertRaisesRegex(
                SourceStateError, "CHECKOUT_CAPTURE_POSIX_REQUIRED"
            ):
                CheckoutCaptureCoordinator(
                    checkout_root=root,
                    control_root=root,
                    policy=SourceSelectionPolicy.pullwise_full_scan(
                        root_identity="repository:windows-dev-only"
                    ),
                    git_executable=root / "git",
                )


@unittest.skipUnless(os.name == "posix", "production coordinator is POSIX-only")
class AgentKernelCheckoutWindowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.scratch = tempfile.TemporaryDirectory(prefix="checkout-window-")
        self.base = Path(self.scratch.name)
        self.root = self.base / "repository"
        self.control = self.base / "control"
        self.root.mkdir(mode=0o700)
        self.control.mkdir(mode=0o700)
        (self.root / "README.md").write_text("before", encoding="utf-8")
        self.policy = SourceSelectionPolicy.pullwise_full_scan(
            root_identity="repository:checkout-window"
        )
        self.git = self.base / "git"
        self.git.write_bytes(b"test executable")
        self.git.chmod(0o700)
        self.before = SourceTreeSnapshot(
            base_revision=BASE_REVISION,
            selection_policy_digest=self.policy.digest,
            entries=(),
        )
        self.after = SourceTreeSnapshot(
            base_revision=BASE_REVISION,
            selection_policy_digest=self.policy.digest,
            entries=(),
        )

    def tearDown(self) -> None:
        self.scratch.cleanup()

    def coordinator(self) -> CheckoutCaptureCoordinator:
        return CheckoutCaptureCoordinator(
            checkout_root=self.root,
            control_root=self.control,
            policy=self.policy,
            git_executable=self.git,
        )

    def test_checkout_root_is_the_validated_read_only_absolute_root(self) -> None:
        coordinator = self.coordinator()

        self.assertEqual(self.root, coordinator.checkout_root)
        self.assertTrue(coordinator.checkout_root.is_absolute())
        with self.assertRaises(AttributeError):
            coordinator.checkout_root = self.base / "other"

        alias = self.base / "repository-alias"
        alias.symlink_to(self.root, target_is_directory=True)
        aliased = CheckoutCaptureCoordinator(
            checkout_root=alias,
            control_root=self.control,
            policy=self.policy,
            git_executable=self.git,
        )
        self.assertEqual(self.root.resolve(strict=True), aliased.checkout_root)

    def test_checkout_and_control_roots_must_be_disjoint(self) -> None:
        with self.assertRaisesRegex(
            SourceStateError, "CHECKOUT_ROOTS_NOT_DISJOINT"
        ):
            CheckoutCaptureCoordinator(
                checkout_root=self.root,
                control_root=self.root,
                policy=self.policy,
                git_executable=self.git,
            )

        nested_control = self.root / "control"
        nested_control.mkdir()
        with self.assertRaisesRegex(
            SourceStateError, "CHECKOUT_ROOTS_NOT_DISJOINT"
        ):
            CheckoutCaptureCoordinator(
                checkout_root=self.root,
                control_root=nested_control,
                policy=self.policy,
                git_executable=self.git,
            )

        aliased_control_target = self.root / "aliased-control-target"
        aliased_control_target.mkdir()
        control_alias = self.base / "control-alias"
        control_alias.symlink_to(
            aliased_control_target, target_is_directory=True
        )
        with self.assertRaisesRegex(
            SourceStateError, "CHECKOUT_ROOTS_NOT_DISJOINT"
        ):
            CheckoutCaptureCoordinator(
                checkout_root=self.root,
                control_root=control_alias,
                policy=self.policy,
                git_executable=self.git,
            )

        nested_checkout = self.control / "repository"
        nested_checkout.mkdir()
        with self.assertRaisesRegex(
            SourceStateError, "CHECKOUT_ROOTS_NOT_DISJOINT"
        ):
            CheckoutCaptureCoordinator(
                checkout_root=nested_checkout,
                control_root=self.control,
                policy=self.policy,
                git_executable=self.git,
            )

    def test_session_keeps_catalog_private_and_reinspects_for_after(self) -> None:
        first_catalog = object()
        second_catalog = object()
        events: list[str] = []

        def inspect(*args: object, **kwargs: object) -> object:
            events.append("inspect")
            return first_catalog if len(events) == 1 else second_catalog

        def snapshot(*args: object, **kwargs: object) -> SourceTreeSnapshot:
            events.append("snapshot")
            expected = first_catalog if len(events) == 2 else second_catalog
            self.assertIs(expected, kwargs["gitlink_catalog"])
            return self.before if len(events) == 2 else self.after

        with mock.patch(
            "pullwise_worker.agent_kernel_checkout_window.inspect_gitlinks",
            side_effect=inspect,
        ) as inspect_call, mock.patch(
            "pullwise_worker.agent_kernel_checkout_window.snapshot_source_tree",
            side_effect=snapshot,
        ):
            session = self.coordinator().begin_capture(
                base_revision=BASE_REVISION
            )
            self.assertIs(self.before, session.source_before)
            self.assertFalse(hasattr(session, "catalog"))
            self.assertFalse(hasattr(session, "gitlink_catalog"))

            captured = session.capture_after()

        self.assertIs(self.after, captured)
        self.assertTrue(session.closed)
        self.assertEqual(
            ["inspect", "snapshot", "inspect", "snapshot"], events
        )
        self.assertTrue(
            all(
                call.kwargs["base_revision"] == BASE_REVISION
                for call in inspect_call.call_args_list
            )
        )

    def test_capture_session_blocks_a_cooperating_writer_until_after(self) -> None:
        entered = threading.Event()
        finished = threading.Event()
        first = self.coordinator()
        second = self.coordinator()

        def write() -> None:
            with second.writer():
                entered.set()
            finished.set()

        with mock.patch(
            "pullwise_worker.agent_kernel_checkout_window.inspect_gitlinks",
            side_effect=(object(), object()),
        ), mock.patch(
            "pullwise_worker.agent_kernel_checkout_window.snapshot_source_tree",
            side_effect=(self.before, self.after),
        ):
            session = first.begin_capture(base_revision=BASE_REVISION)
            thread = threading.Thread(target=write, daemon=True)
            thread.start()
            self.assertFalse(entered.wait(0.1))

            self.assertIs(self.after, session.capture_after())
            self.assertTrue(entered.wait(1.0))
            self.assertTrue(finished.wait(1.0))
            thread.join(timeout=1.0)

    def test_checkout_identity_change_fails_and_releases_window(self) -> None:
        original = self.base / "original"
        replacement = self.base / "replacement"
        replacement.mkdir()

        def replace_root(*args: object, **kwargs: object) -> object:
            self.root.rename(original)
            replacement.rename(self.root)
            return object()

        with mock.patch(
            "pullwise_worker.agent_kernel_checkout_window.inspect_gitlinks",
            side_effect=replace_root,
        ), mock.patch(
            "pullwise_worker.agent_kernel_checkout_window.snapshot_source_tree"
        ) as snapshot:
            with self.assertRaisesRegex(
                SourceStateError, "CHECKOUT_IDENTITY_CHANGED"
            ):
                self.coordinator().begin_capture(base_revision=BASE_REVISION)
            snapshot.assert_not_called()

        entered = threading.Event()

        def write() -> None:
            with self.coordinator().writer():
                entered.set()

        thread = threading.Thread(target=write, daemon=True)
        thread.start()
        self.assertTrue(entered.wait(1.0))
        thread.join(timeout=1.0)

    def test_after_failure_closes_session_and_releases_window(self) -> None:
        with mock.patch(
            "pullwise_worker.agent_kernel_checkout_window.inspect_gitlinks",
            side_effect=(object(), RuntimeError("after inspection failed")),
        ), mock.patch(
            "pullwise_worker.agent_kernel_checkout_window.snapshot_source_tree",
            return_value=self.before,
        ):
            session = self.coordinator().begin_capture(
                base_revision=BASE_REVISION
            )
            with self.assertRaisesRegex(RuntimeError, "after inspection failed"):
                session.capture_after()

        self.assertTrue(session.closed)
        with self.coordinator().writer():
            pass

    def test_checkout_replacement_before_after_capture_is_rejected(self) -> None:
        with mock.patch(
            "pullwise_worker.agent_kernel_checkout_window.inspect_gitlinks",
            return_value=object(),
        ), mock.patch(
            "pullwise_worker.agent_kernel_checkout_window.snapshot_source_tree",
            return_value=self.before,
        ) as snapshot:
            session = self.coordinator().begin_capture(
                base_revision=BASE_REVISION
            )
            original = self.base / "original"
            replacement = self.base / "replacement"
            replacement.mkdir()
            self.root.rename(original)
            replacement.rename(self.root)

            with self.assertRaisesRegex(
                SourceStateError, "CHECKOUT_IDENTITY_CHANGED"
            ):
                session.capture_after()

        self.assertEqual(1, snapshot.call_count)
        self.assertTrue(session.closed)
        with self.coordinator().writer():
            pass

    def test_context_and_close_are_exception_safe_and_idempotent(self) -> None:
        with mock.patch(
            "pullwise_worker.agent_kernel_checkout_window.inspect_gitlinks",
            return_value=object(),
        ), mock.patch(
            "pullwise_worker.agent_kernel_checkout_window.snapshot_source_tree",
            return_value=self.before,
        ):
            with self.assertRaisesRegex(RuntimeError, "caller failed"):
                with self.coordinator().begin_capture(
                    base_revision=BASE_REVISION
                ) as session:
                    raise RuntimeError("caller failed")

        session.close()
        self.assertTrue(session.closed)
        with self.assertRaisesRegex(
            SourceStateError, "CHECKOUT_CAPTURE_SESSION_CLOSED"
        ):
            session.capture_after()

    def test_invalid_revision_is_rejected_before_inspection(self) -> None:
        with mock.patch(
            "pullwise_worker.agent_kernel_checkout_window.inspect_gitlinks"
        ) as inspect:
            with self.assertRaisesRegex(
                SourceStateError, "SOURCE_BASE_REVISION_INVALID"
            ):
                self.coordinator().begin_capture(base_revision="HEAD")
            inspect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
