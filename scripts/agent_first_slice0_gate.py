"""Strict observation helpers for the Worker Slice 0 evidence gate."""

from __future__ import annotations

import ast
import json
import subprocess
from pathlib import Path, PurePosixPath
from typing import Any, Iterable


HANDWRITTEN_SUFFIXES = frozenset(
    {
        ".asm", ".awk", ".bash", ".bat", ".c", ".cc", ".cjs", ".clj",
        ".cljs", ".cmake", ".cmd", ".cpp", ".cs", ".css", ".cxx", ".dart",
        ".ex", ".exs", ".fish", ".fs", ".fsx", ".go", ".gradle", ".graphql",
        ".groovy", ".h", ".hcl", ".hh", ".hpp", ".htm", ".html", ".hxx",
        ".java", ".jl", ".js", ".jsx", ".kt", ".kts", ".less", ".lua",
        ".m", ".mm", ".mjs", ".nim", ".php", ".pl", ".pm", ".proto",
        ".ps1", ".psm1", ".py", ".pyi", ".r", ".rb", ".rs", ".s",
        ".scala", ".scss", ".sh", ".sql", ".svelte", ".swift", ".tf",
        ".toml", ".ts", ".tsx", ".vb", ".vbs", ".vue", ".xml", ".yaml",
        ".yml", ".zig", ".zsh",
    }
)
HANDWRITTEN_NAMES = frozenset(
    {
        "build", "cmakelists.txt", "dockerfile", "gemfile", "jenkinsfile",
        "makefile", "meson.build", "rakefile", "vagrantfile", "workspace",
    }
)
RATCHET_ANCHORS = {
    "worker-current-implementation-2026-07-17": (
        "904165f3bed784faaa209ca80e33214c7b07f909",
        "contracts/agent-first/worker-slice-0-baseline.json",
    )
}


class GateObservationError(RuntimeError):
    pass


def physical_line_count(data: bytes) -> int:
    normalized = data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    if not normalized:
        return 0
    return normalized.count(b"\n") + int(not normalized.endswith(b"\n"))


def git_bytes(repo_root: Path, *args: str) -> bytes:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), *args],
            check=False,
            capture_output=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GateObservationError(f"git_unavailable:{args[0]}:{exc}") from exc
    if result.returncode != 0:
        raise GateObservationError(f"git_failed:{args[0]}:{result.returncode}")
    return result.stdout


def _git_blob_or_none(repo_root: Path, revision: str, path: str) -> bytes | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), "cat-file", "blob", f"{revision}:{path}"],
            check=False,
            capture_output=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GateObservationError(f"git_blob_unavailable:{revision}:{path}") from exc
    if result.returncode == 0:
        return result.stdout
    if result.returncode in {1, 128}:
        return None
    raise GateObservationError(f"git_blob_failed:{revision}:{path}:{result.returncode}")


def tracked_handwritten_files(repo_root: Path) -> tuple[tuple[str, bool], ...]:
    return parse_tracked_handwritten_files(
        git_bytes(repo_root, "ls-files", "--stage", "-z")
    )


def parse_tracked_handwritten_files(data: bytes) -> tuple[tuple[str, bool], ...]:
    try:
        records = data.decode("utf-8").split("\0")
    except UnicodeError as exc:
        raise GateObservationError("git_paths_not_utf8") from exc
    result: list[tuple[str, bool]] = []
    for record in records:
        if not record:
            continue
        try:
            metadata, raw_path = record.split("\t", 1)
            fields = metadata.split()
        except ValueError as exc:
            raise GateObservationError("git_stage_record_invalid") from exc
        if len(fields) != 3 or fields[2] != "0":
            raise GateObservationError("git_stage_record_invalid")
        mode = fields[0]
        if mode not in {"100644", "100755"}:
            continue
        if chr(92) in raw_path:
            raise GateObservationError("git_path_not_canonical")
        path = raw_path
        if is_handwritten(path, executable=mode == "100755"):
            result.append((path, mode == "100755"))
    return tuple(sorted(result))


def is_handwritten(path: str, *, executable: bool = False) -> bool:
    value = PurePosixPath(path)
    return (
        value.suffix.lower() in HANDWRITTEN_SUFFIXES
        or value.name.lower() in HANDWRITTEN_NAMES
        or (not value.suffix and executable)
    )


