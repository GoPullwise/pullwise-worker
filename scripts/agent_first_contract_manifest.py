"""Strict-v1 baseline manifest validation and canonical text inspection."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
from typing import Any, Mapping


SCHEMA_ID = "pullwise-contract-baseline/v1"
HASH_PROFILE = "sha256-utf8-lf/v1"
REPOSITORY_DIRS = {
    "server": "pullwise-server",
    "web": "pullwise-web",
    "worker": "pullwise-worker",
}
EXPECTED_POLICY = {
    "head_drift": "informational",
    "unlisted_path_drift": "ignored",
    "blocking_surface_drift": "incompatible",
    "watched_surface_drift": "warning_if_fixed_probes_pass",
    "probe_failure": "incompatible",
    "probe_indeterminate": "indeterminate",
    "required_review": "baseline_owner_and_affected_repo_owner",
}
ALLOWED_ROLES = {
    "consumer",
    "fixture",
    "policy_source",
    "producer",
    "projection",
    "registry",
    "storage",
    "validator",
}
MAX_SURFACE_BYTES = 8 * 1024 * 1024
ID_PATTERN = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
GIT_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
RESERVED_WINDOWS_NAMES = {
    "aux",
    "con",
    "nul",
    "prn",
    *(f"com{number}" for number in range(1, 10)),
    *(f"lpt{number}" for number in range(1, 10)),
}
RunnerCatalog = Mapping[str, Mapping[str, Any]]


class ManifestError(ValueError):
    """The baseline manifest is malformed, ambiguous, or executable."""


class BaselineEnvironmentError(RuntimeError):
    """The baseline inputs cannot be inspected safely."""


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ManifestError(f"duplicate_key:{key}")
        result[key] = value
    return result


def load_manifest(path: Path, *, runner_catalog: RunnerCatalog) -> dict[str, Any]:
    try:
        payload = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ManifestError(f"non_finite_number:{value}")
            ),
        )
    except ManifestError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ManifestError("cannot_load_manifest") from exc
    if not isinstance(payload, dict):
        raise ManifestError("manifest_root_not_object")
    validate_manifest(payload, runner_catalog=runner_catalog)
    return payload


def _exact(value: object, required: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ManifestError(f"{label}:not_object")
    if set(value) != required:
        raise ManifestError(f"{label}:keys_mismatch")
    return value


def _text(value: object, label: str, *, single_line: bool = False) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ManifestError(f"{label}:invalid_text")
    if single_line and any(character in value for character in "\r\n"):
        raise ManifestError(f"{label}:not_single_line")
    return value


def _identifier(value: object, label: str) -> str:
    text = _text(value, label, single_line=True)
    if not ID_PATTERN.fullmatch(text):
        raise ManifestError(f"{label}:invalid_id")
    return text


def _relative_path(value: object, label: str) -> str:
    text = _text(value, label, single_line=True)
    if text != text.strip() or "\\" in text or ":" in text:
        raise ManifestError(f"{label}:unsafe_path")
    if text.startswith("/") or text.endswith("/") or "//" in text:
        raise ManifestError(f"{label}:unsafe_path")
    parts = text.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ManifestError(f"{label}:unsafe_path")
    for part in parts:
        if part.endswith((" ", ".")) or any(ord(character) < 32 for character in part):
            raise ManifestError(f"{label}:unsafe_path")
        if part.split(".", 1)[0].casefold() in RESERVED_WINDOWS_NAMES:
            raise ManifestError(f"{label}:reserved_path")
    if PurePosixPath(text).is_absolute():
        raise ManifestError(f"{label}:unsafe_path")
    return text


def _sorted_unique(value: object, label: str) -> list[str]:
    if not isinstance(value, list) or not value:
        raise ManifestError(f"{label}:not_nonempty_array")
    texts = [_text(item, f"{label}[]", single_line=True) for item in value]
    if texts != sorted(set(texts)):
        raise ManifestError(f"{label}:not_sorted_unique")
    return texts


def validate_manifest(payload: object, *, runner_catalog: RunnerCatalog) -> None:
    root = _exact(
        payload,
        {
            "schema_id",
            "baseline_id",
            "protocol_version",
            "hash_profile",
            "baseline_owner",
            "appendix",
            "compatibility_policy",
            "repositories",
            "surfaces",
            "tests",
        },
        "manifest",
    )
    if root["schema_id"] != SCHEMA_ID:
        raise ManifestError("unsupported_schema")
    _identifier(root["baseline_id"], "baseline_id")
    if root["protocol_version"] != "review-worker-protocol/v1":
        raise ManifestError("unsupported_protocol")
    if root["hash_profile"] != HASH_PROFILE:
        raise ManifestError("unsupported_hash_profile")
    _text(root["baseline_owner"], "baseline_owner", single_line=True)
    policy = _exact(
        root["compatibility_policy"], set(EXPECTED_POLICY), "compatibility_policy"
    )
    if policy != EXPECTED_POLICY:
        raise ManifestError("compatibility_policy:not_frozen")

    repositories = root["repositories"]
    if not isinstance(repositories, list):
        raise ManifestError("repositories:not_array")
    repo_ids: list[str] = []
    for index, item in enumerate(repositories):
        repo = _exact(item, {"id", "owner", "frozen_head"}, f"repositories[{index}]")
        repo_id = _identifier(repo["id"], f"repositories[{index}].id")
        _text(repo["owner"], f"repositories[{index}].owner", single_line=True)
        if not GIT_SHA_PATTERN.fullmatch(str(repo["frozen_head"])):
            raise ManifestError(f"repositories[{index}].frozen_head:invalid_sha")
        repo_ids.append(repo_id)
    if repo_ids != sorted(REPOSITORY_DIRS):
        raise ManifestError("repositories:must_match_fixed_catalog")

    appendix = _exact(
        root["appendix"], {"repo", "path", "start_marker", "end_marker"}, "appendix"
    )
    if appendix["repo"] != "worker":
        raise ManifestError("appendix:must_be_worker_owned")
    _relative_path(appendix["path"], "appendix.path")
    start = _text(appendix["start_marker"], "appendix.start_marker", single_line=True)
    end = _text(appendix["end_marker"], "appendix.end_marker", single_line=True)
    if start == end:
        raise ManifestError("appendix:markers_not_distinct")

    tests = root["tests"]
    if not isinstance(tests, list) or not tests:
        raise ManifestError("tests:not_nonempty_array")
    test_ids: list[str] = []
    for index, item in enumerate(tests):
        test = _exact(item, {"id", "runner_id"}, f"tests[{index}]")
        test_id = _identifier(test["id"], f"tests[{index}].id")
        runner_id = _identifier(test["runner_id"], f"tests[{index}].runner_id")
        if runner_id not in runner_catalog:
            raise ManifestError(f"tests[{index}].runner_id:unknown")
        if runner_catalog[runner_id].get("repo") not in REPOSITORY_DIRS:
            raise ManifestError(f"tests[{index}].runner_id:invalid_catalog_repo")
        test_ids.append(test_id)
    if test_ids != sorted(set(test_ids)):
        raise ManifestError("tests:not_sorted_unique")
    known_tests = set(test_ids)

    surfaces = root["surfaces"]
    if not isinstance(surfaces, list) or not surfaces:
        raise ManifestError("surfaces:not_nonempty_array")
    surface_ids: list[str] = []
    path_keys: set[tuple[str, str]] = set()
    referenced_probes: set[str] = set()
    for index, item in enumerate(surfaces):
        surface = _exact(
            item,
            {
                "id",
                "repo",
                "path",
                "roles",
                "anchors",
                "enforcement",
                "probe_ids",
                "sha256",
            },
            f"surfaces[{index}]",
        )
        surface_id = _identifier(surface["id"], f"surfaces[{index}].id")
        repo_id = _identifier(surface["repo"], f"surfaces[{index}].repo")
        if repo_id not in REPOSITORY_DIRS:
            raise ManifestError(f"surfaces[{index}].repo:unknown")
        relative = _relative_path(surface["path"], f"surfaces[{index}].path")
        roles = _sorted_unique(surface["roles"], f"surfaces[{index}].roles")
        if not set(roles) <= ALLOWED_ROLES:
            raise ManifestError(f"surfaces[{index}].roles:unknown")
        _sorted_unique(surface["anchors"], f"surfaces[{index}].anchors")
        if surface["enforcement"] not in {"blocking", "watched"}:
            raise ManifestError(f"surfaces[{index}].enforcement:unknown")
        probes = _sorted_unique(surface["probe_ids"], f"surfaces[{index}].probe_ids")
        if not set(probes) <= known_tests:
            raise ManifestError(f"surfaces[{index}].probe_ids:unknown")
        if not SHA256_PATTERN.fullmatch(str(surface["sha256"])):
            raise ManifestError(f"surfaces[{index}].sha256:invalid")
        path_key = (repo_id, relative.casefold())
        if path_key in path_keys:
            raise ManifestError("surfaces:casefold_path_collision")
        path_keys.add(path_key)
        surface_ids.append(surface_id)
        referenced_probes.update(probes)
    if surface_ids != sorted(set(surface_ids)):
        raise ManifestError("surfaces:not_sorted_unique")
    if referenced_probes != known_tests:
        raise ManifestError("tests:must_be_referenced_by_surfaces")


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


def _display_command(spec: Mapping[str, Any]) -> str:
    nodes = " ".join(str(node) for node in spec["nodes"])
    if spec["runner"] == "python_unittest":
        return f"python -B -m unittest {nodes}"
    return f"node node_modules/vitest/vitest.mjs run --reporter=json {nodes}"


def render_appendix(manifest: dict[str, Any], *, runner_catalog: RunnerCatalog) -> str:
    validate_manifest(manifest, runner_catalog=runner_catalog)
    lines = [
        f"> Generated from `{manifest['baseline_id']}` with `{HASH_PROFILE}`. Do not edit this block by hand.",
        "",
        "| Repository | Frozen HEAD (informational only) | Owner |",
        "|---|---|---|",
    ]
    for repo in manifest["repositories"]:
        lines.append(
            f"| `{REPOSITORY_DIRS[repo['id']]}` | `{repo['frozen_head']}` | {repo['owner']} |"
        )
    lines.extend(
        [
            "",
            "| Contract surface | Repository path | Roles | Enforcement | Fixed probes | SHA-256 |",
            "|---|---|---|---|---|---|",
        ]
    )
    for surface in manifest["surfaces"]:
        full_path = f"{REPOSITORY_DIRS[surface['repo']]}/{surface['path']}"
        lines.append(
            f"| `{surface['id']}` | `{full_path}` | `{','.join(surface['roles'])}` | "
            f"`{surface['enforcement']}` | `{','.join(surface['probe_ids'])}` | `{surface['sha256']}` |"
        )
    lines.extend(["", "Fixed executable probes:", ""])
    for test in manifest["tests"]:
        spec = runner_catalog[test["runner_id"]]
        cwd = REPOSITORY_DIRS[str(spec["repo"])]
        lines.append(
            f"- `{test['id']}` (cwd `{cwd}`): `{_display_command(spec)}`"
        )
    lines.extend(
        [
            "",
            "Compatibility rule: HEAD and unlisted-path drift are informational. Blocking fixture drift, "
            "Appendix drift, or a completed failing fixed probe is incompatible. Watched source drift is "
            "a warning only after every linked fixed probe passes; an unavailable or incomplete probe is indeterminate.",
            "Baseline refresh is a read-only candidate operation and requires both the baseline owner and the affected repository owner.",
        ]
    )
    return "\n".join(lines)
