#!/usr/bin/env python3
"""Report legacy Agent-First surfaces without requiring their removal yet."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


from scripts.agent_first_contract_files import (
    BaselineEnvironmentError,
    repository_roots,
    read_surface,
    surface_path,
)
from scripts.agent_first_legacy_inventory import (
    InventoryError,
    parse_inventory,
    reject_duplicate_keys,
)
from scripts.agent_first_legacy_catalog import (
    EXPECTED_CATALOG_SHA256,
    EXPECTED_INVENTORY_ID,
)
from scripts.agent_first_legacy_observation import observe_legacy_surfaces
from scripts.agent_first_decision_core import (
    DecisionRegisterFormatError,
    selected_option_id,
    validate_register,
)


DEFAULT_INVENTORY = (
    ROOT / "contracts" / "agent-first" / "legacy-removal-inventory.json"
)
INVENTORY_RELATIVE_PATH = DEFAULT_INVENTORY.relative_to(ROOT).as_posix()
REPORT_SCHEMA_ID = "pullwise-agent-first-legacy-absence-report/v1"

class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise InventoryError("invalid_cli_arguments")


def _validate_d27(
    inventory: dict[str, Any],
    roots: dict[str, Path],
) -> tuple[dict[str, str], bytes]:
    binding = inventory.get("d27")
    if not isinstance(binding, dict):
        raise InventoryError("d27_binding_invalid")
    register_path = binding.get("register_path")
    if not isinstance(register_path, str):
        raise InventoryError("d27_binding_invalid")
    path = surface_path(roots["worker"], register_path)
    if path is None:
        raise InventoryError("d27_register_missing")
    raw = read_surface(path)
    try:
        register_text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BaselineEnvironmentError("d27_register_not_utf8") from exc
    register = validate_register(
        json.loads(
            register_text.replace("\r\n", "\n").replace("\r", "\n"),
            object_pairs_hook=reject_duplicate_keys,
        )
    )
    decision = next(
        (item for item in register["decisions"] if item["id"] == binding.get("decision_id")),
        None,
    )
    if (
        decision is None
        or decision["status"] != "resolved"
        or decision["supersedes"] != ["D4"]
        or decision["resolution"]["kind"] != "option"
        or selected_option_id(decision) != binding.get("selected_option_id")
        or decision["resolution"]["resolution_sha256"]
        != binding.get("resolution_sha256")
    ):
        raise InventoryError("d27_binding_mismatch")
    return (
        {
            "decision_id": decision["id"],
            "selected_option_id": decision["resolution"]["selected_option_id"],
            "resolution_sha256": decision["resolution"]["resolution_sha256"],
        },
        raw,
    )


def _strict_catalog_indeterminate_reasons(
    inventory: dict[str, Any],
    reports: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    baseline_path = inventory["frozen_baseline"]["path"]
    return [
        {
            "code": "strict_catalog_self_reference",
            "surface_id": item["id"],
            "repo": item["repo"],
            "path": item["path"],
        }
        for item in reports
        if (
            item["source"] == "high_signal_inventory"
            and item["repo"] == "worker"
            and item["path"] == baseline_path
            and item["status"] == "present"
        )
    ]


def verify_legacy_absence(
    inventory: dict[str, Any],
    workspace_root: Path,
    *,
    inventory_raw: bytes,
    require_absent: bool = False,
) -> dict[str, Any]:
    snapshot_inventory = parse_inventory(inventory_raw)
    if snapshot_inventory != inventory:
        raise InventoryError("inventory_snapshot_mismatch")
    inventory = snapshot_inventory
    roots = repository_roots(workspace_root)
    control_snapshot = {
        ("worker", INVENTORY_RELATIVE_PATH): inventory_raw,
    }
    d27, register_raw = _validate_d27(inventory, roots)
    control_snapshot[
        ("worker", inventory["d27"]["register_path"])
    ] = register_raw
    signatures = {
        item["id"]: item["literal"] for item in inventory["signatures"]
    }
    reports, unexpected = observe_legacy_surfaces(
        inventory,
        roots,
        signatures,
        read_file=read_surface,
        initial_snapshot=control_snapshot,
    )
    legacy_absent = not unexpected and all(
        item["status"] == "absent" for item in reports
    )
    ratchet_clean = not unexpected
    indeterminate_reasons = (
        _strict_catalog_indeterminate_reasons(inventory, reports)
        if require_absent
        else []
    )
    status = (
        "indeterminate"
        if indeterminate_reasons
        else "unexpected_legacy"
        if unexpected
        else "absent" if legacy_absent else "legacy_present"
    )
    failures: list[dict[str, Any]] = [
        {"code": "unexpected_legacy_surface", **item} for item in unexpected
    ]
    self_reference_ids = {
        item["surface_id"] for item in indeterminate_reasons
    }
    if require_absent:
        failures.extend(
            {
                "code": "legacy_surface_present",
                "surface_id": item["id"],
            }
            for item in reports
            if (
                item["status"] == "present"
                and item["id"] not in self_reference_ids
            )
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
        "indeterminate_reasons": indeterminate_reasons,
    }


def _error_report(kind: str) -> dict[str, Any]:
    return {
        "schema_id": REPORT_SCHEMA_ID,
        "status": "indeterminate",
        "legacy_absent": False,
        "ratchet_clean": False,
        "error_kind": kind,
    }


def _canonical_inventory_path(provided: Path, workspace_root: Path) -> Path:
    roots = repository_roots(workspace_root)
    expected = surface_path(roots["worker"], INVENTORY_RELATIVE_PATH)
    if expected is None:
        raise InventoryError("inventory:missing")
    lexical = Path(os.path.abspath(provided))
    if os.path.normcase(str(lexical)) != os.path.normcase(str(expected)):
        raise InventoryError("inventory:canonical_path")
    return expected


def _require_catalog_ceiling(
    inventory: dict[str, Any], inventory_path: Path | None = None
) -> None:
    del inventory_path
    if inventory.get("inventory_id") != EXPECTED_INVENTORY_ID:
        raise InventoryError("inventory_id:production_mismatch")
    if inventory.get("catalog_sha256") != EXPECTED_CATALOG_SHA256:
        raise InventoryError("catalog_sha256:ceiling_mismatch")


def main(argv: list[str] | None = None) -> int:
    parser = JsonArgumentParser(description=__doc__)
    parser.add_argument("--inventory", type=Path, default=DEFAULT_INVENTORY)
    parser.add_argument("--workspace-root", type=Path, required=True)
    parser.add_argument("--require-absent", action="store_true")
    try:
        args = parser.parse_args(argv)
        inventory_path = _canonical_inventory_path(args.inventory, args.workspace_root)
        inventory_raw = read_surface(inventory_path)
        inventory = parse_inventory(inventory_raw)
        _require_catalog_ceiling(inventory, inventory_path)
        report = verify_legacy_absence(
            inventory,
            args.workspace_root,
            require_absent=args.require_absent,
            inventory_raw=inventory_raw,
        )
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        if report["indeterminate_reasons"]:
            return 2
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
