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
    from scripts.agent_first_slice0_manifest import (
        BaselineFormatError,
        OVERSIZED_LIMIT,
        REVIEW_TRIGGER,
        load_baseline,
        validate_baseline,
    )
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
    from agent_first_slice0_manifest import (  # type: ignore[no-redef]
        BaselineFormatError,
        OVERSIZED_LIMIT,
        REVIEW_TRIGGER,
        load_baseline,
        validate_baseline,
    )
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

REPORT_SCHEMA_ID = "pullwise-agent-first-slice-0-baseline-report/v1"


class BaselineObservationError(RuntimeError):
    pass


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
        return (
            _regular_bytes(repo_root, relative)
            .decode("utf-8")
            .replace("\r\n", "\n")
            .replace("\r", "\n")
        )
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
    baseline = validate_baseline(baseline)
    root = repo_root.resolve()
    failures: list[dict[str, Any]] = []
    try:
        if tracked_paths is None:
            inventory = tracked_handwritten_files(root)
            tracked = tuple(path for path, _executable in inventory)
            executable_paths = {path for path, executable in inventory if executable}
            current_head = git_bytes(root, "rev-parse", "HEAD").decode("ascii").strip()
            current_dirty = bool(
                git_bytes(root, "status", "--porcelain=v1", "--untracked-files=all")
            )
            history, source_history = historical_ratchet_evidence(
                root,
                baseline["baseline_id"],
                current_head,
            )
        else:
            tracked = tuple(tracked_paths)
            executable_paths = set(tracked_executable_paths)
            current_head = current_dirty = None
            history = tuple(ratchet_baselines or ())
            source_history = dict(ratchet_source_counts or {})
        history = tuple(validate_baseline(snapshot) for snapshot in history)
    except (
        BaselineFormatError,
        BaselineObservationError,
        GateObservationError,
        UnicodeError,
    ) as exc:
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
                failures.append(
                    {
                        "code": "code_map_source_unreadable",
                        "path": path,
                        "detail": str(exc),
                    }
                )
                continue
            for anchor in source["anchors"]:
                if anchor not in text:
                    failures.append(
                        {
                            "code": "code_map_anchor_missing",
                            "map_id": entry["id"],
                            "path": path,
                            "anchor": anchor,
                        }
                    )

    pipeline = baseline["pipeline"]
    try:
        pipeline_text = text_cache.setdefault(
            pipeline["path"],
            _canonical_text(root, pipeline["path"]),
        )
        actual_pipeline = pipeline_values(pipeline_text, pipeline["symbol"])
    except BaselineObservationError:
        actual_pipeline = None
    if actual_pipeline != pipeline["values"]:
        failures.append(
            {
                "code": "pipeline_registry_drift",
                "path": pipeline["path"],
                "symbol": pipeline["symbol"],
            }
        )

    actual_trigger: dict[str, int] = {}
    generated_paths = {entry["path"] for entry in baseline["generated_file_exceptions"]}
    for entry in baseline["generated_file_exceptions"]:
        path = entry["path"]
        if path not in tracked:
            failures.append({"code": "generated_exception_path_untracked", "path": path})
            continue
        try:
            data = _regular_bytes(root, path)
        except BaselineObservationError as exc:
            failures.append(
                {
                    "code": "generated_exception_unreadable",
                    "path": path,
                    "detail": str(exc),
                }
            )
            continue
        actual = physical_line_count(data)
        if actual != entry["physical_lines"]:
            failures.append(
                {
                    "code": "generated_exception_line_count_mismatch",
                    "path": path,
                    "expected": entry["physical_lines"],
                    "actual": actual,
                }
            )
        if not data.startswith((entry["marker"] + "\n").encode("utf-8")):
            failures.append({"code": "generated_exception_marker_mismatch", "path": path})
        if hashlib.sha256(data).hexdigest() != entry["sha256"]:
            failures.append({"code": "generated_exception_digest_mismatch", "path": path})
    for path in tracked:
        normalized = path
        if normalized in generated_paths:
            continue
        if not is_handwritten(normalized, executable=normalized in executable_paths):
            continue
        try:
            count = physical_line_count(_regular_bytes(root, normalized))
        except BaselineObservationError as exc:
            failures.append(
                {
                    "code": "tracked_handwritten_file_unreadable",
                    "path": normalized,
                    "detail": str(exc),
                }
            )
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
            failures.append(
                {
                    "code": "physical_line_count_drift",
                    "path": path,
                    "expected": entry["physical_lines"],
                    "actual": actual,
                }
            )
        actual_class = "oversized_legacy" if actual > OVERSIZED_LIMIT else "review_trigger_existing"
        if actual_class != entry["classification"]:
            failures.append(
                {
                    "code": "file_classification_drift",
                    "path": path,
                    "expected": entry["classification"],
                    "actual": actual_class,
                }
            )
        try:
            text = text_cache.setdefault(path, _canonical_text(root, path))
        except BaselineObservationError:
            continue
        for anchor in entry["anchors"]:
            if anchor not in text:
                failures.append(
                    {
                        "code": "file_baseline_anchor_missing",
                        "path": path,
                        "anchor": anchor,
                    }
                )

    document_matches = False
    if check_document:
        try:
            document_matches = _document_matches(baseline, root)
        except BaselineObservationError:
            document_matches = False
        if not document_matches:
            failures.append(
                {
                    "code": "generated_document_drift",
                    "path": baseline["document"]["path"],
                }
            )

    failures.sort(key=lambda item: json.dumps(item, sort_keys=True))
    manifest_bytes = (
        json.dumps(
            baseline,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
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
        "review_trigger_count": sum(
            REVIEW_TRIGGER < count <= OVERSIZED_LIMIT
            for count in actual_trigger.values()
        ),
        "generated_exception_count": len(baseline["generated_file_exceptions"]),
        "ratchet_history_count": len(history),
        "failures": failures,
        "indeterminate_reasons": [],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    check = subparsers.add_parser("check")
    check.add_argument("--repo-root", default=".")
    check.add_argument(
        "--manifest",
        default="contracts/agent-first/worker-slice-0-baseline.json",
    )
    render = subparsers.add_parser("render-document")
    render.add_argument(
        "--manifest",
        default="contracts/agent-first/worker-slice-0-baseline.json",
    )
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
