"""Load and validate the architecture-neutral Worker Slice 0 baseline manifest."""

from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
from typing import Any


LEGACY_SCHEMA_ID = "pullwise-agent-first-slice-0-baseline/v1"
SCHEMA_ID = "pullwise-agent-first-slice-0-baseline/v2"
LINE_COUNT_PROFILE = "physical-lf/v1"
REVIEW_TRIGGER = 400
OVERSIZED_LIMIT = 600
LEGACY_TOP_LEVEL_KEYS = {
    "schema_id",
    "baseline_id",
    "captured_head",
    "line_count_profile",
    "document",
    "pipeline",
    "code_map",
    "file_baselines",
}
TOP_LEVEL_KEYS = LEGACY_TOP_LEVEL_KEYS | {"generated_file_exceptions"}
GENERATED_FILE_CATALOG = {
    "pullwise_worker/_generated_agent_task_contract.py": {
        "marker": '"""Generated from the Server-owned Agent-First bundle; do not edit."""',
        "provenance": (
            "pullwise-server@e997688beb7ec9d071d3e3c20e2685edd98c36fc:"
            "pullwise_server/agent_first_contract_bundle_python.py"
        ),
    },
}
GENERATED_EXCEPTION_KEYS = {
    "path",
    "physical_lines",
    "sha256",
    "marker",
    "provenance",
    "reason",
    "considered_split_seam",
    "owner",
    "removal_condition",
}


class BaselineFormatError(ValueError):
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


