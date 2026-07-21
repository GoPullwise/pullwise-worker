"""Closed materialized-source lease ownership for R0 reads."""

from __future__ import annotations

import os
from pathlib import Path
import threading
from typing import Protocol

from .agent_kernel_gateway import GatewayError
from .agent_kernel_source_state import SourceEntry, SourceTreeSnapshot


class R0ReadError(GatewayError):
    pass


class MaterializedSourceCapture(Protocol):
    @property
    def source_before(self) -> SourceTreeSnapshot: ...

    def capture_after(self) -> SourceTreeSnapshot: ...

    def close(self) -> None: ...


class MaterializedSourceCaptureProvider(Protocol):
    @property
    def checkout_root(self) -> Path: ...

    def begin_capture(
        self, *, base_revision: str
    ) -> MaterializedSourceCapture: ...


class PreparedR0ReadHandle:
    """Owns both the leaf descriptor and the complete capture lease."""

    __slots__ = (
        "_capture",
        "_descriptor",
        "_expected_sha256",
        "_expected_size",
        "_lock",
    )

    def __init__(
        self,
        descriptor: int,
        entry: SourceEntry,
        capture: MaterializedSourceCapture,
    ) -> None:
        self._capture: MaterializedSourceCapture | None = capture
        self._descriptor: int | None = descriptor
        self._expected_sha256 = entry.sha256
        self._expected_size = entry.size_bytes
        self._lock = threading.Lock()

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._descriptor is None and self._capture is None

    def take(self) -> tuple[int, str, int]:
        with self._lock:
            if self._descriptor is None or self._capture is None:
                raise R0ReadError("PREPARED_READ_CLOSED")
            descriptor = self._descriptor
            self._descriptor = None
        assert self._expected_sha256 is not None
        assert self._expected_size is not None
        return descriptor, self._expected_sha256, self._expected_size

    def capture_after(self) -> SourceTreeSnapshot:
        with self._lock:
            capture = self._capture
            descriptor = self._descriptor
            self._capture = None
            self._descriptor = None
        if capture is None:
            raise R0ReadError("PREPARED_READ_CLOSED")
        try:
            if descriptor is not None:
                os.close(descriptor)
            return capture.capture_after()
        finally:
            capture.close()

    def discard(self) -> None:
        with self._lock:
            descriptor = self._descriptor
            capture = self._capture
            self._descriptor = None
            self._capture = None
        try:
            if descriptor is not None:
                os.close(descriptor)
        finally:
            if capture is not None:
                capture.close()


__all__ = [
    "MaterializedSourceCapture",
    "MaterializedSourceCaptureProvider",
    "R0ReadError",
]
