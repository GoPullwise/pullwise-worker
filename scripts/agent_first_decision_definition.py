"""Canonical immutable projection of the reviewed D1-D26 packet."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from scripts.agent_first_decision_catalog import (
    CATALOG_BY_ID,
    NORMATIVE_UNIT_CATALOG,
    REQUIRED_CATALOG,
)


def required_definition_sha256(root: dict[str, Any]) -> str:
    required_ids = set(CATALOG_BY_ID)
    projection = {
        "register_id": root["register_id"],
        "document": root["document"],
        "normative_unit_catalog": list(NORMATIVE_UNIT_CATALOG),
        "normative_units": [
            {
                "id": unit["id"],
                "decision_ids": [
                    decision_id
                    for decision_id in unit["decision_ids"]
                    if decision_id in required_ids
                ],
            }
            for unit in root["normative_units"]
        ],
        "decisions": [
            {
                key: value
                for key, value in decision.items()
                if key not in {"status", "resolution"}
            }
            for decision in root["decisions"][: len(REQUIRED_CATALOG)]
        ],
    }
    canonical = json.dumps(
        projection, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()
