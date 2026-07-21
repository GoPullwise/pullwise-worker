"""Deterministic, fail-closed SourceState primitives for the Agent Kernel."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import stat
import unicodedata
from typing import Callable, Mapping

from .agent_kernel_canonical import canonical_bytes, canonical_sha256


DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")
REVISION_PATTERN = re.compile(r"^(?:[0-9a-f]{40}|unversioned:[0-9a-f]{64})$")
DRIVE_PATTERN = re.compile(r"^[A-Za-z]:")
StageHook = Callable[[str, Path], None]


class SourceStateError(RuntimeError):
    """Source identity could not be established without ambiguity."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


def _canonical_path(value: str) -> str:
    invalid = (
        not isinstance(value, str)
        or not value
        or chr(92) in value
        or "\x00" in value
    )
    if invalid:
        raise SourceStateError("SOURCE_PATH_INVALID", str(value))
    if unicodedata.normalize("NFC", value) != value:
        raise SourceStateError("SOURCE_PATH_NOT_NFC", value)
    path = PurePosixPath(value)
    if (
        value.startswith("/")
        or DRIVE_PATTERN.match(value)
        or any(part in {"", ".", ".."} for part in value.split("/"))
        or path.as_posix() != value
    ):
        raise SourceStateError("SOURCE_PATH_INVALID", value)
    return value


def _ordered_paths(values: tuple[str, ...]) -> tuple[str, ...]:
    canonical = tuple(_canonical_path(value.rstrip("/")) for value in values)
    ordered = tuple(sorted(canonical, key=lambda item: item.encode("utf-8")))
    if len(set(ordered)) != len(ordered):
        raise SourceStateError("SOURCE_PATH_DUPLICATE")
    folded: dict[str, str] = {}
    for value in ordered:
        prior = folded.setdefault(value.casefold(), value)
        if prior != value:
            raise SourceStateError("SOURCE_PATH_CASE_COLLISION", f"{prior}, {value}")
    return ordered


@dataclass(frozen=True)
class SourceSelectionPolicy:
    root_identity: str
    include: str
    excluded_control_roots: tuple[str, ...]
    ephemeral_patterns: tuple[str, ...] = ()
    symlink_policy: str = "record_target_no_follow"
    case_collision_policy: str = "reject"

    def __post_init__(self) -> None:
        invalid_root = (
            not isinstance(self.root_identity, str)
            or not self.root_identity
            or self.root_identity.startswith(("/", chr(92)))
            or DRIVE_PATTERN.match(self.root_identity)
            or "\x00" in self.root_identity
        )
        if invalid_root:
            raise SourceStateError("SOURCE_ROOT_IDENTITY_INVALID")
        if unicodedata.normalize("NFC", self.root_identity) != self.root_identity:
            raise SourceStateError("SOURCE_ROOT_IDENTITY_NOT_NFC")
        if self.include != "all_repository_regular_files":
            raise SourceStateError("SOURCE_INCLUDE_POLICY_UNSUPPORTED", self.include)
        if self.symlink_policy != "record_target_no_follow":
            raise SourceStateError("SOURCE_SYMLINK_POLICY_UNSUPPORTED")
        if self.case_collision_policy != "reject":
            raise SourceStateError("SOURCE_CASE_POLICY_UNSUPPORTED")
        object.__setattr__(
            self, "excluded_control_roots", _ordered_paths(self.excluded_control_roots)
        )
        patterns = tuple(self.ephemeral_patterns)
        expected = tuple(
            sorted(set(patterns), key=lambda item: item.encode("utf-8"))
        )
        if any(not isinstance(value, str) or not value for value in patterns):
            raise SourceStateError("SOURCE_EPHEMERAL_PATTERN_INVALID")
        if expected != patterns:
            raise SourceStateError("SOURCE_EPHEMERAL_PATTERN_ORDER_INVALID")

    @classmethod
    def pullwise_full_scan(
        cls,
        *,
        root_identity: str,
        excluded_control_roots: tuple[str, ...] = (".git", ".codex-review"),
    ) -> "SourceSelectionPolicy":
        return cls(
            root_identity=root_identity,
            include="all_repository_regular_files",
            excluded_control_roots=excluded_control_roots,
        )

    def _identity(self) -> dict[str, object]:
        return {
            "schema_id": "source-selection-policy/v1",
            "root_identity": self.root_identity,
            "include": self.include,
            "excluded_control_roots": list(self.excluded_control_roots),
            "ephemeral_patterns": list(self.ephemeral_patterns),
            "symlink_policy": self.symlink_policy,
            "case_collision_policy": self.case_collision_policy,
        }

    @property
    def digest(self) -> str:
        return canonical_sha256(self._identity())

    def as_dict(self) -> dict[str, object]:
        return {**self._identity(), "digest": self.digest}


