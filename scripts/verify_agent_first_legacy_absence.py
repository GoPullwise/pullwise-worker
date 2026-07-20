#!/usr/bin/env python3
"""Report legacy Agent-First surfaces without requiring their removal yet."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
from typing import Any

from scripts.agent_first_contract_files import (
    BaselineEnvironmentError,
    canonical_text,
    repository_roots,
    surface_path,
)
from scripts.agent_first_legacy_inventory import (
    InventoryError,
    load_inventory,
    validate_inventory,
)
from scripts.agent_first_decision_core import (
    DecisionRegisterFormatError,
    selected_option_id,
)
from scripts.agent_first_decision_register import load_register


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INVENTORY = (
    ROOT / "contracts" / "agent-first" / "legacy-removal-inventory.json"
)
REPORT_SCHEMA_ID = "pullwise-agent-first-legacy-absence-report/v1"
TEXT_SUFFIXES = {
    ".css",
    ".html",
    ".js",
    ".json",
    ".jsx",
    ".md",
    ".mjs",
    ".py",
    ".sh",
    ".sql",
    ".toml",
    ".ts",
    ".tsx",
    ".txt",
    ".yaml",
    ".yml",
}



class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise InventoryError("invalid_cli_arguments")


def _validate_d27(
    inventory: dict[str, Any], roots: dict[str, Path]
) -> dict[str, str]:
    binding = inventory.get("d27")
    if not isinstance(binding, dict):
        raise InventoryError("d27_binding_invalid")
    register_path = binding.get("register_path")
    if not isinstance(register_path, str):
        raise InventoryError("d27_binding_invalid")
    path = surface_path(roots["worker"], register_path)
    if path is None:
        raise InventoryError("d27_register_missing")
    register = load_register(path)
    decision = next(
        (item for item in register["decisions"] if item["id"] == binding.get("decision_id")),
        None,
    )
    if (
        decision is None
        or decision["status"] != "resolved"
        or decision["resolution"]["kind"] != "option"
        or selected_option_id(decision) != binding.get("selected_option_id")
        or decision["resolution"]["resolution_sha256"]
        != binding.get("resolution_sha256")
    ):
        raise InventoryError("d27_binding_mismatch")
    return {
        "decision_id": decision["id"],
        "selected_option_id": decision["resolution"]["selected_option_id"],
        "resolution_sha256": decision["resolution"]["resolution_sha256"],
    }


def _worktree_paths(repo_root: Path) -> list[str]:
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(repo_root),
                "ls-files",
                "--cached",
                "--others",
                "--exclude-standard",
                "-z",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BaselineEnvironmentError("worktree_catalog_unavailable") from exc
    if result.returncode != 0:
        raise BaselineEnvironmentError("worktree_catalog_unavailable")
    try:
        decoded = result.stdout.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BaselineEnvironmentError("worktree_path_not_utf8") from exc
    return sorted(set(decoded.split("\0")) - {""})

def _without_excluded_sections(
    text: str, exclusions: list[dict[str, Any]]
) -> str:
    spans: list[tuple[int, int]] = []
    for item in exclusions:
        start_marker = item["start_marker"]
        end_marker = item["end_marker"]
        if (
            not isinstance(start_marker, str)
            or not start_marker
            or not isinstance(end_marker, str)
            or not end_marker
            or start_marker == end_marker
            or text.count(start_marker) != 1
            or text.count(end_marker) != 1
        ):
            raise InventoryError("evidence_exclusion_markers_invalid")
        start = text.index(start_marker)
        end = text.index(end_marker) + len(end_marker)
        if start >= end:
            raise InventoryError("evidence_exclusion_markers_invalid")
        spans.append((start, end))
    spans.sort()
    if any(left[1] > right[0] for left, right in zip(spans, spans[1:])):
        raise InventoryError("evidence_exclusion_spans_overlap")
    pieces: list[str] = []
    cursor = 0
    for start, end in spans:
        pieces.append(text[cursor:start])
        cursor = end
    pieces.append(text[cursor:])
    return "".join(pieces)


def verify_legacy_absence(
    inventory: dict[str, Any], workspace_root: Path, *, require_absent: bool = False
) -> dict[str, Any]:
    validate_inventory(inventory)
    roots = repository_roots(workspace_root)
    d27 = _validate_d27(inventory, roots)
    signatures = {
        item["id"]: item["literal"] for item in inventory.get("signatures", [])
    }
    reports: list[dict[str, Any]] = []
    for surface in inventory.get("surfaces", []):
        path = surface_path(roots[surface["repo"]], surface["path"])
        matched: list[str] = []
        if path is not None:
            text = canonical_text(path)
            matched = [
                signature_id
                for signature_id in surface["signature_ids"]
                if signatures[signature_id] in text
            ]
        reports.append(
            {
                "id": surface["id"],
                "repo": surface["repo"],
                "path": surface["path"],
                "status": "present" if matched else "absent",
                "matched_signature_ids": matched,
            }
        )
    reports.sort(key=lambda item: item["id"])
    registered = {
        (surface["repo"], surface["path"], signature_id)
        for surface in inventory.get("surfaces", [])
        for signature_id in surface["signature_ids"]
    }
    whole_exclusions = {
        (item["repo"], item["path"])
        for item in inventory.get("evidence_exclusions", [])
        if item["start_marker"] is None and item["end_marker"] is None
    }
    unexpected: list[dict[str, str]] = []
    for repo_id, repo_root in roots.items():
        for relative in _worktree_paths(repo_root):
            path = surface_path(repo_root, relative)
            if path is None:
                raise BaselineEnvironmentError("worktree_path_disappeared")
            if (repo_id, relative) in whole_exclusions:
                canonical_text(path)
                continue
            if Path(relative).suffix.lower() not in TEXT_SUFFIXES:
                continue
            text = canonical_text(path)
            text = _without_excluded_sections(
                text,
                [
                    item
                    for item in inventory.get("evidence_exclusions", [])
                    if (item["repo"], item["path"]) == (repo_id, relative)
                    and (repo_id, relative) not in whole_exclusions
                ],
            )
            for signature_id, literal in signatures.items():
                key = (repo_id, relative, signature_id)
                if literal in text and key not in registered:
                    unexpected.append(
                        {
                            "repo": repo_id,
                            "path": relative,
                            "signature_id": signature_id,
                        }
                    )
    unexpected.sort(key=lambda item: (item["repo"], item["path"], item["signature_id"]))
    legacy_absent = all(item["status"] == "absent" for item in reports)
    ratchet_clean = not unexpected
    status = (
        "unexpected_legacy"
        if unexpected
        else "absent" if legacy_absent else "legacy_present"
    )
    failures: list[dict[str, str]] = [
        {"code": "unexpected_legacy_surface", **item} for item in unexpected
    ]
    if require_absent:
        failures.extend(
            {
                "code": "legacy_surface_present",
                "surface_id": item["id"],
            }
            for item in reports if item["status"] == "present"
        )
    return {
        "schema_id": REPORT_SCHEMA_ID,
        "inventory_id": inventory["inventory_id"],
        "d27": d27,
        "status": status,
        "legacy_absent": legacy_absent,
        "require_absent": require_absent,
        "ratchet_clean": ratchet_clean,
        "surfaces": reports,
        "unexpected_surfaces": unexpected,
        "failures": failures,
        "indeterminate_reasons": [],
    }


def _error_report(kind: str) -> dict[str, Any]:
    return {
        "schema_id": REPORT_SCHEMA_ID,
        "status": "indeterminate",
        "legacy_absent": False,
        "ratchet_clean": False,
        "error_kind": kind,
    }


def main(argv: list[str] | None = None) -> int:
    parser = JsonArgumentParser(description=__doc__)
    parser.add_argument("--inventory", type=Path, default=DEFAULT_INVENTORY)
    parser.add_argument("--workspace-root", type=Path, required=True)
    parser.add_argument("--require-absent", action="store_true")
    try:
        args = parser.parse_args(argv)
        inventory = load_inventory(args.inventory)
        report = verify_legacy_absence(
            inventory,
            args.workspace_root,
            require_absent=args.require_absent,
        )
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if not report["failures"] else 1
    except (
        InventoryError,
        DecisionRegisterFormatError,
        KeyError,
        TypeError,
        ValueError,
    ):
        print(json.dumps(_error_report("inventory_invalid"), indent=2, sort_keys=True))
        return 2
    except (BaselineEnvironmentError, OSError, UnicodeError):
        print(json.dumps(_error_report("environment_invalid"), indent=2, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
