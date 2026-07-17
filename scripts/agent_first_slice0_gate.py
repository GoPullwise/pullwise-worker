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


def tracked_handwritten_files(repo_root: Path) -> tuple[tuple[str, bool], ...]:
    try:
        records = git_bytes(repo_root, "ls-files", "--stage", "-z").decode("utf-8").split("\0")
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
        path = raw_path.replace(chr(92), "/")
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
    assignments: list[ast.expr] = []
    for node in tree.body:
        if isinstance(node, ast.Assign):
            if any(isinstance(target, ast.Name) and target.id == symbol for target in node.targets):
                assignments.append(node.value)
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and node.target.id == symbol and node.value is not None:
                assignments.append(node.value)
        elif isinstance(node, (ast.AugAssign, ast.Delete)):
            target = node.target if isinstance(node, ast.AugAssign) else None
            if isinstance(target, ast.Name) and target.id == symbol:
                return None
    if len(assignments) != 1:
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


def historical_ratchet_baselines(
    repo_root: Path,
    baseline_id: str,
    head: str,
) -> tuple[dict[str, Any], ...]:
    try:
        anchor, manifest_path = RATCHET_ANCHORS[baseline_id]
    except KeyError as exc:
        raise GateObservationError(f"ratchet_anchor_unknown:{baseline_id}") from exc
    git_bytes(repo_root, "merge-base", "--is-ancestor", anchor, head)
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
            manifest_path,
        ).decode("ascii").splitlines()
    except UnicodeError as exc:
        raise GateObservationError("ratchet_history_not_ascii") from exc
    revisions = [anchor, *(revision for revision in changed if revision != anchor)]
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
    return tuple(baselines)


def ratchet_failures(
    current: dict[str, Any],
    historical: Iterable[dict[str, Any]],
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
