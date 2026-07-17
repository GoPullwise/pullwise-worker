"""Cross-field invariants that JSON Schema cannot express on its own."""

from __future__ import annotations

from typing import Callable

from .agent_kernel_canonical import canonical_sha256
from .agent_kernel_identity import legacy_v1_task_mapping
from .agent_kernel_schema_validation import SchemaValidationError


SemanticValidator = Callable[[dict[str, object]], None]


def _fail(code: str, path: str = "$") -> None:
    raise SchemaValidationError(code, path)


def _content_sha(value: object) -> object:
    return value.get("sha256") if isinstance(value, dict) else None


def _validate_policy(instance: dict[str, object]) -> None:
    if instance.get("digest") != canonical_sha256(instance, digest_field="digest"):
        _fail("digest_mismatch", "$.digest")
    granted = instance.get("granted_capabilities")
    denied = instance.get("denied_capabilities")
    granted_ids = set(granted) if isinstance(granted, list) else set()
    denied_ids = [
        entry.get("id") for entry in denied if isinstance(entry, dict)
    ] if isinstance(denied, list) else []
    if len(denied_ids) != len(set(denied_ids)):
        _fail("denied_capability_duplicate", "$.denied_capabilities")
    if granted_ids.intersection(denied_ids):
        _fail("capability_grant_deny_overlap", "$.granted_capabilities")
    if instance.get("source_write_mode") == "read_only" and instance.get(
        "allowed_write_roots"
    ):
        _fail("read_only_write_root_forbidden", "$.allowed_write_roots")
    network = instance.get("agent_tool_network")
    if isinstance(network, dict):
        if network.get("mode") == "deny" and network.get("origins"):
            _fail("denied_network_origin_forbidden", "$.agent_tool_network.origins")
        if network.get("mode") == "allowlist" and not network.get("origins"):
            _fail("network_allowlist_empty", "$.agent_tool_network.origins")
    budgets = instance.get("budgets")
    if isinstance(budgets, dict):
        if instance.get("terminalization_reserve_ms", 0) > budgets.get("wall_ms", 0):
            _fail("terminalization_reserve_exceeds_wall_budget")
        if instance.get("max_agent_sessions_total", 0) > budgets.get(
            "agent_sessions", 0
        ):
            _fail("session_limit_exceeds_budget")
        if instance.get("max_attempts", 0) > budgets.get("attempts", 0):
            _fail("attempt_limit_exceeds_budget")


def _validate_task_record(instance: dict[str, object]) -> None:
    if instance.get("request_digest") != _content_sha(instance.get("request_ref")):
        _fail("request_digest_mismatch", "$.request_digest")
    if instance.get("policy_digest") != _content_sha(instance.get("policy_ref")):
        _fail("policy_digest_mismatch", "$.policy_digest")
    if (instance.get("charter_version") == 0) != (instance.get("charter_ref") is None):
        _fail("charter_pointer_mismatch", "$.charter_ref")
    if (instance.get("current_checkpoint_generation") == 0) != (
        instance.get("current_checkpoint_hash") is None
    ):
        _fail("checkpoint_pointer_mismatch", "$.current_checkpoint_hash")
    lifecycle = instance.get("lifecycle")
    terminal_kind = instance.get("terminal_kind")
    terminal_values = (
        instance.get("result_ref"),
        instance.get("result_digest"),
        instance.get("outcome"),
        instance.get("terminal_at"),
    )
    if lifecycle != "TERMINAL":
        if terminal_kind is not None or any(value is not None for value in terminal_values):
            _fail("nonterminal_result_present")
        return
    if terminal_kind == "task_result":
        if any(value is None for value in terminal_values):
            _fail("terminal_result_incomplete")
    elif terminal_kind == "transport_abandoned":
        if any(value is not None for value in terminal_values[:3]) or terminal_values[3] is None:
            _fail("transport_abandonment_invalid")
    else:
        _fail("terminal_kind_missing", "$.terminal_kind")


def _validate_task_request(instance: dict[str, object]) -> None:
    entries: list[object] = []
    for key in ("acceptance_criteria", "constraints"):
        value = instance.get(key)
        if isinstance(value, list):
            entries.extend(value)
    source_ids = [
        entry.get("source_id") for entry in entries if isinstance(entry, dict)
    ]
    if len(source_ids) != len(set(source_ids)):
        _fail("source_id_duplicate")
    interaction = instance.get("interaction_policy")
    if isinstance(interaction, dict) and interaction.get("mode") == "unavailable":
        if interaction.get("input_deadline_ms") != 0 or interaction.get(
            "approval_deadline_ms"
        ) != 0:
            _fail("unavailable_interaction_deadline_nonzero")


