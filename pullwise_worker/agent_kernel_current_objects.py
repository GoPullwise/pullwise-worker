"""Private no-clobber CAS for current-only tool payloads."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import math
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import stat
import time


class CurrentObjectError(RuntimeError):
    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


@dataclass(frozen=True)
class PublishedCurrentObject:
    sha256: str
    size_bytes: int
    relative_path: str


@dataclass(frozen=True)
class PublishedCurrentReference:
    object: PublishedCurrentObject
    content_ref: dict[str, object]
    content_ref_bytes: bytes

    @property
    def sha256(self) -> str:
        return self.object.sha256

    @property
    def size_bytes(self) -> int:
        return self.object.size_bytes


@dataclass(frozen=True)
class PublishedCurrentPayload:
    payload: PublishedCurrentReference
    source: PublishedCurrentReference

    @property
    def object(self) -> PublishedCurrentObject:
        return self.payload.object

    @property
    def content_ref(self) -> dict[str, object]:
        return self.payload.content_ref

    @property
    def content_ref_bytes(self) -> bytes:
        return self.payload.content_ref_bytes


class CurrentObjectStore:
    def __init__(self, root: Path) -> None:
        if not isinstance(root, Path):
            raise CurrentObjectError("CURRENT_OBJECT_ROOT_INVALID")
        if root.exists() and not self._safe_directory(root):
            raise CurrentObjectError("CURRENT_OBJECT_ROOT_INVALID")
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(root, 0o700)
        self.root = root
        self.objects = root / "objects"
        self.staging = root / "staging"
        self._ensure_directory(self.objects)
        self._ensure_directory(self.staging)

    def publish(self, payload: bytes) -> PublishedCurrentObject:
        if not isinstance(payload, bytes):
            raise CurrentObjectError("CURRENT_OBJECT_BYTES_INVALID")
        self._ensure_directory(self.objects)
        self._ensure_directory(self.staging)
        digest = hashlib.sha256(payload).hexdigest()
        relative = f"objects/{digest[:2]}/{digest}"
        published = PublishedCurrentObject(digest, len(payload), relative)
        parent = self.root / "objects" / digest[:2]
        self._ensure_directory(parent)
        target = self.path_for(published)
        staging = self.staging / f"{digest}.{secrets.token_hex(16)}.tmp"
        descriptor: int | None = None
        try:
            flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
            flags |= getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(staging, flags, 0o600)
            view = memoryview(payload)
            while view:
                written = os.write(descriptor, view)
                if written < 1:
                    raise CurrentObjectError("CURRENT_OBJECT_WRITE_FAILED")
                view = view[written:]
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None
            try:
                os.link(staging, target, follow_symlinks=False)
                os.chmod(target, 0o600)
                self._fsync_directory(parent)
            except FileExistsError:
                pass
            staging.unlink()
            self._fsync_directory(self.staging)
            return self._verify(published, return_bytes=False)
        except CurrentObjectError:
            raise
        except OSError as exc:
            raise CurrentObjectError("CURRENT_OBJECT_PUBLISH_FAILED", str(exc)) from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)
            try:
                staging.unlink(missing_ok=True)
            except OSError:
                pass

    def read_verified(self, published: PublishedCurrentObject) -> bytes:
        return self._verify(published, return_bytes=True)

    def collect_orphans(
        self,
        reachable_sha256: set[str] | frozenset[str],
        *,
        min_age_seconds: int,
        now: float | None = None,
    ) -> tuple[str, ...]:
        if (
            not isinstance(reachable_sha256, (set, frozenset))
            or any(
                not isinstance(item, str)
                or re.fullmatch(r"[0-9a-f]{64}", item) is None
                for item in reachable_sha256
            )
            or isinstance(min_age_seconds, bool)
            or not isinstance(min_age_seconds, int)
            or min_age_seconds < 1
        ):
            raise CurrentObjectError("CURRENT_OBJECT_COLLECTION_INVALID")
        selected_now = time.time() if now is None else now
        if (
            isinstance(selected_now, bool)
            or not isinstance(selected_now, (int, float))
            or not math.isfinite(selected_now)
        ):
            raise CurrentObjectError("CURRENT_OBJECT_COLLECTION_INVALID")
        cutoff = float(selected_now) - min_age_seconds
        self._ensure_directory(self.objects)
        removed: list[str] = []
        for prefix in sorted(self.objects.iterdir(), key=lambda item: item.name):
            if re.fullmatch(r"[0-9a-f]{2}", prefix.name) is None:
                raise CurrentObjectError("CURRENT_OBJECT_LAYOUT_INVALID")
            if not self._safe_directory(prefix):
                raise CurrentObjectError("CURRENT_OBJECT_UNSAFE")
            for path in sorted(prefix.iterdir(), key=lambda item: item.name):
                digest = path.name
                try:
                    info = path.lstat()
                except OSError as exc:
                    raise CurrentObjectError("CURRENT_OBJECT_UNSAFE") from exc
                if (
                    re.fullmatch(r"[0-9a-f]{64}", digest) is None
                    or digest[:2] != prefix.name
                    or not stat.S_ISREG(info.st_mode)
                    or stat.S_ISLNK(info.st_mode)
                    or info.st_nlink != 1
                    or self._is_reparse(info)
                ):
                    raise CurrentObjectError("CURRENT_OBJECT_UNSAFE")
                if digest in reachable_sha256 or info.st_mtime > cutoff:
                    continue
                published = PublishedCurrentObject(
                    digest,
                    info.st_size,
                    f"objects/{prefix.name}/{digest}",
                )
                self._verify(published, return_bytes=False)
                current = path.lstat()
                if self._stable_file_identity(info) != self._stable_file_identity(
                    current
                ):
                    raise CurrentObjectError("CURRENT_OBJECT_UNSAFE")
                path.unlink()
                self._fsync_directory(prefix)
                removed.append(published.relative_path)
        return tuple(removed)

    def path_for(self, published: PublishedCurrentObject) -> Path:
        if not isinstance(published, PublishedCurrentObject):
            raise CurrentObjectError("CURRENT_OBJECT_IDENTITY_INVALID")
        if (
            re.fullmatch(r"[0-9a-f]{64}", published.sha256) is None
            or isinstance(published.size_bytes, bool)
            or not isinstance(published.size_bytes, int)
            or published.size_bytes < 0
        ):
            raise CurrentObjectError("CURRENT_OBJECT_IDENTITY_INVALID")
        expected = f"objects/{published.sha256[:2]}/{published.sha256}"
        if (
            published.relative_path != expected
            or PurePosixPath(expected).as_posix() != expected
        ):
            raise CurrentObjectError("CURRENT_OBJECT_IDENTITY_INVALID")
        return self.root.joinpath(*PurePosixPath(expected).parts)

    def _verify(
        self, published: PublishedCurrentObject, *, return_bytes: bool
    ) -> PublishedCurrentObject | bytes:
        path = self.path_for(published)
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            path_before = path.lstat()
            if (
                not stat.S_ISREG(path_before.st_mode)
                or stat.S_ISLNK(path_before.st_mode)
                or path_before.st_nlink != 1
                or self._is_reparse(path_before)
            ):
                raise CurrentObjectError("CURRENT_OBJECT_UNSAFE")
            descriptor = os.open(path, flags)
        except OSError as exc:
            raise CurrentObjectError("CURRENT_OBJECT_UNSAFE", str(exc)) from exc
        try:
            before = os.fstat(descriptor)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_nlink != 1
                or self._is_reparse(before)
                or self._stable_file_identity(path_before)
                != self._stable_file_identity(before)
            ):
                raise CurrentObjectError("CURRENT_OBJECT_UNSAFE")
            chunks: list[bytes] = []
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
            payload = b"".join(chunks)
            after = os.fstat(descriptor)
            path_after = path.lstat()
            if (
                self._stable_file_identity(before)
                != self._stable_file_identity(after)
                or self._stable_file_identity(path_before)
                != self._stable_file_identity(path_after)
            ):
                raise CurrentObjectError("CURRENT_OBJECT_UNSAFE")
            if (
                len(payload) != published.size_bytes
                or hashlib.sha256(payload).hexdigest() != published.sha256
            ):
                raise CurrentObjectError("CURRENT_OBJECT_CORRUPT")
            return payload if return_bytes else published
        finally:
            os.close(descriptor)

    def _ensure_directory(self, path: Path) -> None:
        path.mkdir(mode=0o700, exist_ok=True)
        if not self._safe_directory(path):
            raise CurrentObjectError("CURRENT_OBJECT_ROOT_INVALID")
        os.chmod(path, 0o700)

    @classmethod
    def _safe_directory(cls, path: Path) -> bool:
        try:
            info = path.lstat()
        except OSError:
            return False
        return stat.S_ISDIR(info.st_mode) and not cls._is_reparse(info)

    @staticmethod
    def _is_reparse(info: os.stat_result) -> bool:
        attributes = getattr(info, "st_file_attributes", 0)
        marker = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        return bool(attributes & marker)

    @staticmethod
    def _stable_file_identity(info: os.stat_result) -> tuple[int, ...]:
        return (
            info.st_dev,
            info.st_ino,
            info.st_mode,
            info.st_nlink,
            info.st_size,
            info.st_mtime_ns,
            info.st_ctime_ns,
        )

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        if os.name != "posix":
            return
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


__all__ = [
    "CurrentObjectError",
    "CurrentObjectStore",
    "PublishedCurrentObject",
    "PublishedCurrentPayload",
    "PublishedCurrentReference",
]
