"""Strict machine contract for the Agent-First legacy-removal inventory."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
import re
from typing import Any

from scripts.agent_first_contract_files import REPOSITORY_DIRS, read_surface
from scripts.agent_first_legacy_catalog import (
    ALLOWED_BOUNDED_EXCLUSIONS,
    ALLOWED_WHOLE_EXCLUSIONS,
    CATALOG_FIELDS,
    EXPECTED_D27,
    EXPECTED_FROZEN_BASELINE,
)


SCHEMA_ID = "pullwise-agent-first-legacy-removal-inventory/v1"
TOP_LEVEL_KEYS = {
    "schema_id",
    "inventory_id",
    "catalog_sha256",
    "d27",
    "frozen_baseline",
    "signatures",
    "evidence_exclusions",
    "surfaces",
}
EXCLUSION_REASONS = {
    "absence_gate_control",
    "d27_evidence",
    "immutable_decision_history",
}
ID_PATTERN = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
RESERVED_WINDOWS_NAMES = {
    "aux",
    "con",
    "nul",
    "prn",
    *(f"com{number}" for number in range(1, 10)),
    *(f"lpt{number}" for number in range(1, 10)),
    "conin\u0024",
    "conout\u0024",
}
FORBIDDEN_WINDOWS_PATH_CHARS = frozenset(map(chr, (34, 42, 60, 62, 63, 124)))


class InventoryError(ValueError):
    """The deletion inventory is malformed, ambiguous, or not bound to D27."""


def reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise InventoryError(f"duplicate_key:{key}")
        result[key] = value
    return result


def _exact(value: object, keys: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise InventoryError(f"{label}:keys")
    return value


def _text(value: object, label: str, *, single_line: bool = False) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or "\0" in value:
        raise InventoryError(f"{label}:text")
    if single_line and any(character in value for character in "\r\n"):
        raise InventoryError(f"{label}:single_line")
    return value


def _identifier(value: object, label: str) -> str:
    text = _text(value, label, single_line=True)
    if ID_PATTERN.fullmatch(text) is None:
        raise InventoryError(f"{label}:identifier")
    return text


def _sha256(value: object, label: str) -> str:
    digest = _text(value, label, single_line=True)
    if SHA256_PATTERN.fullmatch(digest) is None:
        raise InventoryError(f"{label}:sha256")
    return digest


def catalog_sha256(value: dict[str, Any]) -> str:
    payload = {key: value[key] for key in CATALOG_FIELDS}
    canonical = json.dumps(
        payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()



def validate_relative_path(value: object, label: str) -> str:
    text = _text(value, label, single_line=True)
    path = PurePosixPath(text)
    if (
        "\\" in text
        or ":" in text
        or path.is_absolute()
        or text != path.as_posix()
        or any(part in {"", ".", ".."} for part in path.parts)
        or not FORBIDDEN_WINDOWS_PATH_CHARS.isdisjoint(text)
    ):
        raise InventoryError(f"{label}:unsafe_path")
    for part in path.parts:
        if part.endswith((" ", ".")) or any(ord(character) < 32 for character in part):
            raise InventoryError(f"{label}:unsafe_path")
        if part.split(".", 1)[0].casefold() in RESERVED_WINDOWS_NAMES:
            raise InventoryError(f"{label}:reserved_path")
    return text


def _repo(value: object, label: str) -> str:
    repo = _identifier(value, label)
    if repo not in REPOSITORY_DIRS:
        raise InventoryError(f"{label}:unknown")
    return repo


def _sorted_ids(value: object, label: str, *, known: set[str] | None = None) -> list[str]:
    if not isinstance(value, list) or not value:
        raise InventoryError(f"{label}:array")
    result = [_identifier(item, f"{label}[]") for item in value]
    if result != sorted(set(result)):
        raise InventoryError(f"{label}:not_sorted_unique")
    if known is not None and not set(result) <= known:
        raise InventoryError(f"{label}:unknown")
    return result


def validate_inventory(value: object) -> dict[str, Any]:
    root = _exact(value, TOP_LEVEL_KEYS, "inventory")
    if root["schema_id"] != SCHEMA_ID:
        raise InventoryError("inventory:schema_id")
    _identifier(root["inventory_id"], "inventory_id")
    declared_catalog_sha256 = _sha256(root["catalog_sha256"], "catalog_sha256")
    binding = _exact(
        root["d27"],
        {"register_path", "decision_id", "selected_option_id", "resolution_sha256"},
        "d27",
    )
    validate_relative_path(binding["register_path"], "d27.register_path")
    if binding != EXPECTED_D27:
        raise InventoryError("d27:not_canonical")

    frozen_baseline = _exact(
        root["frozen_baseline"],
        {"path", "baseline_id", "text_sha256", "surface_ids"},
        "frozen_baseline",
    )
    validate_relative_path(frozen_baseline["path"], "frozen_baseline.path")
    _identifier(frozen_baseline["baseline_id"], "frozen_baseline.baseline_id")
    _sha256(frozen_baseline["text_sha256"], "frozen_baseline.text_sha256")
    _sorted_ids(frozen_baseline["surface_ids"], "frozen_baseline.surface_ids")
    if frozen_baseline != EXPECTED_FROZEN_BASELINE:
        raise InventoryError("frozen_baseline:not_canonical")

    signatures = root["signatures"]
    if not isinstance(signatures, list) or not signatures:
        raise InventoryError("signatures:array")
    signature_ids: list[str] = []
    literals: list[str] = []
    for index, value in enumerate(signatures):
        item = _exact(value, {"id", "literal"}, f"signatures[{index}]")
        signature_ids.append(_identifier(item["id"], f"signatures[{index}].id"))
        literals.append(_text(item["literal"], f"signatures[{index}].literal", single_line=True))
    if signature_ids != sorted(set(signature_ids)) or len(literals) != len(set(literals)):
        raise InventoryError("signatures:not_canonical")
    known_signatures = set(signature_ids)

    exclusions = root["evidence_exclusions"]
    if not isinstance(exclusions, list) or not exclusions:
        raise InventoryError("evidence_exclusions:array")
    exclusion_ids: list[str] = []
    canonical_paths: dict[tuple[str, str], str] = {}
    whole_paths: set[tuple[str, str]] = set()
    bounded_paths: set[tuple[str, str]] = set()
    seen_exclusions: set[tuple[object, ...]] = set()
    for index, value in enumerate(exclusions):
        label = f"evidence_exclusions[{index}]"
        item = _exact(
            value,
            {"id", "repo", "path", "reason", "start_marker", "end_marker"},
            label,
        )
        exclusion_ids.append(_identifier(item["id"], f"{label}.id"))
        repo = _repo(item["repo"], f"{label}.repo")
        path = validate_relative_path(item["path"], f"{label}.path")
        reason = item["reason"]
        if reason not in EXCLUSION_REASONS:
            raise InventoryError(f"{label}.reason")
        start, end = item["start_marker"], item["end_marker"]
        if (start is None) != (end is None):
            raise InventoryError(f"{label}.markers")
        if start is None:
            whole_paths.add((repo, path.casefold()))
            exclusion_key = (repo, path, reason, None, None)
            if exclusion_key[:3] not in ALLOWED_WHOLE_EXCLUSIONS:
                raise InventoryError(f"{label}:unapproved")
        else:
            start = _text(start, f"{label}.start_marker", single_line=True)
            end = _text(end, f"{label}.end_marker", single_line=True)
            if start == end or start in end or end in start:
                raise InventoryError(f"{label}.markers")
            bounded_paths.add((repo, path.casefold()))
            exclusion_key = (repo, path, reason, start, end)
            if exclusion_key not in ALLOWED_BOUNDED_EXCLUSIONS:
                raise InventoryError(f"{label}:unapproved")
        if exclusion_key in seen_exclusions:
            raise InventoryError("evidence_exclusions:duplicate")
        seen_exclusions.add(exclusion_key)
        key = (repo, path.casefold())
        prior = canonical_paths.setdefault(key, path)
        if prior != path:
            raise InventoryError("evidence_exclusions:casefold_path_collision")
    if whole_paths & bounded_paths:
        raise InventoryError("evidence_exclusions:whole_bounded_collision")
    if exclusion_ids != sorted(set(exclusion_ids)):
        raise InventoryError("evidence_exclusions:not_sorted_unique")

    surfaces = root["surfaces"]
    if not isinstance(surfaces, list) or not surfaces:
        raise InventoryError("surfaces:array")
    surface_ids: list[str] = []
    surface_paths: set[tuple[str, str]] = set()
    referenced_signatures: set[str] = set()
    for index, value in enumerate(surfaces):
        label = f"surfaces[{index}]"
        item = _exact(
            value,
            {"id", "repo", "path", "signature_occurrence_ceilings"},
            label,
        )
        surface_ids.append(_identifier(item["id"], f"{label}.id"))
        repo = _repo(item["repo"], f"{label}.repo")
        path = validate_relative_path(item["path"], f"{label}.path")
        ceilings = item["signature_occurrence_ceilings"]
        if not isinstance(ceilings, dict) or not ceilings:
            raise InventoryError(f"{label}.signature_occurrence_ceilings:object")
        ceiling_signature_ids = [
            _identifier(key, f"{label}.signature_occurrence_ceilings.key")
            for key in ceilings
        ]
        if ceiling_signature_ids != sorted(ceiling_signature_ids):
            raise InventoryError(
                f"{label}.signature_occurrence_ceilings:not_sorted_unique"
            )
        if not set(ceiling_signature_ids) <= known_signatures:
            raise InventoryError(f"{label}.signature_occurrence_ceilings:unknown")
        for signature_id, ceiling in ceilings.items():
            if (
                isinstance(ceiling, bool)
                or not isinstance(ceiling, int)
                or ceiling <= 0
            ):
                raise InventoryError(
                    f"{label}.signature_occurrence_ceilings.{signature_id}:positive_integer"
                )
        key = (repo, path.casefold())
        if key in surface_paths:
            raise InventoryError("surfaces:casefold_path_collision")
        if key in whole_paths:
            raise InventoryError("surfaces:whole_file_exclusion_collision")
        surface_paths.add(key)
        referenced_signatures.update(ceiling_signature_ids)
    if surface_ids != sorted(set(surface_ids)):
        raise InventoryError("surfaces:not_sorted_unique")
    if referenced_signatures != known_signatures:
        raise InventoryError("signatures:must_be_referenced")
    if declared_catalog_sha256 != catalog_sha256(root):
        raise InventoryError("catalog_sha256:mismatch")
    return root


def parse_inventory(raw: bytes) -> dict[str, Any]:
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=reject_duplicate_keys,
            parse_constant=lambda item: (_ for _ in ()).throw(
                InventoryError(f"non_finite_number:{item}")
            ),
        )
    except InventoryError:
        raise
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise InventoryError("inventory:unreadable") from exc
    return validate_inventory(value)


def load_inventory(path: Path) -> dict[str, Any]:
    try:
        raw = read_surface(path)
    except OSError as exc:
        raise InventoryError("inventory:unreadable") from exc
    return parse_inventory(raw)