@dataclass(frozen=True)
class SourceEntry:
    path: str
    type: str
    size_bytes: int | None = None
    sha256: str | None = None
    executable: bool | None = None
    target: str | None = None
    commit_sha: str | None = None

    def __post_init__(self) -> None:
        _canonical_path(self.path)
        expected = {
            "file": (self.size_bytes, self.sha256, self.executable, None, None),
            "symlink": (None, None, None, self.target, None),
            "gitlink": (None, None, None, None, self.commit_sha),
        }
        if self.type not in expected:
            raise SourceStateError("SOURCE_ENTRY_TYPE_INVALID", self.type)
        actual = (
            self.size_bytes,
            self.sha256,
            self.executable,
            self.target,
            self.commit_sha,
        )
        if actual != expected[self.type]:
            raise SourceStateError("SOURCE_ENTRY_UNION_INVALID", self.path)
        if self.type == "file":
            if (
                isinstance(self.size_bytes, bool)
                or not isinstance(self.size_bytes, int)
                or self.size_bytes < 0
                or not isinstance(self.sha256, str)
                or not DIGEST_PATTERN.fullmatch(self.sha256)
                or not isinstance(self.executable, bool)
            ):
                raise SourceStateError("SOURCE_FILE_ENTRY_INVALID", self.path)
        elif self.type == "symlink":
            if not isinstance(self.target, str) or "\x00" in self.target:
                raise SourceStateError("SOURCE_SYMLINK_ENTRY_INVALID", self.path)
            if unicodedata.normalize("NFC", self.target) != self.target:
                raise SourceStateError("SOURCE_SYMLINK_TARGET_NOT_NFC", self.path)
        elif not isinstance(self.commit_sha, str) or not re.fullmatch(
            r"[0-9a-f]{40}", self.commit_sha
        ):
            raise SourceStateError("SOURCE_GITLINK_ENTRY_INVALID", self.path)

    @classmethod
    def file(
        cls,
        path: str,
        *,
        size_bytes: int,
        sha256: str,
        executable: bool = False,
    ) -> "SourceEntry":
        return cls(
            path,
            "file",
            size_bytes=size_bytes,
            sha256=sha256,
            executable=executable,
        )

    @classmethod
    def symlink(cls, path: str, *, target: str) -> "SourceEntry":
        return cls(path, "symlink", target=target)

    @classmethod
    def gitlink(cls, path: str, *, commit_sha: str) -> "SourceEntry":
        return cls(path, "gitlink", commit_sha=commit_sha)

    def as_dict(self) -> dict[str, object]:
        if self.type == "file":
            return {
                "path": self.path,
                "type": self.type,
                "size_bytes": self.size_bytes,
                "sha256": self.sha256,
                "executable": self.executable,
            }
        if self.type == "symlink":
            return {"path": self.path, "type": self.type, "target": self.target}
        return {
            "path": self.path,
            "type": self.type,
            "commit_sha": self.commit_sha,
        }


@dataclass(frozen=True)
class SourceTreeSnapshot:
    base_revision: str
    selection_policy_digest: str
    entries: tuple[SourceEntry, ...]
    policy_content_sha256: str | None = None
    policy_content_size: int | None = None

    def __post_init__(self) -> None:
        if not REVISION_PATTERN.fullmatch(self.base_revision):
            raise SourceStateError("SOURCE_BASE_REVISION_INVALID")
        if not DIGEST_PATTERN.fullmatch(self.selection_policy_digest):
            raise SourceStateError("SOURCE_POLICY_DIGEST_INVALID")
        ordered = _ordered_paths(tuple(entry.path for entry in self.entries))
        if ordered != tuple(entry.path for entry in self.entries):
            raise SourceStateError("SOURCE_ENTRY_ORDER_INVALID")
        if (self.policy_content_sha256 is None) != (
            self.policy_content_size is None
        ):
            raise SourceStateError("SOURCE_POLICY_CONTENT_IDENTITY_INVALID")

    @property
    def entry_count(self) -> int:
        return len(self.entries)

    @property
    def total_bytes(self) -> int:
        return sum(
            entry.size_bytes or 0 for entry in self.entries if entry.type == "file"
        )

    @property
    def source_state_id(self) -> str:
        return canonical_sha256(
            {
                "base_revision": self.base_revision,
                "selection_policy_digest": self.selection_policy_digest,
                "entries": [entry.as_dict() for entry in self.entries],
            }
        )

    def to_manifest(
        self, policy_ref: Mapping[str, object]
    ) -> dict[str, object]:
        if (
            self.policy_content_sha256 is None
            or policy_ref.get("schema_id") != "content-ref/v1"
            or policy_ref.get("content_schema_id")
            != "source-selection-policy/v1"
            or policy_ref.get("encoding") != "utf-8"
            or policy_ref.get("sha256") != self.policy_content_sha256
            or policy_ref.get("size_bytes") != self.policy_content_size
        ):
            raise SourceStateError("SOURCE_POLICY_REF_MISMATCH")
        manifest: dict[str, object] = {
            "schema_id": "source-tree-manifest/v1",
            "base_revision": self.base_revision,
            "selection_policy_ref": dict(policy_ref),
            "selection_policy_digest": self.selection_policy_digest,
            "entries": [entry.as_dict() for entry in self.entries],
            "entry_count": self.entry_count,
            "total_bytes": self.total_bytes,
        }
        manifest["manifest_digest"] = canonical_sha256(manifest)
        return manifest


