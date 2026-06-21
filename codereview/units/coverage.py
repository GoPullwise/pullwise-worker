from __future__ import annotations

from .planner import PRODUCTION_SYMBOL_KINDS, STATE_KINDS, TRUST_TAGS


def build_unit_coverage(graph: dict, inventory: dict, units: list[dict], review_results: list[dict] | None = None) -> dict:
    review_results = review_results or []
    unit_ids = {str(unit.get("unit_id") or "") for unit in units}
    baseline_reviewed = {
        str((item.get("task") or {}).get("unit_id") or "")
        for item in review_results
        if _successful_review_result(item) and (item.get("task") or {}).get("focus") == "correctness"
    }
    production = _production_symbol_ids(graph)
    unit_node_ids = {str(node_id) for unit in units for node_id in unit.get("node_ids", [])}
    high_risk = [unit for unit in units if _high_risk(unit)]
    specialist_reviewed = {
        str((item.get("task") or {}).get("unit_id") or "")
        for item in review_results
        if _successful_review_result(item) and (item.get("task") or {}).get("focus") not in {"", "correctness"}
    }
    boundary_units = [unit for unit in units if unit.get("unit_type") == "cross_boundary"]
    global_units = [unit for unit in units if unit.get("unit_type") == "global_invariant"]
    return {
        "inventory_files": len(inventory.get("files", []) or []),
        "analyzed_files": sum(1 for item in inventory.get("files", []) if isinstance(item, dict) and item.get("scope") == "analyze"),
        "explicitly_excluded_files": sum(1 for item in inventory.get("files", []) if isinstance(item, dict) and item.get("scope") == "excluded"),
        "production_symbols": len(production),
        "covered_production_symbols": len(production & unit_node_ids),
        "uncovered_production_symbols": sorted(production - unit_node_ids)[:200],
        "review_units": len(units),
        "baseline_reviewed_units": len(unit_ids & baseline_reviewed),
        "high_risk_units": len(high_risk),
        "specialist_reviewed_high_risk_units": sum(1 for unit in high_risk if str(unit.get("unit_id") or "") in specialist_reviewed),
        "cross_boundary_reviews": len(boundary_units),
        "global_invariant_reviews": len(global_units),
        "unit_ids": sorted(unit_ids),
    }


def require_full_unit_coverage(coverage: dict, *, require_baseline_review: bool = False) -> None:
    errors = []
    if coverage.get("production_symbols") != coverage.get("covered_production_symbols"):
        errors.append("not all production symbols are assigned to review units")
    if require_baseline_review and coverage.get("review_units") != coverage.get("baseline_reviewed_units"):
        errors.append("not all review units received baseline correctness review")
    if errors:
        raise RuntimeError("; ".join(errors))


def _production_symbol_ids(graph: dict) -> set[str]:
    return {
        str(node.get("id") or "")
        for node in graph.get("nodes", []) or []
        if isinstance(node, dict)
        and node.get("kind") in PRODUCTION_SYMBOL_KINDS
        and not _is_test_file(str(node.get("file") or ""))
    }


def _successful_review_result(item: object) -> bool:
    return isinstance(item, dict) and item.get("status") == "ok" and isinstance(item.get("task"), dict)


def _high_risk(unit: dict) -> bool:
    tags = {str(tag) for tag in unit.get("risk_tags", []) if str(tag)}
    return bool(tags & (TRUST_TAGS | {"state", "db-write", "cross-boundary", "global-invariant"}))


def _is_test_file(path: str) -> bool:
    lower = path.lower()
    return "/test" in lower or "/tests" in lower or lower.endswith((".test.py", ".spec.py", ".test.js", ".spec.js", ".test.ts", ".spec.ts"))
