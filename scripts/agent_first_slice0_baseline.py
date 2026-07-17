#!/usr/bin/env python3
"""Verify the architecture-neutral Worker Slice 0 evidence baseline."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import stat
import subprocess
import sys
from pathlib import Path, PurePosixPath
from typing import Any, Iterable


SCHEMA_ID = "pullwise-agent-first-slice-0-baseline/v1"
REPORT_SCHEMA_ID = "pullwise-agent-first-slice-0-baseline-report/v1"
LINE_COUNT_PROFILE = "physical-lf/v1"
REVIEW_TRIGGER = 400
OVERSIZED_LIMIT = 600
HANDWRITTEN_SUFFIXES = {
    ".bash",
    ".cjs",
    ".js",
    ".jsx",
    ".mjs",
    ".ps1",
    ".py",
    ".pyi",
    ".sh",
    ".ts",
    ".tsx",
    ".zsh",
}
TOP_LEVEL_KEYS = {
    "schema_id",
    "baseline_id",
    "captured_head",
    "line_count_profile",
    "document",
    "pipeline",
    "code_map",
    "file_baselines",
}


class BaselineFormatError(ValueError):
    pass


class BaselineObservationError(RuntimeError):
    pass


def physical_line_count(data: bytes) -> int:
    normalized = data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    if not normalized:
        return 0
    return normalized.count(b"\n") + int(not normalized.endswith(b"\n"))


def _exact_keys(value: object, expected: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != expected:
        raise BaselineFormatError(f"{label}:keys")
    return value


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or "\n" in value or "\r" in value:
        raise BaselineFormatError(f"{label}:text")
    return value


def _text_list(value: object, label: str) -> list[str]:
    if not isinstance(value, list) or not value:
        raise BaselineFormatError(f"{label}:list")
    result = [_text(item, f"{label}[]") for item in value]
    if len(result) != len(set(result)):
        raise BaselineFormatError(f"{label}:duplicate")
    return result


def _relative_path(value: object, label: str) -> str:
    text = _text(value, label)
    path = PurePosixPath(text)
    if path.is_absolute() or text != path.as_posix() or any(part in {"", ".", ".."} for part in path.parts):
        raise BaselineFormatError(f"{label}:relative_path")
    return text


def validate_baseline(baseline: object) -> dict[str, Any]:
    root = _exact_keys(baseline, TOP_LEVEL_KEYS, "baseline")
    if root["schema_id"] != SCHEMA_ID or root["line_count_profile"] != LINE_COUNT_PROFILE:
        raise BaselineFormatError("baseline:profile")
    _text(root["baseline_id"], "baseline_id")
    head = _text(root["captured_head"], "captured_head")
    if len(head) != 40 or any(char not in "0123456789abcdef" for char in head):
        raise BaselineFormatError("captured_head:sha1")

    document = _exact_keys(root["document"], {"path", "start_marker", "end_marker"}, "document")
    _relative_path(document["path"], "document.path")
    start = _text(document["start_marker"], "document.start_marker")
    end = _text(document["end_marker"], "document.end_marker")
    if start == end:
        raise BaselineFormatError("document:markers")

    pipeline = _exact_keys(root["pipeline"], {"path", "symbol", "values"}, "pipeline")
    _relative_path(pipeline["path"], "pipeline.path")
    _text(pipeline["symbol"], "pipeline.symbol")
    if not isinstance(pipeline["values"], list) or not pipeline["values"]:
        raise BaselineFormatError("pipeline.values:list")
    phase_names: list[str] = []
    for index, item in enumerate(pipeline["values"]):
        if not isinstance(item, list) or len(item) != 2:
            raise BaselineFormatError(f"pipeline.values[{index}]:pair")
        phase_names.append(_text(item[0], f"pipeline.values[{index}].name"))
        if not isinstance(item[1], int) or isinstance(item[1], bool) or not 0 <= item[1] <= 100:
            raise BaselineFormatError(f"pipeline.values[{index}].progress")
    if len(phase_names) != len(set(phase_names)):
        raise BaselineFormatError("pipeline.values:duplicate")

    if not isinstance(root["code_map"], list) or not root["code_map"]:
        raise BaselineFormatError("code_map:list")
    map_ids: list[str] = []
    for index, entry in enumerate(root["code_map"]):
        item = _exact_keys(
            entry,
            {"id", "paths", "current_responsibilities", "boundary", "candidate_extraction_seam"},
            f"code_map[{index}]",
        )
        map_ids.append(_text(item["id"], f"code_map[{index}].id"))
        for field in ("current_responsibilities", "boundary", "candidate_extraction_seam"):
            _text(item[field], f"code_map[{index}].{field}")
        if not isinstance(item["paths"], list) or not item["paths"]:
            raise BaselineFormatError(f"code_map[{index}].paths:list")
        for path_index, path_entry in enumerate(item["paths"]):
            source = _exact_keys(path_entry, {"path", "anchors"}, f"code_map[{index}].paths[{path_index}]")
            _relative_path(source["path"], f"code_map[{index}].paths[{path_index}].path")
            _text_list(source["anchors"], f"code_map[{index}].paths[{path_index}].anchors")
    if len(map_ids) != len(set(map_ids)):
        raise BaselineFormatError("code_map:duplicate_id")

    if not isinstance(root["file_baselines"], list) or not root["file_baselines"]:
        raise BaselineFormatError("file_baselines:list")
    paths: list[str] = []
    for index, entry in enumerate(root["file_baselines"]):
        item = _exact_keys(
            entry,
            {"path", "kind", "classification", "physical_lines", "anchors", "current_responsibilities", "candidate_extraction_seam"},
            f"file_baselines[{index}]",
        )
        paths.append(_relative_path(item["path"], f"file_baselines[{index}].path"))
        if item["kind"] not in {"production", "test", "maintenance_script"}:
            raise BaselineFormatError(f"file_baselines[{index}].kind")
        lines = item["physical_lines"]
        if not isinstance(lines, int) or isinstance(lines, bool) or lines <= REVIEW_TRIGGER:
            raise BaselineFormatError(f"file_baselines[{index}].physical_lines")
        expected_class = "oversized_legacy" if lines > OVERSIZED_LIMIT else "review_trigger_existing"
        if item["classification"] != expected_class:
            raise BaselineFormatError(f"file_baselines[{index}].classification")
        _text_list(item["anchors"], f"file_baselines[{index}].anchors")
        _text(item["current_responsibilities"], f"file_baselines[{index}].current_responsibilities")
        _text(item["candidate_extraction_seam"], f"file_baselines[{index}].candidate_extraction_seam")
    if len(paths) != len(set(paths)):
        raise BaselineFormatError("file_baselines:duplicate_path")
    expected_order = sorted(root["file_baselines"], key=lambda item: (-item["physical_lines"], item["path"]))
    if root["file_baselines"] != expected_order:
        raise BaselineFormatError("file_baselines:order")
    return root


def load_baseline(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BaselineFormatError(f"manifest_unreadable:{exc}") from exc
    return validate_baseline(value)


def _repo_path(repo_root: Path, relative: str) -> Path:
    return repo_root.joinpath(*PurePosixPath(relative).parts)


def _regular_bytes(repo_root: Path, relative: str) -> bytes:
    path = _repo_path(repo_root, relative)
    try:
        mode = os.lstat(path).st_mode
        if not stat.S_ISREG(mode):
            raise BaselineObservationError(f"not_regular:{relative}")
        return path.read_bytes()
    except OSError as exc:
        raise BaselineObservationError(f"unreadable:{relative}:{exc}") from exc


def _canonical_text(repo_root: Path, relative: str) -> str:
    try:
        return _regular_bytes(repo_root, relative).decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
    except UnicodeError as exc:
        raise BaselineObservationError(f"not_utf8:{relative}") from exc


def _git(repo_root: Path, *args: str) -> bytes:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), *args],
            check=False,
            capture_output=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BaselineObservationError(f"git_unavailable:{args[0]}:{exc}") from exc
    if result.returncode != 0:
        raise BaselineObservationError(f"git_failed:{args[0]}:{result.returncode}")
    return result.stdout


def _tracked_paths(repo_root: Path) -> tuple[str, ...]:
    try:
        values = _git(repo_root, "ls-files", "-z").decode("utf-8").split("\0")
    except UnicodeError as exc:
        raise BaselineObservationError("git_paths_not_utf8") from exc
    return tuple(sorted(value.replace("\\", "/") for value in values if value))


def _pipeline_values(text: str, symbol: str) -> list[list[Any]] | None:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return None
    for node in tree.body:
        targets: list[ast.expr] = []
        value: ast.expr | None = None
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign):
            targets, value = [node.target], node.value
        if value is not None and any(isinstance(target, ast.Name) and target.id == symbol for target in targets):
            try:
                raw = ast.literal_eval(value)
                return [[str(name), int(progress)] for name, progress in raw]
            except (TypeError, ValueError, SyntaxError):
                return None
    return None


def _markdown(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def render_document(baseline: dict[str, Any]) -> str:
    validate_baseline(baseline)
    lines = [
        f"> Generated from `{baseline['baseline_id']}` with `{LINE_COUNT_PROFILE}`. Do not edit this block by hand.",
        "",
        f"Captured Worker HEAD `{baseline['captured_head']}` is informational only. This is current-implementation evidence; it does not assign future Agent Kernel ownership or authorize production implementation.",
        "",
        "### Current implementation map",
        "",
        "| Current scope | Paths | Current responsibilities | Ownership/call boundary | Candidate extraction seam |",
        "|---|---|---|---|---|",
    ]
    for entry in baseline["code_map"]:
        paths = ", ".join(f"`{source['path']}`" for source in entry["paths"])
        lines.append(
            f"| `{entry['id']}` | {paths} | {_markdown(entry['current_responsibilities'])} | {_markdown(entry['boundary'])} | {_markdown(entry['candidate_extraction_seam'])} |"
        )
    lines.extend(["", "### Current fixed pipeline", "", "| Order | Phase | Progress ceiling |", "|---:|---|---:|"])
    for index, (phase, progress) in enumerate(baseline["pipeline"]["values"], start=1):
        lines.append(f"| {index} | `{phase}` | {progress} |")
    lines.extend(
        [
            "",
            "### Handwritten file-size ratchet",
            "",
            "The inventory covers every Git-tracked handwritten code/script suffix above 400 physical lines. `oversized_legacy` is the >600 grandfathered baseline; `review_trigger_existing` is the existing 401–600 review-trigger range. Any count drift or unregistered trigger file fails verification.",
            "",
            "| Path | Kind | Classification | Physical lines | Current responsibilities | Candidate extraction seam |",
            "|---|---|---|---:|---|---|",
        ]
    )
    for entry in baseline["file_baselines"]:
        lines.append(
            f"| `{entry['path']}` | `{entry['kind']}` | `{entry['classification']}` | {entry['physical_lines']} | {_markdown(entry['current_responsibilities'])} | {_markdown(entry['candidate_extraction_seam'])} |"
        )
    return "\n".join(lines)


def _document_matches(baseline: dict[str, Any], repo_root: Path) -> bool:
    document = baseline["document"]
    text = _canonical_text(repo_root, document["path"])
    start, end = document["start_marker"], document["end_marker"]
    if text.count(start) != 1 or text.count(end) != 1:
        return False
    start_index = text.index(start) + len(start)
    end_index = text.index(end, start_index)
    if end_index <= start_index:
        return False
    return text[start_index:end_index].strip("\n") == render_document(baseline)


def verify_baseline(
    baseline: dict[str, Any],
    repo_root: Path,
    *,
    tracked_paths: Iterable[str] | None = None,
    check_document: bool = True,
) -> dict[str, Any]:
    validate_baseline(baseline)
    root = repo_root.resolve()
    failures: list[dict[str, Any]] = []
    try:
        tracked = tuple(tracked_paths) if tracked_paths is not None else _tracked_paths(root)
        current_head = _git(root, "rev-parse", "HEAD").decode("ascii").strip() if tracked_paths is None else None
        current_dirty = bool(_git(root, "status", "--porcelain=v1", "--untracked-files=all")) if tracked_paths is None else None
    except (BaselineObservationError, UnicodeError) as exc:
        return {
            "schema_id": REPORT_SCHEMA_ID,
            "baseline_id": baseline["baseline_id"],
            "status": "indeterminate",
            "compatible": False,
            "failures": [],
            "indeterminate_reasons": [{"code": str(exc)}],
        }

    text_cache: dict[str, str] = {}
    for entry in baseline["code_map"]:
        for source in entry["paths"]:
            path = source["path"]
            try:
                text = text_cache.setdefault(path, _canonical_text(root, path))
            except BaselineObservationError as exc:
                failures.append({"code": "code_map_source_unreadable", "path": path, "detail": str(exc)})
                continue
            for anchor in source["anchors"]:
                if anchor not in text:
                    failures.append({"code": "code_map_anchor_missing", "map_id": entry["id"], "path": path, "anchor": anchor})

    pipeline = baseline["pipeline"]
    try:
        pipeline_text = text_cache.setdefault(pipeline["path"], _canonical_text(root, pipeline["path"]))
        actual_pipeline = _pipeline_values(pipeline_text, pipeline["symbol"])
    except BaselineObservationError:
        actual_pipeline = None
    if actual_pipeline != pipeline["values"]:
        failures.append({"code": "pipeline_registry_drift", "path": pipeline["path"], "symbol": pipeline["symbol"]})

    actual_trigger: dict[str, int] = {}
    for path in tracked:
        normalized = path.replace("\\", "/")
        if PurePosixPath(normalized).suffix.lower() not in HANDWRITTEN_SUFFIXES:
            continue
        try:
            count = physical_line_count(_regular_bytes(root, normalized))
        except BaselineObservationError as exc:
            failures.append({"code": "tracked_handwritten_file_unreadable", "path": normalized, "detail": str(exc)})
            continue
        if count > REVIEW_TRIGGER:
            actual_trigger[normalized] = count

    expected = {entry["path"]: entry for entry in baseline["file_baselines"]}
    for path in sorted(set(actual_trigger) - set(expected)):
        failures.append({"code": "trigger_file_missing_from_baseline", "path": path})
    for path in sorted(set(expected) - set(actual_trigger)):
        failures.append({"code": "baseline_entry_not_trigger_file", "path": path})
    for path in sorted(set(actual_trigger) & set(expected)):
        entry = expected[path]
        actual = actual_trigger[path]
        if actual != entry["physical_lines"]:
            failures.append({"code": "physical_line_count_drift", "path": path, "expected": entry["physical_lines"], "actual": actual})
        actual_class = "oversized_legacy" if actual > OVERSIZED_LIMIT else "review_trigger_existing"
        if actual_class != entry["classification"]:
            failures.append({"code": "file_classification_drift", "path": path, "expected": entry["classification"], "actual": actual_class})
        try:
            text = text_cache.setdefault(path, _canonical_text(root, path))
        except BaselineObservationError:
            continue
        for anchor in entry["anchors"]:
            if anchor not in text:
                failures.append({"code": "file_baseline_anchor_missing", "path": path, "anchor": anchor})

    document_matches = False
    if check_document:
        try:
            document_matches = _document_matches(baseline, root)
        except BaselineObservationError:
            document_matches = False
        if not document_matches:
            failures.append({"code": "generated_document_drift", "path": baseline["document"]["path"]})

    failures.sort(key=lambda item: json.dumps(item, sort_keys=True))
    manifest_bytes = (json.dumps(baseline, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    return {
        "schema_id": REPORT_SCHEMA_ID,
        "baseline_id": baseline["baseline_id"],
        "status": "incompatible" if failures else "compatible",
        "compatible": not failures,
        "captured_head": baseline["captured_head"],
        "current_head": current_head,
        "head_status": "informational",
        "current_worktree_dirty": current_dirty,
        "worktree_status": "informational",
        "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "document_matches": document_matches if check_document else None,
        "pipeline_phase_count": len(actual_pipeline or []),
        "oversized_legacy_count": sum(count > OVERSIZED_LIMIT for count in actual_trigger.values()),
        "review_trigger_count": sum(REVIEW_TRIGGER < count <= OVERSIZED_LIMIT for count in actual_trigger.values()),
        "failures": failures,
        "indeterminate_reasons": [],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    check = subparsers.add_parser("check")
    check.add_argument("--repo-root", default=".")
    check.add_argument("--manifest", default="contracts/agent-first/worker-slice-0-baseline.json")
    render = subparsers.add_parser("render-document")
    render.add_argument("--manifest", default="contracts/agent-first/worker-slice-0-baseline.json")
    args = parser.parse_args(argv)
    try:
        baseline = load_baseline(Path(args.manifest))
        if args.command == "render-document":
            print(render_document(baseline))
            return 0
        report = verify_baseline(baseline, Path(args.repo_root))
    except BaselineFormatError as exc:
        report = {
            "schema_id": REPORT_SCHEMA_ID,
            "status": "incompatible",
            "compatible": False,
            "failures": [{"code": "manifest_invalid", "detail": str(exc)}],
            "indeterminate_reasons": [],
        }
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["status"] == "compatible" else 1 if report["status"] == "incompatible" else 2


if __name__ == "__main__":
    raise SystemExit(main())
