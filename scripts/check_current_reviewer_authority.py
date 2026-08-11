#!/usr/bin/env python3
"""Verify that every Pullwise repository routes Reviewer work to one authority."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
REPORT_SCHEMA_ID = "pullwise-current-reviewer-authority-report/v1"
START_MARKER = "<!-- PULLWISE_REVIEWER_CURRENT_AUTHORITY_START -->"
END_MARKER = "<!-- PULLWISE_REVIEWER_CURRENT_AUTHORITY_END -->"

CURRENT_AUTHORITY_URL = (
    "https://app.notion.com/p/3b4e5c88f85f8128bd39dac3a7679c4a"
)
IMPLEMENTATION_SPEC_URL = (
    "https://app.notion.com/p/3b4e5c88f85f818e933ecf3864c97469"
)
IMPLEMENTATION_CARDS_URL = (
    "https://app.notion.com/p/b79ceacfedcd4d34a0d619c1790066c4"
)
AUTHORIZATION_REGISTRY_URL = (
    "https://app.notion.com/p/760a1698a86b404083662eeb1b637f64"
)
PAGE_00A_URL = "https://app.notion.com/p/3b5e5c88f85f81bc840ace8b8a65962e"
PAGE_00B_URL = "https://app.notion.com/p/3b5e5c88f85f81aeaeaef4621d211126"
PAGE_00C_URL = "https://app.notion.com/p/3b5e5c88f85f81d89deef714c8b23eeb"
PAGE_00D_URL = "https://app.notion.com/p/3b8e5c88f85f814d8296c6c60541946d"
PAGE_19_URL = "https://app.notion.com/p/3b4e5c88f85f8192a488f6db72fa116b"

REPOSITORIES = {
    "admin": "pullwise-admin",
    "server": "pullwise-server",
    "web": "pullwise-web",
    "worker": "pullwise-worker",
}

ROUTING_BLOCK = f"""{START_MARKER}
## Pullwise Reviewer — Current Implementation Authority

For every new Pullwise Reviewer implementation task, the only entry point is
[Pullwise Reviewer — Current Implementation Authority]({CURRENT_AUTHORITY_URL}).
Follow its [current implementation specification]({IMPLEMENTATION_SPEC_URL}),
[live implementation cards]({IMPLEMENTATION_CARDS_URL}), and
[Code Authorization Registry]({AUTHORIZATION_REGISTRY_URL}).

Before any card, read [00A]({PAGE_00A_URL}), [00B]({PAGE_00B_URL}),
[00C]({PAGE_00C_URL}), [00D]({PAGE_00D_URL}) whenever a named asset is required,
and [Page 19]({PAGE_19_URL}). Only an exact live card authorization plus a PASS
running gate permits tracked writes. Never self-authorize a card or treat
documentation status as code authority.

