from __future__ import annotations

from pathlib import Path
from typing import Any

from pullwise_worker.review_worker_v1 import (
    materialize_agent_bundle_plan,
    prepare_bundle_planning_input,
    write_json,
)


def materialize_test_bundle_plan(run_dir: Path) -> dict[str, Any]:
    """Materialize a valid Agent grouping for renderer-focused tests."""

    job = {
        "model_profile": {
            "default_model": "gpt-5.5",
            "core_effort": "high",
        },
        "review_request": {
            "policy": {
                "allow_source_modification": False,
                "allow_dependency_install": False,
                "allow_network": False,
                "helper_scripts_standard_library_only": True,
                "turn_timeout_seconds": 1800,
                "reviewer_concurrency": 2,
                "max_bundles": 64,
                "max_reviewer_assignments": 128,
            },
            "budget": {"max_wall_time_seconds": 14400},
        },
        "repositoryLimits": {
            "maxFiles": 2000,
            "maxBytes": 50 * 1024 * 1024,
        },
    }
    planning_input = prepare_bundle_planning_input(run_dir, job)
    items = planning_input.get("items")
    items = items if isinstance(items, list) else []
    groups = []
    for tier in ("P0", "P1", "P2"):
        paths = [
            str(item.get("path") or "")
            for item in items
            if isinstance(item, dict)
            and str(item.get("tier") or "") == tier
            and str(item.get("path") or "")
        ]
        if not paths:
            continue
        groups.append(
            {
                "group_id": f"{tier.lower()}-test-fixture",
                "tier": tier,
                "title": f"{tier} test fixture group",
                "paths": paths,
                "grouping_reasons": [
                    "test fixture preserves routed tiers while exercising the production materializer"
                ],
            }
        )
    write_json(
        run_dir / "bundle-grouping.json",
        {
            "schema_version": "bundle-grouping/v1",
            "run_id": run_dir.name,
            "groups": groups,
        },
    )
    return materialize_agent_bundle_plan(run_dir, job)
