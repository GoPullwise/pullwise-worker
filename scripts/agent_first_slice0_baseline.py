#!/usr/bin/env python3
"""Verify the architecture-neutral Worker Slice 0 evidence baseline."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

try:
    from scripts.agent_first_slice0_gate import (
        GateObservationError,
        git_bytes,
        historical_ratchet_evidence,
        is_handwritten,
        physical_line_count,
        pipeline_values,
        ratchet_failures,
        tracked_handwritten_files,
    )
    from scripts.agent_first_slice0_render import render_document
except ModuleNotFoundError:
    from agent_first_slice0_gate import (  # type: ignore[no-redef]
        GateObservationError,
        git_bytes,
        historical_ratchet_evidence,
        is_handwritten,
        physical_line_count,
        pipeline_values,
        ratchet_failures,
        tracked_handwritten_files,
    )
    from agent_first_slice0_render import render_document  # type: ignore[no-redef]


SCHEMA_ID = "pullwise-agent-first-slice-0-baseline/v1"
REPORT_SCHEMA_ID = "pullwise-agent-first-slice-0-baseline-report/v1"
LINE_COUNT_PROFILE = "physical-lf/v1"
REVIEW_TRIGGER = 400
OVERSIZED_LIMIT = 600
TOP_LEVEL_KEYS = {
    "schema_id", "baseline_id", "captured_head", "line_count_profile",
    "document", "pipeline", "code_map", "file_baselines",
}


class BaselineFormatError(ValueError):
    pass


class BaselineObservationError(RuntimeError):
    pass


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
    if (
        chr(92) in text
        or "\0" in text
        or ":" in text
        or path.is_absolute()
        or text != path.as_posix()
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
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
    root = repo_root.resolve()
    candidate = root.joinpath(*PurePosixPath(relative).parts)
    try:
        candidate.resolve(strict=False).relative_to(root)
    except (OSError, ValueError) as exc:
        raise BaselineObservationError(f"outside_repo:{relative}") from exc
    return candidate


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


def _document_matches(baseline: dict[str, Any], repo_root: Path) -> bool:
    document = baseline["document"]
    text = _canonical_text(repo_root, document["path"])
    start, end = document["start_marker"], document["end_marker"]
    if text.count(start) != 1 or text.count(end) != 1:
        return False
    start_token = f"{start}\n"
    end_token = f"\n{end}"
    start_index = text.find(start_token)
    if start_index < 0 or (start_index > 0 and text[start_index - 1] != "\n"):
        return False
    body_start = start_index + len(start_token)
    end_index = text.find(end_token, body_start)
    if end_index < body_start:
        return False
    marker_end = end_index + len(end_token)
    if marker_end < len(text) and text[marker_end] != "\n":
        return False
    return text[body_start:end_index] == render_document(baseline)


def verify_baseline(
    baseline: dict[str, Any],
    repo_root: Path,
    *,
    tracked_paths: Iterable[str] | None = None,
    tracked_executable_paths: Iterable[str] = (),
    ratchet_baselines: Iterable[dict[str, Any]] | None = None,
    ratchet_source_counts: dict[str, Iterable[int | None]] | None = None,
    check_document: bool = True,
) -> dict[str, Any]:
    validate_baseline(baseline)
    root = repo_root.resolve()
    failures: list[dict[str, Any]] = []
    try:
        if tracked_paths is None:
            inventory = tracked_handwritten_files(root)
            tracked = tuple(path for path, _executable in inventory)
            executable_paths = {path for path, executable in inventory if executable}
            current_head = git_bytes(root, "rev-parse", "HEAD").decode("ascii").strip()
            current_dirty = bool(git_bytes(root, "status", "--porcelain=v1", "--untracked-files=all"))
            history, source_history = historical_ratchet_evidence(root, baseline["baseline_id"], current_head)
        else:
            tracked = tuple(tracked_paths)
            executable_paths = set(tracked_executable_paths)
            current_head = current_dirty = None
            history = tuple(ratchet_baselines or ())
            source_history = dict(ratchet_source_counts or {})
        history = tuple(validate_baseline(snapshot) for snapshot in history)
    except (BaselineFormatError, BaselineObservationError, GateObservationError, UnicodeError) as exc:
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
        actual_pipeline = pipeline_values(pipeline_text, pipeline["symbol"])
    except BaselineObservationError:
        actual_pipeline = None
    if actual_pipeline != pipeline["values"]:
        failures.append({"code": "pipeline_registry_drift", "path": pipeline["path"], "symbol": pipeline["symbol"]})

    actual_trigger: dict[str, int] = {}
    for path in tracked:
        normalized = path
        if not is_handwritten(normalized, executable=normalized in executable_paths):
            continue
        try:
            count = physical_line_count(_regular_bytes(root, normalized))
        except BaselineObservationError as exc:
            failures.append({"code": "tracked_handwritten_file_unreadable", "path": normalized, "detail": str(exc)})
            continue
        if count > REVIEW_TRIGGER:
            actual_trigger[normalized] = count

    failures.extend(ratchet_failures(baseline, history, source_history))

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
        "ratchet_history_count": len(history),
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
