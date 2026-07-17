#!/usr/bin/env python3
"""Validate and render the Agent-First specification decision register."""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.agent_first_decision_catalog import REPORT_SCHEMA_ID, SLICES
from scripts.agent_first_decision_core import (
    DecisionRegisterFormatError,
    canonical_resolution_sha256,
    validate_register,
)
from scripts.agent_first_decision_gate import (
    DecisionRegisterObservationError,
    normative_reference_failures,
    resolved_history_failures,
    verify_register,
)
from scripts.agent_first_decision_render import (
    render_document,
    sync_generated_file,
)


DEFAULT_MANIFEST = "contracts/agent-first/spec-decision-register.json"


def load_register(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DecisionRegisterFormatError(f"manifest_unreadable:{exc}") from exc
    return validate_register(value)


def load_repo_register(repo_root: Path, manifest: str) -> dict[str, Any]:
    if manifest != DEFAULT_MANIFEST:
        raise DecisionRegisterFormatError("manifest:canonical_path")
    root = repo_root.resolve()
    path = root / DEFAULT_MANIFEST
    try:
        if path.resolve(strict=False).relative_to(root) != Path(DEFAULT_MANIFEST):
            raise DecisionRegisterFormatError("manifest:canonical_path")
        if not stat.S_ISREG(os.lstat(path).st_mode):
            raise DecisionRegisterFormatError("manifest:not_regular")
    except DecisionRegisterFormatError:
        raise
    except (OSError, ValueError) as exc:
        raise DecisionRegisterFormatError(f"manifest:unreadable:{exc}") from exc
    return load_register(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    check = subparsers.add_parser("check")
    check.add_argument("--repo-root", default=".")
    check.add_argument("--manifest", default=DEFAULT_MANIFEST)
    check.add_argument("--require-slice", choices=SLICES)
    render = subparsers.add_parser("render-document")
    render.add_argument("--repo-root", default=".")
    render.add_argument("--manifest", default=DEFAULT_MANIFEST)
    sync = subparsers.add_parser("sync-document")
    sync.add_argument("--repo-root", default=".")
    sync.add_argument("--manifest", default=DEFAULT_MANIFEST)
    args = parser.parse_args(argv)
    try:
        repo_root = Path(args.repo_root)
        register = load_repo_register(repo_root, args.manifest)
        if args.command == "render-document":
            print(render_document(register))
            return 0
        if args.command == "sync-document":
            print(sync_generated_file(register, repo_root))
            return 0
        report = verify_register(
            register, repo_root, require_slice=args.require_slice
        )
    except (DecisionRegisterFormatError, DecisionRegisterObservationError) as exc:
        report = {
            "schema_id": REPORT_SCHEMA_ID,
            "status": "invalid",
            "valid": False,
            "ready": False,
            "failures": [{"code": "manifest_invalid", "detail": str(exc)}],
            "indeterminate_reasons": [],
        }
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    if report["status"] in {"valid_pending", "ready"}:
        return 0
    return 2 if report["status"] == "indeterminate" else 1


__all__ = [
    "DecisionRegisterFormatError",
    "DecisionRegisterObservationError",
    "canonical_resolution_sha256",
    "load_register",
    "load_repo_register",
    "normative_reference_failures",
    "render_document",
    "resolved_history_failures",
    "sync_generated_file",
    "validate_register",
    "verify_register",
]


if __name__ == "__main__":
    raise SystemExit(main())