All later Reviewer protocol, runtime, phase, fanout, generated-package, and
production-cutover guidance is retained only as current-state cleanup evidence;
it must not govern new Reviewer implementation. Unrelated repository security,
deployment, frontend, and testing rules remain binding unless they conflict with
the current Reviewer authority.
{END_MARKER}"""

REQUIRED_URLS = (
    CURRENT_AUTHORITY_URL,
    IMPLEMENTATION_SPEC_URL,
    IMPLEMENTATION_CARDS_URL,
    AUTHORIZATION_REGISTRY_URL,
    PAGE_00A_URL,
    PAGE_00B_URL,
    PAGE_00C_URL,
    PAGE_00D_URL,
    PAGE_19_URL,
)
CONTRADICTORY_LITERALS = (
    "review-worker-protocol/v1 is the implementation authority",
    "Agent-First is the implementation authority",
    "Agent Kernel is the implementation authority",
    "may govern new Reviewer implementation",
)


class WorkspaceInputError(RuntimeError):
    pass


def _is_link_or_reparse(metadata: os.stat_result) -> bool:
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    file_attributes = getattr(metadata, "st_file_attributes", 0)
    return stat.S_ISLNK(metadata.st_mode) or bool(
        reparse_flag and file_attributes & reparse_flag
    )


def _resolve_workspace_root(workspace_root: Path) -> Path:
    lexical = Path(os.path.abspath(os.fspath(workspace_root)))
    try:
        metadata = lexical.lstat()
    except FileNotFoundError as exc:
        raise WorkspaceInputError("workspace_root_missing") from exc
    except OSError as exc:
        raise WorkspaceInputError("workspace_root_unreadable") from exc
    if _is_link_or_reparse(metadata) or not stat.S_ISDIR(metadata.st_mode):
        raise WorkspaceInputError("workspace_root_not_safe")
    try:
        resolved = lexical.resolve(strict=True)
    except OSError as exc:
        raise WorkspaceInputError("workspace_root_unreadable") from exc
    if resolved != lexical:
        raise WorkspaceInputError("workspace_root_not_safe")
    return resolved


def _resolve_repository_root(root: Path, directory: str) -> Path:
    path = root / directory
    try:
        metadata = path.lstat()
    except FileNotFoundError as exc:
        raise WorkspaceInputError("repository_path_missing") from exc
    except OSError as exc:
        raise WorkspaceInputError("repository_path_unreadable") from exc
    if _is_link_or_reparse(metadata) or not stat.S_ISDIR(metadata.st_mode):
        raise WorkspaceInputError("repository_path_not_safe")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise WorkspaceInputError("repository_path_unreadable") from exc
    if resolved.parent != root:
        raise WorkspaceInputError("repository_path_outside_workspace")
    return resolved


def _read_regular_utf8(path: Path, *, root: Path, repository_root: Path) -> tuple[str, str]:
    try:
        metadata = path.lstat()
    except FileNotFoundError as exc:
        raise WorkspaceInputError("agents_file_missing") from exc
    except OSError as exc:
        raise WorkspaceInputError("agents_file_unreadable") from exc
    if _is_link_or_reparse(metadata) or not stat.S_ISREG(metadata.st_mode):
        raise WorkspaceInputError("agents_file_not_regular")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise WorkspaceInputError("agents_file_unreadable") from exc
    if resolved.parent != repository_root or not resolved.is_relative_to(root):
        raise WorkspaceInputError("agents_file_outside_repository")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise WorkspaceInputError("agents_file_unreadable") from exc
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise WorkspaceInputError("agents_file_not_utf8") from exc
    return text, hashlib.sha256(raw).hexdigest()


def _normalize(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _routing_errors(text: str) -> tuple[list[str], str | None]:
    normalized = _normalize(text)
    if not normalized.startswith(START_MARKER + "\n"):
        return ["missing_current_authority_block"], None
    end = normalized.find(END_MARKER)
    if end < 0:
        return ["unterminated_current_authority_block"], None
    block = normalized[: end + len(END_MARKER)]
    errors: list[str] = []
    if CURRENT_AUTHORITY_URL not in block:
        errors.append("stale_authority")
    if any(literal in block for literal in CONTRADICTORY_LITERALS):
        errors.append("contradictory_block")
    if any(url not in block for url in REQUIRED_URLS):
        errors.append("required_reference_missing")
    if block != ROUTING_BLOCK:
        errors.append("routing_block_mismatch")
    return errors, block


def validate_workspace(workspace_root: Path) -> dict[str, Any]:
    reports: list[dict[str, Any]] = []
    blocks: list[str] = []
    environment_errors: list[dict[str, str]] = []
    try:
        root = _resolve_workspace_root(workspace_root)
    except WorkspaceInputError as exc:
        return {
            "schema_id": REPORT_SCHEMA_ID,
            "status": "INDETERMINATE",
            "repositories": reports,
            "routing_parity_sha256": None,
            "environment_errors": [{"repo": "workspace", "code": str(exc)}],
        }
    for repo, directory in REPOSITORIES.items():
        try:
            repository_root = _resolve_repository_root(root, directory)
            path = repository_root / "AGENTS.md"
            text, byte_sha256 = _read_regular_utf8(
                path, root=root, repository_root=repository_root
            )
        except WorkspaceInputError as exc:
            environment_errors.append({"repo": repo, "code": str(exc)})
            continue
        errors, block = _routing_errors(text)
        if block is not None:
            blocks.append(block)
        reports.append(
            {
                "repo": repo,
                "path": f"{directory}/AGENTS.md",
                "status": "PASS" if not errors else "FAIL",
                "errors": errors,
                "sha256": byte_sha256,
            }
        )

    parity_sha256 = None
    if len(blocks) == len(REPOSITORIES):
        parity_sha256 = hashlib.sha256(blocks[0].encode("utf-8")).hexdigest()
        if any(block != blocks[0] for block in blocks[1:]):
            for report in reports:
                if "routing_parity_mismatch" not in report["errors"]:
                    report["errors"].append("routing_parity_mismatch")
                    report["status"] = "FAIL"

    failed = any(report["status"] != "PASS" for report in reports)
    status = "INDETERMINATE" if environment_errors else "FAIL" if failed else "PASS"
    return {
        "schema_id": REPORT_SCHEMA_ID,
        "status": status,
        "repositories": reports,
        "routing_parity_sha256": parity_sha256,
        "environment_errors": environment_errors,
    }


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise WorkspaceInputError(message)


def main(argv: list[str] | None = None) -> int:
    parser = JsonArgumentParser()
    parser.add_argument("--workspace-root", type=Path, default=ROOT.parent)
    try:
        args = parser.parse_args(argv)
        report = validate_workspace(args.workspace_root)
    except WorkspaceInputError as exc:
        report = {
            "schema_id": REPORT_SCHEMA_ID,
            "status": "INDETERMINATE",
            "repositories": [],
            "routing_parity_sha256": None,
            "environment_errors": [{"repo": "workspace", "code": str(exc)}],
        }
    json.dump(report, sys.stdout, ensure_ascii=False, sort_keys=True)
    sys.stdout.write("\n")
    return 0 if report["status"] == "PASS" else 1 if report["status"] == "FAIL" else 2


if __name__ == "__main__":
    raise SystemExit(main())
