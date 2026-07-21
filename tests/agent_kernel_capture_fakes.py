from __future__ import annotations

from pathlib import Path
from typing import Callable

from pullwise_worker.agent_kernel_source_state import (
    SourceSelectionPolicy,
    SourceTreeSnapshot,
    snapshot_source_tree,
)


class FakeCaptureSession:
    def __init__(
        self,
        provider: "FakeCaptureProvider",
        source_before: SourceTreeSnapshot,
    ) -> None:
        self.provider = provider
        self.source_before = source_before
        self.closed = False
        self.close_calls = 0

    def capture_after(self) -> SourceTreeSnapshot:
        self.provider.capture_after_calls += 1
        try:
            if self.provider.after_error is not None:
                raise self.provider.after_error
            if self.provider.after_snapshot is not None:
                return self.provider.after_snapshot
            return self.provider.snapshot()
        finally:
            self.close()

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.close_calls += 1


class FakeCaptureProvider:
    """Windows-capable structural fake for the closed capture provider API."""

    def __init__(
        self,
        checkout_root: Path,
        policy: SourceSelectionPolicy,
        *,
        base_revision: str,
    ) -> None:
        self.checkout_root = checkout_root
        self.policy = policy
        self.base_revision = base_revision
        self.before_snapshot: SourceTreeSnapshot | None = None
        self.after_snapshot: SourceTreeSnapshot | None = None
        self.after_error: Exception | None = None
        self.after_begin: Callable[[FakeCaptureSession], None] | None = None
        self.begin_calls = 0
        self.capture_after_calls = 0
        self.sessions: list[FakeCaptureSession] = []

    @property
    def latest_session(self) -> FakeCaptureSession:
        return self.sessions[-1]

    def snapshot(self) -> SourceTreeSnapshot:
        return snapshot_source_tree(
            self.checkout_root,
            policy=self.policy,
            base_revision=self.base_revision,
        )

    def begin_capture(self, *, base_revision: str) -> FakeCaptureSession:
        if base_revision != self.base_revision:
            raise AssertionError("unexpected base revision")
        self.begin_calls += 1
        session = FakeCaptureSession(
            self, self.before_snapshot or self.snapshot()
        )
        self.sessions.append(session)
        try:
            if self.after_begin is not None:
                self.after_begin(session)
            return session
        except BaseException:
            session.close()
            raise
