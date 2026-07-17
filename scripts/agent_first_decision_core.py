"""Schema and semantic validation for the Agent-First decision register."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import date
from pathlib import PurePosixPath
from typing import Any

from scripts.agent_first_decision_catalog import (
    ALLOWED_EFFECTS,
    AUTHORITIES,
    CATALOG_BY_ID,
    NORMATIVE_UNIT_CATALOG,
    QUESTION_ORDER,
    REQUIRED_CATALOG,
    REQUIRED_DEFINITION_SHA256,
    RESOLUTION_DOMAIN,
    SCHEMA_ID,
    SLICES,
)
from scripts.agent_first_decision_definition import required_definition_sha256
from scripts.agent_first_decision_supersession import supersession_error


TOP_LEVEL_KEYS = {
    "schema_id", "register_id", "active_decision_id", "question_order",
    "document", "normative_units", "decisions",
}
DECISION_KEYS = {
    "id", "key", "scope", "title", "question", "status", "depends_on",
    "activation", "required_by_slice", "effects", "source_refs",
    "affected_units", "options", "recommended_option_id", "resolution",
    "supersedes",
}
OPTION_KEYS = {"id", "summary", "rationale", "consequences"}
RESOLUTION_KEYS = {
    "kind", "selected_option_id", "custom_text", "decision_text", "authority",
    "decided_at", "evidence_refs", "resolution_sha256",
}


class DecisionRegisterFormatError(ValueError):
    pass


def _exact_keys(value: object, expected: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != expected:
        raise DecisionRegisterFormatError(f"{label}:keys")
    return value


def _text(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value.strip() != value
        or any(
            unicodedata.category(character) in {"Cc", "Cf", "Cs"}
            for character in value
        )
    ):
        raise DecisionRegisterFormatError(f"{label}:text")
    return value


def _nullable_text(value: object, label: str) -> str | None:
    if value is None:
        return None
    return _text(value, label)


def _text_list(value: object, label: str, *, empty: bool = False) -> list[str]:
    if not isinstance(value, list) or (not value and not empty):
        raise DecisionRegisterFormatError(f"{label}:list")
    result = [_text(item, f"{label}[]") for item in value]
    if len(result) != len(set(result)):
        raise DecisionRegisterFormatError(f"{label}:duplicate")
    return result


def _relative_path(value: object, label: str) -> str:
    text = _text(value, label)
    path = PurePosixPath(text)
    if (
        chr(92) in text or "\0" in text or ":" in text or path.is_absolute()
        or text != path.as_posix()
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise DecisionRegisterFormatError(f"{label}:relative_path")
    return text


def canonical_resolution_sha256(
    decision_id: str,
    resolution: dict[str, object],
    supersedes: list[str] | tuple[str, ...] = (),
) -> str:
    payload = {key: value for key, value in resolution.items() if key != "resolution_sha256"}
    canonical = json.dumps(
        {"resolution": payload, "supersedes": list(supersedes)},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    identity = decision_id.encode("utf-8")
    material = RESOLUTION_DOMAIN + len(identity).to_bytes(4, "big") + identity + canonical
    return hashlib.sha256(material).hexdigest()


def selected_option_id(decision: dict[str, Any]) -> str | None:
    resolution = decision.get("resolution")
    if not isinstance(resolution, dict):
        return None
    value = resolution.get("selected_option_id")
    return value if isinstance(value, str) else None


def decision_applicability(
    register: dict[str, Any], decision_id: str
) -> str:
    decisions = {item["id"]: item for item in register["decisions"]}
    decision = decisions[decision_id]
    activation = decision["activation"]
    if activation is None:
        return "active"
    source = decisions[activation["decision_id"]]
    if source["status"] != "resolved":
        return "unknown"
    if source["resolution"]["kind"] == "custom":
        return "active"
    return (
        "active"
        if selected_option_id(source) == activation["selected_option_id"]
        else "inactive"
    )


def _dependency_satisfied(register: dict[str, Any], decision_id: str) -> bool:
    decision = next(item for item in register["decisions"] if item["id"] == decision_id)
    return decision["status"] == "resolved" or decision_applicability(register, decision_id) == "inactive"


def expected_active_decision(register: dict[str, Any]) -> str | None:
    decisions = {item["id"]: item for item in register["decisions"]}
    for decision_id in register["question_order"]:
        decision = decisions[decision_id]
        if decision["status"] != "pending":
            continue
        if decision_applicability(register, decision_id) != "active":
            continue
        if all(_dependency_satisfied(register, item) for item in decision["depends_on"]):
            return decision_id
    return None


def _validate_resolution(decision: dict[str, Any], label: str) -> None:
    resolution = decision["resolution"]
    if decision["status"] == "pending":
        if resolution is not None:
            raise DecisionRegisterFormatError(f"{label}.resolution:pending")
        return
    if decision["status"] != "resolved":
        raise DecisionRegisterFormatError(f"{label}.status")
    item = _exact_keys(resolution, RESOLUTION_KEYS, f"{label}.resolution")
    if item["kind"] not in {"option", "custom"}:
        raise DecisionRegisterFormatError(f"{label}.resolution.kind")
    option_ids = {option["id"] for option in decision["options"]}
    selected = _text(item["selected_option_id"], f"{label}.resolution.selected_option_id")
    if selected not in option_ids:
        raise DecisionRegisterFormatError(f"{label}.resolution.selected_option_id")
    custom = _nullable_text(item["custom_text"], f"{label}.resolution.custom_text")
    if (item["kind"] == "custom") != (custom is not None):
        raise DecisionRegisterFormatError(f"{label}.resolution.custom_text")
    _text(item["decision_text"], f"{label}.resolution.decision_text")
    if item["authority"] not in AUTHORITIES:
        raise DecisionRegisterFormatError(f"{label}.resolution.authority")
    decided_at = _text(item["decided_at"], f"{label}.resolution.decided_at")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", decided_at) is None:
        raise DecisionRegisterFormatError(f"{label}.resolution.decided_at")
    try:
        if date.fromisoformat(decided_at).isoformat() != decided_at:
            raise ValueError
    except ValueError:
        raise DecisionRegisterFormatError(f"{label}.resolution.decided_at")
    _text_list(item["evidence_refs"], f"{label}.resolution.evidence_refs")
    digest = _text(item["resolution_sha256"], f"{label}.resolution.resolution_sha256")
    if digest != canonical_resolution_sha256(
        decision["id"], item, decision["supersedes"]
    ):
        raise DecisionRegisterFormatError(f"{label}.resolution.resolution_sha256")


def _assert_acyclic(decisions: list[dict[str, Any]]) -> None:
    graph = {item["id"]: item["depends_on"] for item in decisions}
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(decision_id: str) -> None:
        if decision_id in visiting:
            raise DecisionRegisterFormatError("decisions:cycle")
        if decision_id in visited:
            return
        visiting.add(decision_id)
        for dependency in graph[decision_id]:
            visit(dependency)
        visiting.remove(decision_id)
        visited.add(decision_id)

    for decision_id in graph:
        visit(decision_id)


def _validate_units(root: dict[str, Any]) -> None:
    units = root["normative_units"]
    if not isinstance(units, list) or len(units) != len(NORMATIVE_UNIT_CATALOG):
        raise DecisionRegisterFormatError("normative_units:required_catalog")
    expected_ids = [item["id"] for item in NORMATIVE_UNIT_CATALOG]
    actual_ids: list[str] = []
    unit_decisions: dict[str, list[str]] = {}
    for index, value in enumerate(units):
        item = _exact_keys(value, {"id", "decision_ids"}, f"normative_units[{index}]")
        actual_ids.append(_text(item["id"], f"normative_units[{index}].id"))
        unit_decisions[item["id"]] = _text_list(
            item["decision_ids"], f"normative_units[{index}].decision_ids", empty=True
        )
    if actual_ids != expected_ids:
        raise DecisionRegisterFormatError("normative_units:required_catalog")
    reverse = {
        unit_id: [
            decision["id"] for decision in root["decisions"]
            if unit_id in decision["affected_units"]
        ]
        for unit_id in expected_ids
    }
    if unit_decisions != reverse:
        raise DecisionRegisterFormatError("normative_units:bidirectional")


def validate_register(register: object) -> dict[str, Any]:
    root = _exact_keys(register, TOP_LEVEL_KEYS, "register")
    if root["schema_id"] != SCHEMA_ID:
        raise DecisionRegisterFormatError("register:schema_id")
    _text(root["register_id"], "register_id")
    document = _exact_keys(root["document"], {"path", "start_marker", "end_marker"}, "document")
    _relative_path(document["path"], "document.path")
    if _text(document["start_marker"], "document.start_marker") == _text(document["end_marker"], "document.end_marker"):
        raise DecisionRegisterFormatError("document:markers")
    if not isinstance(root["decisions"], list) or len(root["decisions"]) < len(REQUIRED_CATALOG):
        raise DecisionRegisterFormatError("decisions:required_catalog")

    ids: list[str] = []
    keys: list[str] = []
    unit_ids = {item["id"] for item in NORMATIVE_UNIT_CATALOG}
    for index, value in enumerate(root["decisions"]):
        label = f"decisions[{index}]"
        item = _exact_keys(value, DECISION_KEYS, label)
        decision_id = _text(item["id"], f"{label}.id")
        if decision_id != f"D{index + 1}":
            raise DecisionRegisterFormatError(f"{label}.id:order")
        ids.append(decision_id)
        keys.append(_text(item["key"], f"{label}.key"))
        _text(item["scope"], f"{label}.scope")
        _text(item["title"], f"{label}.title")
        _text(item["question"], f"{label}.question")
        _text_list(item["depends_on"], f"{label}.depends_on", empty=True)
        if item["required_by_slice"] not in SLICES:
            raise DecisionRegisterFormatError(f"{label}.required_by_slice")
        effects = _text_list(item["effects"], f"{label}.effects")
        if not set(effects) <= ALLOWED_EFFECTS:
            raise DecisionRegisterFormatError(f"{label}.effects")
        _text_list(item["source_refs"], f"{label}.source_refs")
        affected = _text_list(item["affected_units"], f"{label}.affected_units")
        if not set(affected) <= unit_ids:
            raise DecisionRegisterFormatError(f"{label}.affected_units")
        _text_list(item["supersedes"], f"{label}.supersedes", empty=True)
        if item["activation"] is not None:
            activation = _exact_keys(item["activation"], {"decision_id", "selected_option_id"}, f"{label}.activation")
            _text(activation["decision_id"], f"{label}.activation.decision_id")
            _text(activation["selected_option_id"], f"{label}.activation.selected_option_id")
        if not isinstance(item["options"], list) or len(item["options"]) < 2:
            raise DecisionRegisterFormatError(f"{label}.options:list")
        option_ids: list[str] = []
        for option_index, option_value in enumerate(item["options"]):
            option_label = f"{label}.options[{option_index}]"
            option = _exact_keys(option_value, OPTION_KEYS, option_label)
            option_ids.append(_text(option["id"], f"{option_label}.id"))
            _text(option["summary"], f"{option_label}.summary")
            _text(option["rationale"], f"{option_label}.rationale")
            _text_list(option["consequences"], f"{option_label}.consequences")
        if len(option_ids) != len(set(option_ids)):
            raise DecisionRegisterFormatError(f"{label}.options:duplicate")
        if item["recommended_option_id"] not in option_ids:
            raise DecisionRegisterFormatError(f"{label}.recommended_option_id")
        _validate_resolution(item, label)
    if len(keys) != len(set(keys)):
        raise DecisionRegisterFormatError("decisions:duplicate_key")

    for index, expected in enumerate(REQUIRED_CATALOG):
        actual = root["decisions"][index]
        activation = expected["activation"]
        expected_activation = None if activation is None else {
            "decision_id": activation[0], "selected_option_id": activation[1]
        }
        for field in ("id", "key", "scope", "required_by_slice", "depends_on"):
            expected_value = list(expected[field]) if field == "depends_on" else expected[field]
            if actual[field] != expected_value:
                raise DecisionRegisterFormatError("decisions:required_catalog")
        if actual["activation"] != expected_activation or expected["source_ref"] not in actual["source_refs"]:
            raise DecisionRegisterFormatError("decisions:required_catalog")
    id_set = set(ids)
    for index, item in enumerate(root["decisions"]):
        if not set(item["depends_on"]) <= id_set or not set(item["supersedes"]) <= set(ids[:index]):
            raise DecisionRegisterFormatError(f"decisions[{index}]:reference")
        activation = item["activation"]
        if activation is not None:
            if activation["decision_id"] not in set(ids[:index]):
                raise DecisionRegisterFormatError(
                    f"decisions[{index}].activation:source_order"
                )
            source = next(entry for entry in root["decisions"] if entry["id"] == activation["decision_id"])
            if activation["selected_option_id"] not in {option["id"] for option in source["options"]}:
                raise DecisionRegisterFormatError(f"decisions[{index}].activation:selected_option")
    _assert_acyclic(root["decisions"])
    _validate_units(root)
    if required_definition_sha256(root) != REQUIRED_DEFINITION_SHA256:
        raise DecisionRegisterFormatError("register:required_definition")

    order = _text_list(root["question_order"], "question_order")
    if set(order) != id_set or [item for item in order if item in CATALOG_BY_ID] != list(QUESTION_ORDER):
        raise DecisionRegisterFormatError("question_order:required_catalog")
    supersession_failure = supersession_error(root)
    if supersession_failure is not None:
        raise DecisionRegisterFormatError(supersession_failure)
    for index, item in enumerate(root["decisions"]):
        if item["status"] == "resolved":
            if decision_applicability(root, item["id"]) != "active":
                raise DecisionRegisterFormatError(f"decisions[{index}].resolution:inactive")
            if not all(_dependency_satisfied(root, dependency) for dependency in item["depends_on"]):
                raise DecisionRegisterFormatError(f"decisions[{index}].depends_on:unresolved")
    pending_seen = False
    for decision_id in root["question_order"]:
        if decision_applicability(root, decision_id) == "inactive":
            continue
        decision = next(
            item for item in root["decisions"] if item["id"] == decision_id
        )
        if decision["status"] == "pending":
            pending_seen = True
        elif pending_seen:
            raise DecisionRegisterFormatError(
                f"{decision_id}.resolution:out_of_question_order"
            )
    expected_active = expected_active_decision(root)
    if root["active_decision_id"] != expected_active:
        raise DecisionRegisterFormatError("active_decision_id:first_ready_pending")
    return root
