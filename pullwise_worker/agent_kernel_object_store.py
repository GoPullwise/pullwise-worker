"""Durable content-addressed object storage for the Agent Kernel."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import re
import stat
import tempfile
import time
from typing import Callable, Iterable

from .agent_kernel_database import AgentKernelDatabase, AgentKernelStorageError


DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")
TASK_ID_PATTERN = re.compile(r"^task_[0-9a-f]{32}$")
ARTIFACT_ID_PATTERN = re.compile(r"^art_[0-9a-f]{32}$")
TEMPORARY_PATTERN = re.compile(r"^object-[a-z0-9_]+\.tmp$")
MAX_SAFE_INTEGER = 2**53 - 1


class CasCorruptError(AgentKernelStorageError):
    pass


class ContentRefConflictError(AgentKernelStorageError):
    pass


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _private_directory(path: Path) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        try:
            path.mkdir(parents=True, mode=0o700)
        except FileExistsError:
            pass
        metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise AgentKernelStorageError(f"storage_directory_invalid: {path}")
    path.chmod(0o700)


class ObjectStore:
    def __init__(
        self,
        database: AgentKernelDatabase,
        *,
        stage_hook: Callable[[str], None] | None = None,
    ) -> None:
        self.database = database
        self.root = database.root
        self.objects_root = self.root / "objects" / "sha256"
        self.tmp_root = self.root / "tmp"
        self.stage_hook = stage_hook
        _private_directory(self.objects_root)
        _private_directory(self.tmp_root)

    @staticmethod
    def digest_bytes(payload: bytes) -> str:
        return hashlib.sha256(payload).hexdigest()

    def path_for_digest(self, digest: str) -> Path:
        if not DIGEST_PATTERN.fullmatch(digest):
            raise AgentKernelStorageError("content_digest_invalid")
        return self.objects_root / digest[:2] / digest

    def put_bytes(
        self,
        payload: bytes,
        *,
        task_id: str,
        artifact_id: str,
        media_type: str,
        content_schema_id: str,
        encoding: str,
        max_bytes: int = MAX_SAFE_INTEGER,
    ) -> dict[str, object]:
        if not isinstance(payload, bytes):
            raise AgentKernelStorageError("content_bytes_required")
        return self.put_stream(
            (payload,),
            task_id=task_id,
            artifact_id=artifact_id,
            media_type=media_type,
            content_schema_id=content_schema_id,
            encoding=encoding,
            max_bytes=max_bytes,
        )

    def put_stream(
        self,
        chunks: Iterable[bytes],
        *,
        task_id: str,
        artifact_id: str,
        media_type: str,
        content_schema_id: str,
        encoding: str,
        max_bytes: int,
    ) -> dict[str, object]:
        self._validate_metadata(
            task_id,
            artifact_id,
            media_type,
            content_schema_id,
            encoding,
            max_bytes,
        )
        descriptor, temporary_name = tempfile.mkstemp(
            prefix="object-", suffix=".tmp", dir=self.tmp_root
        )
        temporary = Path(temporary_name)
        digest = hashlib.sha256()
        size = 0
        try:
            with os.fdopen(descriptor, "wb", closefd=True) as handle:
                os.fchmod(handle.fileno(), 0o600)
                for chunk in chunks:
                    if not isinstance(chunk, bytes):
                        raise AgentKernelStorageError("content_chunk_not_bytes")
                    size += len(chunk)
                    if size > max_bytes:
                        raise AgentKernelStorageError("content_size_limit_exceeded")
                    handle.write(chunk)
                    digest.update(chunk)
                handle.flush()
                os.fsync(handle.fileno())
            self._stage("after_file_fsync")
            sha256 = digest.hexdigest()
            self._verify_path(temporary, sha256, size)
            final = self.path_for_digest(sha256)
            _private_directory(final.parent)
            try:
                os.link(temporary, final, follow_symlinks=False)
            except FileExistsError:
                self._verify_concurrent_publish(final, sha256, size)
            else:
                os.chmod(final, 0o600, follow_symlinks=False)
            self._fsync_directory(final.parent)
            self._remove_temporary(temporary)
            self._stage("after_object_publish")
            ref = {
                "schema_id": "content-ref/v1",
                "artifact_id": artifact_id,
                "sha256": sha256,
                "size_bytes": size,
                "media_type": media_type,
                "content_schema_id": content_schema_id,
                "encoding": encoding,
            }
            self._record_reference(task_id, ref)
            self._stage("after_database_commit")
            return ref
        finally:
            self._remove_temporary(temporary)

    def read_verified(self, ref: dict[str, object]) -> bytes:
        digest = str(ref.get("sha256") or "")
        size = ref.get("size_bytes")
        if isinstance(size, bool) or not isinstance(size, int):
            raise CasCorruptError("CAS_CORRUPT: invalid ContentRef size")
        path = self.path_for_digest(digest)
        payload = self._verify_path(path, digest, size, capture=True)
        assert payload is not None
        return payload

    def collect_orphans(
        self, *, idle: bool, older_than_seconds: int, now: float | None = None
    ) -> list[str]:
        if not idle:
            return []
        invalid_age = isinstance(older_than_seconds, bool) or not isinstance(older_than_seconds, int)
        if invalid_age or older_than_seconds <= 0:
            raise AgentKernelStorageError('orphan_age_threshold_invalid')
        cutoff = (time.time() if now is None else now) - older_than_seconds
        with self.database.connect() as connection:
            object_rows = connection.execute(
                'SELECT sha256,size_bytes FROM content_objects'
            ).fetchall()
            referenced = {
                row[0]
                for row in connection.execute("SELECT DISTINCT sha256 FROM content_bindings")
            }
        for digest, size in object_rows:
            self._verify_path(self.path_for_digest(digest), digest, size)
        removed = self._collect_staging_orphans(cutoff)
        object_digests: list[str] = []
        changed_parents: set[Path] = set()
        for prefix in sorted(self.objects_root.iterdir()):
            if prefix.is_symlink() or not prefix.is_dir():
                continue
            for candidate in sorted(prefix.iterdir()):
                try:
                    metadata = candidate.lstat()
                except FileNotFoundError:
                    continue
                digest = candidate.name
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or not DIGEST_PATTERN.fullmatch(digest)
                    or digest in referenced
                    or metadata.st_mtime > cutoff
                ):
                    continue
                candidate.unlink()
                object_digests.append(digest)
                changed_parents.add(prefix)
        for parent in changed_parents:
            self._fsync_directory(parent)
        if object_digests:
            with self.database.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.executemany(
                    "DELETE FROM content_objects WHERE sha256=? "
                    "AND NOT EXISTS (SELECT 1 FROM content_bindings WHERE sha256=?)",
                    ((digest, digest) for digest in object_digests),
                )
                connection.commit()
        return removed + object_digests

    def _collect_staging_orphans(self, cutoff: float) -> list[str]:
        removed: list[str] = []
        for candidate in sorted(self.tmp_root.iterdir()):
            try:
                metadata = candidate.lstat()
            except FileNotFoundError:
                continue
            if (
                not stat.S_ISREG(metadata.st_mode)
                or stat.S_ISLNK(metadata.st_mode)
                or not TEMPORARY_PATTERN.fullmatch(candidate.name)
                or stat.S_IMODE(metadata.st_mode) != 0o600
                or metadata.st_mtime > cutoff
            ):
                continue
            candidate.unlink()
            removed.append(f"tmp:{candidate.name}")
        if removed:
            self._fsync_directory(self.tmp_root)
        return removed

    @staticmethod
    def _validate_metadata(
        task_id: str,
        artifact_id: str,
        media_type: str,
        content_schema_id: str,
        encoding: str,
        max_bytes: int,
    ) -> None:
        if not TASK_ID_PATTERN.fullmatch(task_id):
            raise AgentKernelStorageError("task_id_invalid")
        if not ARTIFACT_ID_PATTERN.fullmatch(artifact_id):
            raise AgentKernelStorageError("artifact_id_invalid")
        for name, value, limit in (
            ("media_type", media_type, 120),
            ("content_schema_id", content_schema_id, 160),
        ):
            if not isinstance(value, str) or not value or len(value) > limit:
                raise AgentKernelStorageError(f"{name}_invalid")
            try:
                value.encode("ascii")
            except UnicodeEncodeError as exc:
                raise AgentKernelStorageError(f"{name}_invalid") from exc
        if encoding not in {"utf-8", "binary"}:
            raise AgentKernelStorageError("content_encoding_invalid")
        if (
            isinstance(max_bytes, bool)
            or not isinstance(max_bytes, int)
            or not 0 <= max_bytes <= MAX_SAFE_INTEGER
        ):
            raise AgentKernelStorageError("content_size_limit_invalid")

    def _record_reference(self, task_id: str, ref: dict[str, object]) -> None:
        values = (
            ref["sha256"],
            ref["size_bytes"],
            ref["media_type"],
            ref["content_schema_id"],
            ref["encoding"],
        )
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing_object = connection.execute(
                    "SELECT size_bytes,media_type,content_schema_id,encoding "
                    "FROM content_objects WHERE sha256=?",
                    (ref["sha256"],),
                ).fetchone()
                expected_object = tuple(values[1:])
                if existing_object is None:
                    now = _timestamp()
                    connection.execute(
                        "INSERT INTO content_objects "
                        "(sha256,size_bytes,media_type,content_schema_id,encoding,created_at,verified_at) "
                        "VALUES(?,?,?,?,?,?,?)",
                        (*values, now, now),
                    )
                elif tuple(existing_object) != expected_object:
                    raise ContentRefConflictError(
                        "CONTENT_REF_CONFLICT: digest metadata differs"
                    )
                existing_binding = connection.execute(
                    "SELECT sha256,size_bytes,media_type,content_schema_id,encoding "
                    "FROM content_bindings WHERE task_id=? AND artifact_id=?",
                    (task_id, ref["artifact_id"]),
                ).fetchone()
                if existing_binding is None:
                    connection.execute(
                        "INSERT INTO content_bindings "
                        "(task_id,artifact_id,sha256,size_bytes,media_type,content_schema_id,encoding,created_at) "
                        "VALUES(?,?,?,?,?,?,?,?)",
                        (task_id, ref["artifact_id"], *values, _timestamp()),
                    )
                elif tuple(existing_binding) != values:
                    raise ContentRefConflictError(
                        "CONTENT_REF_CONFLICT: artifact identity rebound"
                    )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    @staticmethod
    def _verify_path(
        path: Path, digest: str, size: int, *, capture: bool = False
    ) -> bytes | None:
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(descriptor, "rb", closefd=True) as handle:
                metadata = os.fstat(handle.fileno())
                if not stat.S_ISREG(metadata.st_mode):
                    raise CasCorruptError("CAS_CORRUPT: object is not a regular file")
                if metadata.st_nlink != 1:
                    raise CasCorruptError("CAS_CORRUPT: object has unexpected hardlinks")
                if stat.S_IMODE(metadata.st_mode) != 0o600:
                    raise CasCorruptError("CAS_CORRUPT: object permissions are not private")
                if metadata.st_size != size:
                    raise CasCorruptError("CAS_CORRUPT: object size mismatch")
                observed = hashlib.sha256()
                captured: list[bytes] | None = [] if capture else None
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    observed.update(chunk)
                    if captured is not None:
                        captured.append(chunk)
        except OSError as exc:
            raise CasCorruptError("CAS_CORRUPT: object missing or unreadable") from exc
        if observed.hexdigest() != digest:
            raise CasCorruptError("CAS_CORRUPT: object digest mismatch")
        return b"".join(captured) if captured is not None else None

    @classmethod
    def _verify_concurrent_publish(cls, path: Path, digest: str, size: int) -> None:
        deadline = time.monotonic() + 5.0
        while True:
            try:
                cls._verify_path(path, digest, size)
                return
            except CasCorruptError as exc:
                try:
                    metadata = path.lstat()
                except FileNotFoundError:
                    raise exc
                if (
                    "unexpected hardlinks" not in str(exc)
                    or not stat.S_ISREG(metadata.st_mode)
                    or metadata.st_nlink not in {1, 2}
                    or time.monotonic() >= deadline
                ):
                    raise
                time.sleep(0.001)

    def _remove_temporary(self, path: Path) -> None:
        try:
            path.unlink()
        except FileNotFoundError:
            return
        self._fsync_directory(self.tmp_root)

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _stage(self, name: str) -> None:
        if self.stage_hook is not None:
            self.stage_hook(name)
