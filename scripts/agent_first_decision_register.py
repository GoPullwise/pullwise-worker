#!/usr/bin/env python3
"""Validate and render the Agent-First specification decision register."""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
from pathlib import Path, PurePosixPath
from typing import Any


SCHEMA_ID = "pullwise-agent-first-spec-decision-register/v1"
REPORT_SCHEMA_ID = "pullwise-agent-first-spec-decision-register-report/v1"
ALLOWED_EFFECTS = {
    "authority",
    "compatibility",
    "data_model",
    "external_behavior",
    "permission",
    "release_ownership",
    "state_semantics",
}
ALLOWED_SLICES = {f"S{index}" for index in range(2, 9)}
TOP_LEVEL_KEYS = {
    "schema_id",
    "register_id",
    "active_decision_id",
    "document",
    "decisions",
}
DECISION_KEYS = {
    "id",
    "scope",
    "title",
    "question",
    "status",
    "depends_on",
    "blocks",
    "effects",
    "source_refs",
    "options",
    "resolution",
}
OPTION_KEYS = {"id", "summary", "recommended", "rationale", "consequences"}
RESOLUTION_KEYS = {
    "selected_option_id",
    "decided_by",
    "decided_at",
    "evidence_refs",
}


class DecisionRegisterFormatError(ValueError):
    pass


class DecisionRegisterObservationError(RuntimeError):
    pass