def validate_baseline(
    baseline: object,
    *,
    require_current_generated_provenance: bool = True,
) -> dict[str, Any]:
    if not isinstance(baseline, dict):
        raise BaselineFormatError("baseline:keys")
    if baseline.get("schema_id") == LEGACY_SCHEMA_ID:
        root = dict(_exact_keys(baseline, LEGACY_TOP_LEVEL_KEYS, "baseline"))
        root["generated_file_exceptions"] = []
    else:
        root = _exact_keys(baseline, TOP_LEVEL_KEYS, "baseline")
    if (
        root["schema_id"] not in {LEGACY_SCHEMA_ID, SCHEMA_ID}
        or root["line_count_profile"] != LINE_COUNT_PROFILE
    ):
        raise BaselineFormatError("baseline:profile")
    _text(root["baseline_id"], "baseline_id")
    head = _text(root["captured_head"], "captured_head")
    if len(head) != 40 or any(char not in "0123456789abcdef" for char in head):
        raise BaselineFormatError("captured_head:sha1")

    document = _exact_keys(
        root["document"],
        {"path", "start_marker", "end_marker"},
        "document",
    )
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
        if (
            not isinstance(item[1], int)
            or isinstance(item[1], bool)
            or not 0 <= item[1] <= 100
        ):
            raise BaselineFormatError(f"pipeline.values[{index}].progress")
    if len(phase_names) != len(set(phase_names)):
        raise BaselineFormatError("pipeline.values:duplicate")

    if not isinstance(root["code_map"], list) or not root["code_map"]:
        raise BaselineFormatError("code_map:list")
    map_ids: list[str] = []
    for index, entry in enumerate(root["code_map"]):
        item = _exact_keys(
            entry,
            {
                "id",
                "paths",
                "current_responsibilities",
                "boundary",
                "candidate_extraction_seam",
            },
            f"code_map[{index}]",
        )
        map_ids.append(_text(item["id"], f"code_map[{index}].id"))
        for field in ("current_responsibilities", "boundary", "candidate_extraction_seam"):
            _text(item[field], f"code_map[{index}].{field}")
        if not isinstance(item["paths"], list) or not item["paths"]:
            raise BaselineFormatError(f"code_map[{index}].paths:list")
        for path_index, path_entry in enumerate(item["paths"]):
            source = _exact_keys(
                path_entry,
                {"path", "anchors"},
                f"code_map[{index}].paths[{path_index}]",
            )
            _relative_path(
                source["path"],
                f"code_map[{index}].paths[{path_index}].path",
            )
            _text_list(source["anchors"], f"code_map[{index}].paths[{path_index}].anchors")
    if len(map_ids) != len(set(map_ids)):
        raise BaselineFormatError("code_map:duplicate_id")

    if not isinstance(root["file_baselines"], list) or not root["file_baselines"]:
        raise BaselineFormatError("file_baselines:list")
    paths: list[str] = []
    for index, entry in enumerate(root["file_baselines"]):
        item = _exact_keys(
            entry,
            {
                "path",
                "kind",
                "classification",
                "physical_lines",
                "anchors",
                "current_responsibilities",
                "candidate_extraction_seam",
            },
            f"file_baselines[{index}]",
        )
        paths.append(_relative_path(item["path"], f"file_baselines[{index}].path"))
        if item["kind"] not in {"production", "test", "maintenance_script"}:
            raise BaselineFormatError(f"file_baselines[{index}].kind")
        lines = item["physical_lines"]
        if not isinstance(lines, int) or isinstance(lines, bool) or lines <= REVIEW_TRIGGER:
            raise BaselineFormatError(f"file_baselines[{index}].physical_lines")
        expected_class = (
            "oversized_legacy"
            if lines > OVERSIZED_LIMIT
            else "review_trigger_existing"
        )
        if item["classification"] != expected_class:
            raise BaselineFormatError(f"file_baselines[{index}].classification")
        _text_list(item["anchors"], f"file_baselines[{index}].anchors")
        _text(
            item["current_responsibilities"],
            f"file_baselines[{index}].current_responsibilities",
        )
        _text(
            item["candidate_extraction_seam"],
            f"file_baselines[{index}].candidate_extraction_seam",
        )
    if len(paths) != len(set(paths)):
        raise BaselineFormatError("file_baselines:duplicate_path")
    expected_order = sorted(
        root["file_baselines"],
        key=lambda item: (-item["physical_lines"], item["path"]),
    )
    if root["file_baselines"] != expected_order:
        raise BaselineFormatError("file_baselines:order")

    exceptions = root["generated_file_exceptions"]
    if not isinstance(exceptions, list):
        raise BaselineFormatError("generated_file_exceptions:list")
    exception_paths: list[str] = []
    for index, entry in enumerate(exceptions):
        item = _exact_keys(
            entry,
            GENERATED_EXCEPTION_KEYS,
            f"generated_file_exceptions[{index}]",
        )
        path = _relative_path(item["path"], f"generated_file_exceptions[{index}].path")
        exception_paths.append(path)
        catalog = GENERATED_FILE_CATALOG.get(path)
        if catalog is None:
            raise BaselineFormatError(f"generated_file_exceptions[{index}].path")
        if item["marker"] != catalog["marker"]:
            raise BaselineFormatError(f"generated_file_exceptions[{index}].marker")
        provenance_path = f"generated_file_exceptions[{index}].provenance"
        if require_current_generated_provenance:
            if item["provenance"] != catalog["provenance"]:
                raise BaselineFormatError(provenance_path)
        else:
            _text(item["provenance"], provenance_path)
        lines = item["physical_lines"]
        if (
            not isinstance(lines, int)
            or isinstance(lines, bool)
            or lines <= REVIEW_TRIGGER
        ):
            raise BaselineFormatError(f"generated_file_exceptions[{index}].physical_lines")
        digest = item["sha256"]
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(char not in "0123456789abcdef" for char in digest)
        ):
            raise BaselineFormatError(f"generated_file_exceptions[{index}].sha256")
        for field in ("reason", "considered_split_seam", "owner", "removal_condition"):
            _text(item[field], f"generated_file_exceptions[{index}].{field}")
    if len(exception_paths) != len(set(exception_paths)):
        raise BaselineFormatError("generated_file_exceptions:duplicate_path")
    if exception_paths != sorted(exception_paths):
        raise BaselineFormatError("generated_file_exceptions:order")
    if set(exception_paths) & set(paths):
        raise BaselineFormatError("generated_file_exceptions:file_baseline_overlap")
    return root


def load_baseline(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BaselineFormatError(f"manifest_unreadable:{exc}") from exc
    return validate_baseline(value)