def _same_file(left: os.stat_result, right: os.stat_result) -> bool:
    return all(
        getattr(left, field) == getattr(right, field)
        for field in ("st_dev", "st_ino", "st_mode", "st_size", "st_mtime_ns")
    )


def _is_reparse(metadata: os.stat_result) -> bool:
    marker = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(marker and getattr(metadata, "st_file_attributes", 0) & marker)


def _is_excluded(path: str, roots: tuple[str, ...]) -> bool:
    return any(path == root or path.startswith(root + "/") for root in roots)


def _read_file(
    path: Path, relative: str, hook: StageHook | None
) -> SourceEntry:
    try:
        before = path.lstat()
        descriptor = os.open(
            path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        )
        with os.fdopen(descriptor, "rb", closefd=True) as handle:
            opened = os.fstat(handle.fileno())
            if not stat.S_ISREG(opened.st_mode) or _is_reparse(opened):
                raise SourceStateError("SOURCE_FILE_IDENTITY_INVALID", relative)
            digest = hashlib.sha256()
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
            after_read = os.fstat(handle.fileno())
        if hook is not None:
            hook("after_file_read", path)
        after_path = path.lstat()
    except SourceStateError:
        raise
    except OSError as exc:
        raise SourceStateError("SOURCE_FILE_UNREADABLE", relative) from exc
    if not (
        _same_file(before, opened)
        and _same_file(opened, after_read)
        and _same_file(after_read, after_path)
    ):
        raise SourceStateError("SOURCE_CHANGED_DURING_SCAN", relative)
    executable = os.name != "nt" and bool(opened.st_mode & 0o111)
    return SourceEntry.file(
        relative,
        size_bytes=opened.st_size,
        sha256=digest.hexdigest(),
        executable=executable,
    )


