#!/usr/bin/env python3
"""Report legacy Agent-First surfaces without requiring their removal yet."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from scripts.agent_first_contract_files import (
    BaselineEnvironmentError,
    canonical_text,
    repository_roots,
    surface_path,
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
INVENTORY_SCHEMA_ID = "pullwise-agent-first-legacy-removal-inventory/v1"
REPORT_SCHEMA_ID = "pullwise-agent-first-legacy-absence-report/v1"


class InventoryError(ValueError):
    """The legacy-removal inventory is malformed or contradicts D27."""


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise InventoryError("invalid_cli_arguments")


def _load_inventory(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise InventoryError("inventory_unreadable") from exc
    if not isinstance(payload, dict) or payload.get("schema_id") != INVENTORY_SCHEMA_ID:
        raise InventoryError("inventory_schema_invalid")
    return payload


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


def verify_legacy_absence(
    inventory: dict[str, Any], workspace_root: Path
) -> dict[str, Any]:
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
    legacy_absent = all(item["status"] == "absent" for item in reports)
    return {
        "schema_id": REPORT_SCHEMA_ID,
        "inventory_id": inventory["inventory_id"],
        "d27": d27,
        "status": "absent" if legacy_absent else "legacy_present",
        "legacy_absent": legacy_absent,
        "ratchet_clean": True,
        "surfaces": reports,
        "unexpected_surfaces": [],
        "failures": [],
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
    try:
        args = parser.parse_args(argv)
        inventory = _load_inventory(args.inventory)
        report = verify_legacy_absence(inventory, args.workspace_root)
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except (InventoryError, DecisionRegisterFormatError, KeyError, TypeError, ValueError):
        print(json.dumps(_error_report("inventory_invalid"), indent=2, sort_keys=True))
        return 2
    except (BaselineEnvironmentError, OSError, UnicodeError):
        print(json.dumps(_error_report("environment_invalid"), indent=2, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
