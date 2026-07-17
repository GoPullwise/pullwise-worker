"""Safe, canonical reads for strict-v1 contract baseline inputs."""

from __future__ import annotations

import ast
import hashlib
import os
from pathlib import Path, PurePosixPath
import stat


REPOSITORY_DIRS = {
    "server": "pullwise-server",
    "web": "pullwise-web",
    "worker": "pullwise-worker",
}
MAX_SURFACE_BYTES = 8 * 1024 * 1024


class BaselineEnvironmentError(RuntimeError):
    """The baseline inputs cannot be inspected safely."""


def _is_reparse(info: os.stat_result) -> bool:
    marker = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(marker and getattr(info, "st_file_attributes", 0) & marker)


def repository_roots(workspace_root: Path) -> dict[str, Path]:
    try:
        workspace_info = workspace_root.lstat()
        workspace = workspace_root.resolve(strict=True)
    except OSError as exc:
        raise BaselineEnvironmentError("workspace_unavailable") from exc
    if not stat.S_ISDIR(workspace_info.st_mode) or _is_reparse(workspace_info):
        raise BaselineEnvironmentError("workspace_unsafe")
    roots: dict[str, Path] = {}
    for repo_id, directory in REPOSITORY_DIRS.items():
        lexical = workspace / directory
        try:
            info = lexical.lstat()
            resolved = lexical.resolve(strict=True)
        except OSError as exc:
            raise BaselineEnvironmentError(f"repository_unavailable:{repo_id}") from exc
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode) or _is_reparse(info):
            raise BaselineEnvironmentError(f"repository_unsafe:{repo_id}")
        try:
            resolved.relative_to(workspace)
        except ValueError as exc:
            raise BaselineEnvironmentError(f"repository_escape:{repo_id}") from exc
        roots[repo_id] = resolved
    return roots


def surface_path(repo_root: Path, relative_path: str) -> Path | None:
    current = repo_root
    parts = PurePosixPath(relative_path).parts
    for index, part in enumerate(parts):
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise BaselineEnvironmentError("surface_unavailable") from exc
        if stat.S_ISLNK(info.st_mode) or _is_reparse(info):
            raise BaselineEnvironmentError("surface_reparse_point")
        final = index == len(parts) - 1
        if final and not stat.S_ISREG(info.st_mode):
            raise BaselineEnvironmentError("surface_not_regular")
        if not final and not stat.S_ISDIR(info.st_mode):
            raise BaselineEnvironmentError("surface_parent_not_directory")
    resolved = current.resolve(strict=True)
    try:
        resolved.relative_to(repo_root)
    except ValueError as exc:
        raise BaselineEnvironmentError("surface_escape") from exc
    return resolved


def read_surface(path: Path) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise BaselineEnvironmentError("surface_open_failed") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_SURFACE_BYTES:
            raise BaselineEnvironmentError("surface_size_or_type_invalid")
        chunks: list[bytes] = []
        remaining = MAX_SURFACE_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    identity_before = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    identity_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    if len(raw) > MAX_SURFACE_BYTES or identity_before != identity_after:
        raise BaselineEnvironmentError("surface_changed_during_read")
    return raw


def canonical_text(path: Path) -> str:
    try:
        text = read_surface(path).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BaselineEnvironmentError("surface_not_utf8") from exc
    return text.replace("\r\n", "\n").replace("\r", "\n")


def text_sha256(path: Path) -> str:
    return hashlib.sha256(canonical_text(path).encode("utf-8")).hexdigest()


def python_collection_values_from_text(
    text: str, symbol: str, *, ordered: bool
) -> list[str]:
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        raise BaselineEnvironmentError("registry_source_invalid_python") from exc
    matches: list[ast.expr] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            if any(isinstance(target, ast.Name) and target.id == symbol for target in node.targets):
                matches.append(node.value)
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and node.target.id == symbol and node.value:
                matches.append(node.value)
    if len(matches) != 1:
        raise BaselineEnvironmentError("registry_symbol_missing_or_ambiguous")
    try:
        literal = ast.literal_eval(matches[0])
    except (ValueError, SyntaxError) as exc:
        raise BaselineEnvironmentError("registry_value_not_literal") from exc
    if not isinstance(literal, (list, tuple, set, frozenset)):
        raise BaselineEnvironmentError("registry_value_not_collection")
    values = list(literal)
    if any(not isinstance(value, str) or not value for value in values):
        raise BaselineEnvironmentError("registry_value_not_string")
    if len(values) != len(set(values)):
        raise BaselineEnvironmentError("registry_values_not_unique")
    return values if ordered else sorted(values)


def python_collection_values(path: Path, symbol: str, *, ordered: bool) -> list[str]:
    return python_collection_values_from_text(
        canonical_text(path),
        symbol,
        ordered=ordered,
    )