def snapshot_source_tree(
    root: Path,
    *,
    policy: SourceSelectionPolicy,
    base_revision: str,
    gitlinks: Mapping[str, str] | None = None,
    stage_hook: StageHook | None = None,
) -> SourceTreeSnapshot:
    root = Path(root)
    try:
        root_metadata = root.lstat()
    except OSError as exc:
        raise SourceStateError("SOURCE_ROOT_UNREADABLE") from exc
    if (
        not stat.S_ISDIR(root_metadata.st_mode)
        or stat.S_ISLNK(root_metadata.st_mode)
        or _is_reparse(root_metadata)
    ):
        raise SourceStateError("SOURCE_ROOT_INVALID")
    declared = {
        _canonical_path(path): revision
        for path, revision in (gitlinks or {}).items()
    }
    for path, revision in declared.items():
        if _is_excluded(
            path, policy.excluded_control_roots
        ) or not re.fullmatch(r"[0-9a-f]{40}", revision):
            raise SourceStateError(
                "SOURCE_GITLINK_DECLARATION_INVALID", path
            )
    seen_gitlinks: set[str] = set()
    collected: list[SourceEntry] = []

    def scan(directory: Path, prefix: str = "") -> None:
        try:
            before = directory.lstat()
            names_before = tuple(item.name for item in os.scandir(directory))
        except OSError as exc:
            raise SourceStateError(
                "SOURCE_DIRECTORY_UNREADABLE", prefix or "."
            ) from exc
        for name in sorted(
            names_before, key=lambda value: value.encode("utf-8")
        ):
            relative = f"{prefix}/{name}" if prefix else name
            _canonical_path(relative)
            if _is_excluded(relative, policy.excluded_control_roots):
                continue
            path = directory / name
            try:
                metadata = path.lstat()
            except OSError as exc:
                raise SourceStateError(
                    "SOURCE_ENTRY_UNREADABLE", relative
                ) from exc
            if relative in declared:
                if not stat.S_ISDIR(
                    metadata.st_mode
                ) or stat.S_ISLNK(metadata.st_mode):
                    raise SourceStateError(
                        "SOURCE_GITLINK_IDENTITY_INVALID", relative
                    )
                collected.append(
                    SourceEntry.gitlink(
                        relative, commit_sha=declared[relative]
                    )
                )
                seen_gitlinks.add(relative)
            elif stat.S_ISLNK(metadata.st_mode) or _is_reparse(metadata):
                if not stat.S_ISLNK(metadata.st_mode):
                    raise SourceStateError("SOURCE_REPARSE_POINT", relative)
                try:
                    target = os.readlink(path)
                    if os.name == "nt":
                        separator = chr(92)
                        extended = separator * 2 + "?" + separator
                        unc = extended + "UNC" + separator
                        if target.startswith(unc):
                            target = separator * 2 + target[len(unc) :]
                        elif target.startswith(extended):
                            target = target[len(extended) :]
                    after = path.lstat()
                except OSError as exc:
                    raise SourceStateError(
                        "SOURCE_SYMLINK_UNREADABLE", relative
                    ) from exc
                if not _same_file(metadata, after):
                    raise SourceStateError(
                        "SOURCE_CHANGED_DURING_SCAN", relative
                    )
                collected.append(
                    SourceEntry.symlink(relative, target=target)
                )
            elif stat.S_ISDIR(metadata.st_mode):
                scan(path, relative)
            elif stat.S_ISREG(metadata.st_mode):
                collected.append(
                    _read_file(path, relative, stage_hook)
                )
            else:
                raise SourceStateError("SOURCE_SPECIAL_FILE", relative)
        try:
            after = directory.lstat()
            names_after = tuple(item.name for item in os.scandir(directory))
        except OSError as exc:
            raise SourceStateError(
                "SOURCE_CHANGED_DURING_SCAN", prefix or "."
            ) from exc
        if not _same_file(before, after) or set(names_before) != set(
            names_after
        ):
            raise SourceStateError(
                "SOURCE_CHANGED_DURING_SCAN", prefix or "."
            )

    scan(root)
    missing = set(declared) - seen_gitlinks
    if missing:
        raise SourceStateError("SOURCE_GITLINK_MISSING", sorted(missing)[0])
    entries = tuple(
        sorted(collected, key=lambda item: item.path.encode("utf-8"))
    )
    policy_bytes = canonical_bytes(policy.as_dict())
    return SourceTreeSnapshot(
        base_revision=base_revision,
        selection_policy_digest=policy.digest,
        entries=entries,
        policy_content_sha256=hashlib.sha256(policy_bytes).hexdigest(),
        policy_content_size=len(policy_bytes),
    )


def diff_source_trees(
    original: SourceTreeSnapshot, final: SourceTreeSnapshot
) -> dict[str, object]:
    before = {entry.path: entry for entry in original.entries}
    after = {entry.path: entry for entry in final.entries}
    added: list[dict[str, object]] = []
    modified: list[dict[str, object]] = []
    deleted: list[dict[str, object]] = []
    type_changed: list[dict[str, object]] = []
    paths = sorted(
        set(before) | set(after), key=lambda value: value.encode("utf-8")
    )
    for path in paths:
        left, right = before.get(path), after.get(path)
        if left is None:
            assert right is not None
            added.append({"path": path, "after": right.as_dict()})
        elif right is None:
            deleted.append({"path": path, "before": left.as_dict()})
        elif left.type != right.type:
            type_changed.append(
                {
                    "path": path,
                    "before": left.as_dict(),
                    "after": right.as_dict(),
                }
            )
        elif left != right:
            modified.append(
                {
                    "path": path,
                    "before": left.as_dict(),
                    "after": right.as_dict(),
                }
            )
    empty = not (added or modified or deleted or type_changed)
    return {
        "schema_id": "change-set/v1",
        "original_source_state_id": original.source_state_id,
        "final_source_state_id": final.source_state_id,
        "added": added,
        "modified": modified,
        "deleted": deleted,
        "type_changed": type_changed,
        "patch_ref": None,
        "change_set_ref": None if empty else "unpersisted",
    }


def assert_pullwise_source_unchanged(
    original: SourceTreeSnapshot, final: SourceTreeSnapshot
) -> None:
    changes = diff_source_trees(original, final)
    categories = ("added", "modified", "deleted", "type_changed")
    if any(changes[key] for key in categories):
        raise SourceStateError("SOURCE_MUTATION_FORBIDDEN")


__all__ = [
    "SourceEntry",
    "SourceSelectionPolicy",
    "SourceStateError",
    "SourceTreeSnapshot",
    "assert_pullwise_source_unchanged",
    "diff_source_trees",
    "snapshot_source_tree",
]