def pipeline_values(text: str, symbol: str) -> list[list[Any]] | None:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return None
    bindings = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == symbol and isinstance(node.ctx, (ast.Store, ast.Del)):
            bindings += 1
        elif isinstance(node, ast.Attribute) and node.attr == symbol and isinstance(node.ctx, (ast.Store, ast.Del)):
            bindings += 1
        elif (
            isinstance(node, ast.Subscript)
            and isinstance(node.ctx, (ast.Store, ast.Del))
            and isinstance(node.slice, ast.Constant)
            and node.slice.value == symbol
        ):
            bindings += 1
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == symbol:
            bindings += 1
        elif isinstance(node, ast.arg) and node.arg == symbol:
            bindings += 1
        elif isinstance(node, ast.alias):
            if node.name == "*":
                return None
            bound_name = node.asname or node.name.split(".", 1)[0]
            bindings += int(bound_name == symbol)
        elif isinstance(node, ast.ExceptHandler) and node.name == symbol:
            bindings += 1
        elif isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name == symbol:
            bindings += 1
        elif isinstance(node, ast.MatchMapping) and node.rest == symbol:
            bindings += 1
        elif isinstance(node, ast.Call):
            function_name = (
                node.func.id if isinstance(node.func, ast.Name)
                else node.func.attr if isinstance(node.func, ast.Attribute)
                else ""
            )
            if function_name in {"exec", "eval", "globals", "locals"}:
                return None
            if function_name == "vars" and not node.args and not node.keywords:
                return None
            if function_name in {"setattr", "delattr", "update", "setdefault", "pop", "__setitem__", "__delitem__"}:
                if any(isinstance(value, ast.Constant) and value.value == symbol for value in ast.walk(node)):
                    return None
    assignments: list[ast.expr] = []
    for node in tree.body:
        if isinstance(node, ast.Assign):
            if any(isinstance(target, ast.Name) and target.id == symbol for target in node.targets):
                assignments.append(node.value)
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and node.target.id == symbol and node.value is not None:
                assignments.append(node.value)
    if len(assignments) != 1 or bindings != 1:
        return None
    try:
        raw = ast.literal_eval(assignments[0])
    except (TypeError, ValueError, SyntaxError):
        return None
    if not isinstance(raw, tuple):
        return None
    result: list[list[Any]] = []
    for item in raw:
        if not isinstance(item, tuple) or len(item) != 2:
            return None
        name, progress = item
        if not isinstance(name, str) or not name:
            return None
        if not isinstance(progress, int) or isinstance(progress, bool) or not 0 <= progress <= 100:
            return None
        result.append([name, progress])
    return result or None


def _history_revisions(repo_root: Path, anchor: str, head: str, path: str) -> tuple[str, ...]:
    try:
        changed = git_bytes(
            repo_root,
            "log",
            "--format=%H",
            "--reverse",
            "--full-history",
            "--ancestry-path",
            f"{anchor}..{head}",
            "--",
            path,
        ).decode("ascii").splitlines()
    except UnicodeError as exc:
        raise GateObservationError("ratchet_history_not_ascii") from exc
    return tuple(dict.fromkeys((anchor, *changed)))


def historical_ratchet_evidence(
    repo_root: Path,
    baseline_id: str,
    head: str,
) -> tuple[tuple[dict[str, Any], ...], dict[str, tuple[int | None, ...]]]:
    try:
        anchor, manifest_path = RATCHET_ANCHORS[baseline_id]
    except KeyError as exc:
        raise GateObservationError(f"ratchet_anchor_unknown:{baseline_id}") from exc
    git_bytes(repo_root, "merge-base", "--is-ancestor", anchor, head)
    revisions = _history_revisions(repo_root, anchor, head, manifest_path)
    baselines: list[dict[str, Any]] = []
    for revision in revisions:
        try:
            value = json.loads(
                git_bytes(repo_root, "show", f"{revision}:{manifest_path}").decode("utf-8")
            )
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise GateObservationError(f"ratchet_manifest_unreadable:{revision}") from exc
        if not isinstance(value, dict):
            raise GateObservationError(f"ratchet_manifest_invalid:{revision}")
        baselines.append(value)
    entries = baselines[0].get("file_baselines")
    if not isinstance(entries, list):
        raise GateObservationError("ratchet_anchor_manifest_invalid")
    source_counts: dict[str, tuple[int | None, ...]] = {}
    for entry in entries:
        path = entry.get("path") if isinstance(entry, dict) else None
        if not isinstance(path, str):
            raise GateObservationError("ratchet_anchor_manifest_invalid")
        observations: list[int | None] = []
        for revision in _history_revisions(repo_root, anchor, head, path):
            blob = _git_blob_or_none(repo_root, revision, path)
            observations.append(None if blob is None else physical_line_count(blob))
        source_counts[path] = tuple(observations)
    return tuple(baselines), source_counts


def ratchet_failures(
    current: dict[str, Any],
    historical: Iterable[dict[str, Any]],
    source_counts: dict[str, Iterable[int | None]] | None = None,
) -> list[dict[str, Any]]:
    snapshots = tuple(historical)
    if not snapshots:
        return []
    anchor_entries = {entry["path"]: entry for entry in snapshots[0]["file_baselines"]}
    floors = {path: int(entry["physical_lines"]) for path, entry in anchor_entries.items()}
    retired: set[str] = set()
    failures: list[dict[str, Any]] = []
    for snapshot in snapshots[1:]:
        present = {entry["path"] for entry in snapshot["file_baselines"]}
        retired.update(set(anchor_entries) - present)
        for entry in snapshot["file_baselines"]:
            path = entry["path"]
            if path not in anchor_entries:
                continue
            floors[path] = min(floors[path], int(entry["physical_lines"]))
    for path, observations in (source_counts or {}).items():
        if path not in anchor_entries:
            continue
        for count in observations:
            if count is None or count <= 400:
                retired.add(path)
            else:
                floors[path] = min(floors[path], count)
    for entry in current["file_baselines"]:
        path = entry["path"]
        lines = int(entry["physical_lines"])
        if path not in anchor_entries:
            failures.append({"code": "ratchet_new_trigger_path", "path": path})
            continue
        if path in retired:
            failures.append({"code": "ratchet_reintroduced_trigger_path", "path": path})
        if entry["kind"] != anchor_entries[path]["kind"]:
            failures.append(
                {
                    "code": "ratchet_kind_drift",
                    "path": path,
                    "expected": anchor_entries[path]["kind"],
                    "actual": entry["kind"],
                }
            )
        if lines > floors[path]:
            failures.append(
                {
                    "code": "ratchet_physical_line_increase",
                    "path": path,
                    "historical_minimum": floors[path],
                    "current": lines,
                }
            )
    return failures
