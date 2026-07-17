#!/usr/bin/env python3
"""Validate and render the Agent-First specification decision register."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

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
from scripts.agent_first_decision_render import render_document


DEFAULT_MANIFEST = "contracts/agent-first/spec-decision-register.json"


def load_register(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DecisionRegisterFormatError(f"manifest_unreadable:{exc}") from exc
    return validate_register(value)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    check = subparsers.add_parser("check")
    check.add_argument("--repo-root", default=".")
    check.add_argument("--manifest", default=DEFAULT_MANIFEST)
    check.add_argument("--require-slice", choices=SLICES)
    render = subparsers.add_parser("render-document")
    render.add_argument("--manifest", default=DEFAULT_MANIFEST)
    args = parser.parse_args(argv)
    try:
        register = load_register(Path(args.manifest))
        if args.command == "render-document":
            print(render_document(register))
            return 0
        report = verify_register(
            register, Path(args.repo_root), require_slice=args.require_slice
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
    "normative_reference_failures",
    "render_document",
    "resolved_history_failures",
    "validate_register",
    "verify_register",
]


if __name__ == "__main__":
    raise SystemExit(main())