def _exact_keys(value: object, expected: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != expected:
        raise DecisionRegisterFormatError(f"{label}:keys")
    return value


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or "\n" in value or "\r" in value:
        raise DecisionRegisterFormatError(f"{label}:text")
    return value


def _text_list(value: object, label: str, *, allow_empty: bool = False) -> list[str]:
    if not isinstance(value, list) or (not value and not allow_empty):
        raise DecisionRegisterFormatError(f"{label}:list")
    result = [_text(item, f"{label}[]") for item in value]
    if len(result) != len(set(result)):
        raise DecisionRegisterFormatError(f"{label}:duplicate")
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
        raise DecisionRegisterFormatError(f"{label}:relative_path")
    return text


def _validate_resolution(
    decision: dict[str, Any], option_ids: set[str], label: str
) -> None:
    status = decision["status"]
    resolution = decision["resolution"]
    if status == "pending":
        if resolution is not None:
            raise DecisionRegisterFormatError(f"{label}.resolution:pending")
        return
    if status != "resolved":
        raise DecisionRegisterFormatError(f"{label}.status")
    item = _exact_keys(resolution, RESOLUTION_KEYS, f"{label}.resolution")
    selected = _text(
        item["selected_option_id"], f"{label}.resolution.selected_option_id"
    )
    if selected not in option_ids:
        raise DecisionRegisterFormatError(
            f"{label}.resolution.selected_option_id"
        )
    _text(item["decided_by"], f"{label}.resolution.decided_by")
    decided_at = _text(item["decided_at"], f"{label}.resolution.decided_at")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", decided_at) is None:
        raise DecisionRegisterFormatError(f"{label}.resolution.decided_at")
    _text_list(item["evidence_refs"], f"{label}.resolution.evidence_refs")


def _assert_acyclic(decisions: list[dict[str, Any]]) -> None:
    dependencies = {item["id"]: item["depends_on"] for item in decisions}
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(decision_id: str) -> None:
        if decision_id in visiting:
            raise DecisionRegisterFormatError("decisions:cycle")
        if decision_id in visited:
            return
        visiting.add(decision_id)
        for dependency in dependencies[decision_id]:
            visit(dependency)
        visiting.remove(decision_id)
        visited.add(decision_id)

    for decision_id in dependencies:
        visit(decision_id)


def validate_register(register: object) -> dict[str, Any]:
    root = _exact_keys(register, TOP_LEVEL_KEYS, "register")
    if root["schema_id"] != SCHEMA_ID:
        raise DecisionRegisterFormatError("register:schema_id")
    _text(root["register_id"], "register_id")
    document = _exact_keys(
        root["document"], {"path", "start_marker", "end_marker"}, "document"
    )
    _relative_path(document["path"], "document.path")
    start = _text(document["start_marker"], "document.start_marker")
    end = _text(document["end_marker"], "document.end_marker")
    if start == end:
        raise DecisionRegisterFormatError("document:markers")
    if not isinstance(root["decisions"], list) or not root["decisions"]:
        raise DecisionRegisterFormatError("decisions:list")

    decisions: list[dict[str, Any]] = []
    ids: list[str] = []
    for index, value in enumerate(root["decisions"]):
        label = f"decisions[{index}]"
        decision = _exact_keys(value, DECISION_KEYS, label)
        decision_id = _text(decision["id"], f"{label}.id")
        if decision_id != f"D{index + 1}":
            raise DecisionRegisterFormatError(f"{label}.id:order")
        ids.append(decision_id)
        _text(decision["scope"], f"{label}.scope")
        _text(decision["title"], f"{label}.title")
        _text(decision["question"], f"{label}.question")
        dependencies = _text_list(
            decision["depends_on"], f"{label}.depends_on", allow_empty=True
        )
        if any(item not in ids[:-1] for item in dependencies):
            raise DecisionRegisterFormatError(f"{label}.depends_on:order")
        blocks = _text_list(decision["blocks"], f"{label}.blocks")
        if not set(blocks) <= ALLOWED_SLICES:
            raise DecisionRegisterFormatError(f"{label}.blocks:value")
        effects = _text_list(decision["effects"], f"{label}.effects")
        if not set(effects) <= ALLOWED_EFFECTS:
            raise DecisionRegisterFormatError(f"{label}.effects:value")
        _text_list(decision["source_refs"], f"{label}.source_refs")
        if not isinstance(decision["options"], list) or len(decision["options"]) < 2:
            raise DecisionRegisterFormatError(f"{label}.options:list")
        option_ids: set[str] = set()
        recommended = 0
        for option_index, value in enumerate(decision["options"]):
            option_label = f"{label}.options[{option_index}]"
            option = _exact_keys(value, OPTION_KEYS, option_label)
            option_id = _text(option["id"], f"{option_label}.id")
            if option_id in option_ids:
                raise DecisionRegisterFormatError(f"{label}.options:duplicate")
            option_ids.add(option_id)
            _text(option["summary"], f"{option_label}.summary")
            if not isinstance(option["recommended"], bool):
                raise DecisionRegisterFormatError(f"{option_label}.recommended")
            recommended += int(option["recommended"])
            _text(option["rationale"], f"{option_label}.rationale")
            _text_list(option["consequences"], f"{option_label}.consequences")
        if recommended != 1:
            raise DecisionRegisterFormatError(f"{label}.options:recommended")
        _validate_resolution(decision, option_ids, label)
        decisions.append(decision)
    _assert_acyclic(decisions)

    active = root["active_decision_id"]
    if active is not None:
        _text(active, "active_decision_id")
    resolved = {item["id"] for item in decisions if item["status"] == "resolved"}
    ready_pending = [
        item["id"]
        for item in decisions
        if item["status"] == "pending" and set(item["depends_on"]) <= resolved
    ]
    expected_active = ready_pending[0] if ready_pending else None
    if active != expected_active:
        raise DecisionRegisterFormatError("active_decision_id:first_ready_pending")
    return root


def load_register(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DecisionRegisterFormatError(f"manifest_unreadable:{exc}") from exc
    return validate_register(value)


def _markdown(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def render_document(register: dict[str, Any]) -> str:
    lines = [
        f"> Generated from `{register['register_id']}`. Recommendations are not decisions; do not edit this block by hand.",
        "",
        f"Active question: `{register['active_decision_id']}`. Decisions are asked one at a time. A `pending` entry has no normative authority.",
        "",
        "| ID | Scope | Decision | Status | Depends on | Blocks | Recommended option |",
        "|---|---|---|---|---|---|---|",
    ]
    for decision in register["decisions"]:
        recommended = next(
            option for option in decision["options"] if option["recommended"]
        )
        dependencies = ", ".join(decision["depends_on"]) or "—"
        blocks = ", ".join(decision["blocks"])
        lines.append(
            f"| `{decision['id']}` | `{_markdown(decision['scope'])}` | {_markdown(decision['title'])} | `{decision['status']}` | {_markdown(dependencies)} | {_markdown(blocks)} | `{recommended['id']}` |"
        )
    for decision in register["decisions"]:
        lines.extend(
            [
                "",
                f"### {decision['id']} — {decision['title']}",
                "",
                f"**Status:** `{decision['status']}`",
                "",
                f"**Question:** {decision['question']}",
                "",
                "**Options:**",
                "",
            ]
        )
        for option in decision["options"]:
            suffix = " — recommended, not selected" if option["recommended"] else ""
            lines.append(f"- `{option['id']}`{suffix}: {option['summary']} {option['rationale']}")
        lines.extend(
            [
                "",
                f"**Effects:** {', '.join(f'`{item}`' for item in decision['effects'])}",
                "",
                f"**Sources:** {', '.join(f'`{item}`' for item in decision['source_refs'])}",
            ]
        )
    return "\n".join(lines)


def _repo_path(repo_root: Path, relative: str) -> Path:
    root = repo_root.resolve()
    candidate = root.joinpath(*PurePosixPath(relative).parts)
    try:
        candidate.resolve(strict=False).relative_to(root)
    except (OSError, ValueError) as exc:
        raise DecisionRegisterObservationError(f"outside_repo:{relative}") from exc
    return candidate


def _canonical_regular_text(repo_root: Path, relative: str) -> str:
    path = _repo_path(repo_root, relative)
    try:
        if not stat.S_ISREG(os.lstat(path).st_mode):
            raise DecisionRegisterObservationError(f"not_regular:{relative}")
        return path.read_text(encoding="utf-8").replace("\r\n", "\n").replace("\r", "\n")
    except (OSError, UnicodeError) as exc:
        raise DecisionRegisterObservationError(f"unreadable:{relative}:{exc}") from exc


def _document_matches(register: dict[str, Any], repo_root: Path) -> bool:
    document = register["document"]
    text = _canonical_regular_text(repo_root, document["path"])
    start, end = document["start_marker"], document["end_marker"]
    start_token, end_token = f"{start}\n", f"\n{end}"
    if text.count(start) != 1 or text.count(end) != 1:
        return False
    start_index = text.find(start_token)
    body_start = start_index + len(start_token)
    end_index = text.find(end_token, body_start)
    if start_index < 0 or end_index < body_start:
        return False
    return text[body_start:end_index] == render_document(register)


def verify_register(
    register: dict[str, Any], repo_root: Path, *, require_slice: str | None = None
) -> dict[str, Any]:
    validate_register(register)
    if require_slice is not None and require_slice not in ALLOWED_SLICES:
        raise DecisionRegisterFormatError("require_slice:value")
    failures: list[dict[str, Any]] = []
    try:
        document_matches = _document_matches(register, repo_root)
    except DecisionRegisterObservationError as exc:
        return {
            "schema_id": REPORT_SCHEMA_ID,
            "status": "indeterminate",
            "valid": False,
            "failures": [],
            "indeterminate_reasons": [{"code": str(exc)}],
        }
    if not document_matches:
        failures.append(
            {"code": "generated_document_drift", "path": register["document"]["path"]}
        )
    pending = [item for item in register["decisions"] if item["status"] == "pending"]
    if require_slice is not None:
        blockers = sorted(
            item["id"] for item in pending if require_slice in item["blocks"]
        )
        if blockers:
            failures.append(
                {
                    "code": "slice_blocked_by_pending_decisions",
                    "slice": require_slice,
                    "decision_ids": blockers,
                }
            )
    if failures:
        status = "blocked" if all(
            item["code"] == "slice_blocked_by_pending_decisions" for item in failures
        ) else "invalid"
    else:
        status = "valid_pending" if pending else "ready"
    return {
        "schema_id": REPORT_SCHEMA_ID,
        "register_id": register["register_id"],
        "status": status,
        "valid": not failures,
        "active_decision_id": register["active_decision_id"],
        "pending_decision_count": len(pending),
        "resolved_decision_count": len(register["decisions"]) - len(pending),
        "document_matches": document_matches,
        "required_slice": require_slice,
        "failures": failures,
        "indeterminate_reasons": [],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    check = subparsers.add_parser("check")
    check.add_argument("--repo-root", default=".")
    check.add_argument(
        "--manifest", default="contracts/agent-first/spec-decision-register.json"
    )
    check.add_argument("--require-slice", choices=sorted(ALLOWED_SLICES))
    render = subparsers.add_parser("render-document")
    render.add_argument(
        "--manifest", default="contracts/agent-first/spec-decision-register.json"
    )
    args = parser.parse_args(argv)
    try:
        register = load_register(Path(args.manifest))
        if args.command == "render-document":
            print(render_document(register))
            return 0
        report = verify_register(
            register, Path(args.repo_root), require_slice=args.require_slice
        )
    except DecisionRegisterFormatError as exc:
        report = {
            "schema_id": REPORT_SCHEMA_ID,
            "status": "invalid",
            "valid": False,
            "failures": [{"code": "manifest_invalid", "detail": str(exc)}],
            "indeterminate_reasons": [],
        }
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    if report["status"] in {"valid_pending", "ready"}:
        return 0
    return 2 if report["status"] == "indeterminate" else 1


if __name__ == "__main__":
    raise SystemExit(main())
