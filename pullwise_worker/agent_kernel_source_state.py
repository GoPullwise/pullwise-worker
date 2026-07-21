"""Internal SourceState identities; versioned encoding belongs to Server."""

from __future__ import annotations

from dataclasses import dataclass
import re
from pathlib import Path, PurePosixPath
import unicodedata
from typing import Callable

from .agent_kernel_canonical import canonical_sha256


DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")
REVISION_PATTERN = re.compile(r"^(?:[0-9a-f]{40}|unversioned:[0-9a-f]{64})$")
DRIVE_PATTERN = re.compile(r"^[A-Za-z]:")
PULLWISE_EXCLUDED_CONTROL_ROOTS = (".codex-review", ".git")
MAX_SAFE_INTEGER = 2**53 - 1


class SourceStateError(RuntimeError):
    """Source identity could not be established without ambiguity."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


def _utf8(value: str, code: str) -> bytes:
    try:
        return value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise SourceStateError(code) from exc


def _canonical_path(value: str) -> str:
    invalid = (
        not isinstance(value, str)
        or not value
        or chr(92) in value
        or "\x00" in value
    )
    if invalid:
        raise SourceStateError("SOURCE_PATH_INVALID", str(value))
    _utf8(value, "SOURCE_PATH_NOT_UTF8")
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
    ordered = tuple(sorted(canonical, key=_path_key))
    if len(set(ordered)) != len(ordered):
        raise SourceStateError("SOURCE_PATH_DUPLICATE")
    folded: dict[str, str] = {}
    for value in ordered:
        parts = value.split("/")
        for length in range(1, len(parts) + 1):
            component_path = "/".join(parts[:length])
            prior = folded.setdefault(component_path.casefold(), component_path)
            if prior != component_path:
                raise SourceStateError(
                    "SOURCE_PATH_CASE_COLLISION",
                    f"{prior}, {component_path}",
                )
    return ordered


def _path_key(value: str) -> bytes:
    return _utf8(value, "SOURCE_PATH_NOT_UTF8")


def _is_excluded(path: str, roots: tuple[str, ...]) -> bool:
    return any(path == root or path.startswith(root + "/") for root in roots)


@dataclass(frozen=True)
class SourceSelectionPolicy:
    """Worker-internal facts; not a local copy of the current package schema."""

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
        _utf8(self.root_identity, "SOURCE_ROOT_IDENTITY_NOT_UTF8")
        if unicodedata.normalize("NFC", self.root_identity) != self.root_identity:
            raise SourceStateError("SOURCE_ROOT_IDENTITY_NOT_NFC")
        if self.include != "all_repository_regular_files":
            raise SourceStateError("SOURCE_INCLUDE_POLICY_UNSUPPORTED", self.include)
        roots = _ordered_paths(tuple(self.excluded_control_roots))
        if roots != PULLWISE_EXCLUDED_CONTROL_ROOTS:
            raise SourceStateError("SOURCE_CONTROL_ROOTS_UNTRUSTED")
        object.__setattr__(self, "excluded_control_roots", roots)
        if self.ephemeral_patterns:
            raise SourceStateError("SOURCE_EPHEMERAL_PATTERN_UNSUPPORTED")
        object.__setattr__(self, "ephemeral_patterns", ())
        if self.symlink_policy != "record_target_no_follow":
            raise SourceStateError("SOURCE_SYMLINK_POLICY_UNSUPPORTED")
        if self.case_collision_policy != "reject":
            raise SourceStateError("SOURCE_CASE_POLICY_UNSUPPORTED")

    @classmethod
    def pullwise_full_scan(cls, *, root_identity: str) -> "SourceSelectionPolicy":
        return cls(
            root_identity=root_identity,
            include="all_repository_regular_files",
            excluded_control_roots=PULLWISE_EXCLUDED_CONTROL_ROOTS,
        )

    def identity_facts(self) -> dict[str, object]:
        return {
            "root_identity": self.root_identity,
            "include": self.include,
            "excluded_control_roots": list(self.excluded_control_roots),
            "ephemeral_patterns": [],
            "symlink_policy": self.symlink_policy,
            "case_collision_policy": self.case_collision_policy,
        }

    @property
    def digest(self) -> str:
        return canonical_sha256(self.identity_facts())


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
            invalid = (
                isinstance(self.size_bytes, bool)
                or not isinstance(self.size_bytes, int)
                or self.size_bytes < 0
                or self.size_bytes > MAX_SAFE_INTEGER
                or not isinstance(self.sha256, str)
                or not DIGEST_PATTERN.fullmatch(self.sha256)
                or not isinstance(self.executable, bool)
            )
            if invalid:
                raise SourceStateError("SOURCE_FILE_ENTRY_INVALID", self.path)
        elif self.type == "symlink":
            if not isinstance(self.target, str) or "\x00" in self.target:
                raise SourceStateError("SOURCE_SYMLINK_ENTRY_INVALID", self.path)
            _utf8(self.target, "SOURCE_SYMLINK_TARGET_NOT_UTF8")
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

    def identity_facts(self) -> dict[str, object]:
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

    def __post_init__(self) -> None:
        if not isinstance(self.entries, tuple) or not all(
            isinstance(entry, SourceEntry) for entry in self.entries
        ):
            raise SourceStateError("SOURCE_ENTRIES_INVALID")
        if not REVISION_PATTERN.fullmatch(self.base_revision):
            raise SourceStateError("SOURCE_BASE_REVISION_INVALID")
        if not DIGEST_PATTERN.fullmatch(self.selection_policy_digest):
            raise SourceStateError("SOURCE_POLICY_DIGEST_INVALID")
        paths = tuple(entry.path for entry in self.entries)
        ordered = _ordered_paths(paths)
        if ordered != paths:
            raise SourceStateError("SOURCE_ENTRY_ORDER_INVALID")
        selected = set(paths)
        for path in paths:
            parts = path.split("/")
            if any(
                "/".join(parts[:length]) in selected
                for length in range(1, len(parts))
            ):
                raise SourceStateError("SOURCE_ENTRY_TOPOLOGY_INVALID", path)

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
                "entries": [entry.identity_facts() for entry in self.entries],
            }
        )


@dataclass(frozen=True)
class SourceChange:
    path: str
    before: SourceEntry | None
    after: SourceEntry | None


@dataclass(frozen=True)
class SourceDiff:
    original_source_state_id: str
    final_source_state_id: str
    added: tuple[SourceChange, ...]
    modified: tuple[SourceChange, ...]
    deleted: tuple[SourceChange, ...]
    type_changed: tuple[SourceChange, ...]

    @property
    def is_empty(self) -> bool:
        return not (self.added or self.modified or self.deleted or self.type_changed)


def diff_source_trees(
    original: SourceTreeSnapshot, final: SourceTreeSnapshot
) -> SourceDiff:
    if (
        original.base_revision != final.base_revision
        or original.selection_policy_digest != final.selection_policy_digest
    ):
        raise SourceStateError("SOURCE_DIFF_IDENTITY_MISMATCH")
    before = {entry.path: entry for entry in original.entries}
    after = {entry.path: entry for entry in final.entries}
    groups: dict[str, list[SourceChange]] = {
        "added": [],
        "modified": [],
        "deleted": [],
        "type_changed": [],
    }
    for path in sorted(set(before) | set(after), key=_path_key):
        left, right = before.get(path), after.get(path)
        change = SourceChange(path=path, before=left, after=right)
        if left is None:
            groups["added"].append(change)
        elif right is None:
            groups["deleted"].append(change)
        elif left.type != right.type:
            groups["type_changed"].append(change)
        elif left != right:
            groups["modified"].append(change)
    return SourceDiff(
        original_source_state_id=original.source_state_id,
        final_source_state_id=final.source_state_id,
        added=tuple(groups["added"]),
        modified=tuple(groups["modified"]),
        deleted=tuple(groups["deleted"]),
        type_changed=tuple(groups["type_changed"]),
    )


def assert_pullwise_source_unchanged(
    original: SourceTreeSnapshot, final: SourceTreeSnapshot
) -> None:
    if not diff_source_trees(original, final).is_empty:
        raise SourceStateError("SOURCE_MUTATION_FORBIDDEN")


def snapshot_source_tree(
    root: Path,
    *,
    policy: SourceSelectionPolicy,
    base_revision: str,
    gitlink_catalog: object | None = None,
    stage_hook: Callable[[str, Path], None] | None = None,
) -> SourceTreeSnapshot:
    from .agent_kernel_source_scan import snapshot_source_tree as scan

    return scan(
        root,
        policy=policy,
        base_revision=base_revision,
        gitlink_catalog=gitlink_catalog,
        stage_hook=stage_hook,
    )


__all__ = [
    "SourceChange",
    "SourceDiff",
    "SourceEntry",
    "SourceSelectionPolicy",
    "SourceStateError",
    "SourceTreeSnapshot",
    "assert_pullwise_source_unchanged",
    "diff_source_trees",
    "snapshot_source_tree",
]