def _validate_charter(instance: dict[str, object]) -> None:
    if instance.get("digest") != canonical_sha256(instance, digest_field="digest"):
        _fail("digest_mismatch", "$.digest")
    version = instance.get("charter_version")
    predecessor = instance.get("previous_charter_ref")
    if version == 1 and predecessor is not None:
        _fail("genesis_charter_has_predecessor", "$.previous_charter_ref")
    if isinstance(version, int) and version > 1 and predecessor is None:
        _fail("charter_predecessor_missing", "$.previous_charter_ref")
    if isinstance(predecessor, dict) and predecessor.get("content_schema_id") != (
        "task-charter/v1"
    ):
        _fail("charter_predecessor_schema_invalid", "$.previous_charter_ref")


def _validate_interaction_request(instance: dict[str, object]) -> None:
    kind = instance.get("kind")
    capability = instance.get("requested_capability")
    if kind == "approval" and capability is None:
        _fail("approval_capability_missing", "$.requested_capability")
    if kind == "input" and capability is not None:
        _fail("input_capability_forbidden", "$.requested_capability")
    created_at = instance.get("created_at")
    deadline_at = instance.get("deadline_at")
    if isinstance(created_at, str) and isinstance(deadline_at, str) and (
        deadline_at <= created_at
    ):
        _fail("interaction_deadline_invalid", "$.deadline_at")


def _validate_waiver(instance: dict[str, object]) -> None:
    issued_at = instance.get("issued_at")
    expires_at = instance.get("expires_at")
    if isinstance(issued_at, str) and isinstance(expires_at, str) and (
        expires_at <= issued_at
    ):
        _fail("waiver_time_window_invalid", "$.expires_at")


def _validate_requirement(instance: dict[str, object]) -> None:
    source_kind = instance.get("source_kind")
    requirement_id = instance.get("requirement_id")
    if not isinstance(requirement_id, str) or not requirement_id.startswith(
        f"req_{source_kind}_"
    ):
        _fail("requirement_id_source_mismatch", "$.requirement_id")
    if source_kind != "derived":
        if instance.get("mandatory") is not True:
            _fail("explicit_requirement_not_mandatory", "$.mandatory")
        expected = "safety_necessary" if source_kind == "policy" else "explicit"
        if instance.get("necessity") != expected:
            _fail("explicit_requirement_necessity_invalid", "$.necessity")
        if instance.get("supersedes"):
            _fail("explicit_requirement_supersedes_forbidden", "$.supersedes")
        return
    if instance.get("mandatory") is True:
        if instance.get("necessity") not in {
            "mechanically_necessary",
            "safety_necessary",
        }:
            _fail("derived_mandatory_necessity_invalid")
        if not instance.get("rationale"):
            _fail("derived_mandatory_rationale_missing", "$.rationale")
        if not instance.get("parent_requirement_ids"):
            _fail("derived_mandatory_parent_missing")
    elif instance.get("necessity") != "advisory":
        _fail("derived_advisory_necessity_invalid")


def _validate_legacy_mapping(instance: dict[str, object]) -> None:
    expected = legacy_v1_task_mapping(
        str(instance.get("scan_id") or ""),
        instance.get("transport_epoch"),  # type: ignore[arg-type]
    )
    if expected.get("task_id") != instance.get("task_id"):
        _fail("task_id_digest_mismatch", "$.task_id")


VALIDATORS: dict[str, SemanticValidator] = {
    "effective-execution-policy/v1": _validate_policy,
    "interaction-request/v1": _validate_interaction_request,
    "legacy-v1-task-mapping/v1": _validate_legacy_mapping,
    "requirement-entry/v1": _validate_requirement,
    "task-charter/v1": _validate_charter,
    "task-record/v1": _validate_task_record,
    "task-request/v1": _validate_task_request,
    "waiver-event/v1": _validate_waiver,
}


def validate_contract_semantics(schema_id: str, instance: object) -> None:
    validator = VALIDATORS.get(schema_id)
    if validator is not None and isinstance(instance, dict):
        validator(instance)
