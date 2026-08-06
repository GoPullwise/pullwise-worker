from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from reviewer_spec_cards import graph_has_cycle, safe_argv, verify_cards
from reviewer_spec_json import (
    SpecError,
    canonical_rel,
    exact_keys,
    fail,
    load_json,
    sha256,
    validate_profile,
)
from reviewer_spec_model import (
    CARD_IDS,
    ENTRY_ACTION_COMMANDS,
    GATE_IDS,
    MAIN_REL,
    MANIFEST_REL,
    REQUIRED_FILES,
    SPEC_DIR,
    SPEC_ID,
    SPEC_VERSION,
)


def verify_manifest(root: Path) -> dict[str, Any]:
    manifest = load_json(root / MANIFEST_REL)
    validate_profile(manifest)
    exact_keys(
        manifest,
        {"schema_id", "spec_id", "spec_version", "status", "files"},
        "manifest",
    )
    if manifest["schema_id"] != "reviewer-refactor-spec-manifest/v1":
        fail("manifest.schema_id", str(manifest["schema_id"]))
    if manifest["spec_id"] != SPEC_ID or manifest["spec_version"] != SPEC_VERSION:
        fail("manifest.identity", f"{manifest['spec_id']}:{manifest['spec_version']}")
    if manifest["status"] != "PROPOSED_INERT":
        fail("manifest.status", str(manifest["status"]))
    files = manifest["files"]
    if not isinstance(files, list):
        fail("manifest.files_type", type(files).__name__)
    paths: list[str] = []
    folded: set[str] = set()
    entry_keys = {"path", "size_bytes", "sha256", "media_type", "role"}
    for index, entry in enumerate(files):
        if not isinstance(entry, dict):
            fail("manifest.entry_type", str(index))
        exact_keys(entry, entry_keys, f"manifest.files[{index}]")
        rel = canonical_rel(entry["path"])
        if rel == MANIFEST_REL.as_posix():
            fail("manifest.self_reference", rel)
        if rel.casefold() in folded:
            fail("manifest.case_collision", rel)
        folded.add(rel.casefold())
        paths.append(rel)
        target = root / Path(rel)
        if not target.is_file() or target.is_symlink():
            fail("manifest.file_missing_or_unsafe", rel)
        data = target.read_bytes()
        if len(data) != entry["size_bytes"]:
            fail("manifest.size_mismatch", rel)
        if sha256(data) != entry["sha256"]:
            fail("manifest.hash_mismatch", rel)
        if not re.fullmatch(r"[0-9a-f]{64}", entry["sha256"]):
            fail("manifest.hash_shape", rel)
    expected = tuple(sorted(REQUIRED_FILES, key=lambda item: item.encode("utf-8")))
    if tuple(paths) != expected:
        fail("manifest.closed_set", f"expected={len(expected)} actual={len(paths)}")
    return manifest


def verify_readiness(root: Path) -> dict[str, Any]:
    value = load_json(root / SPEC_DIR / "readiness.json")
    validate_profile(value)
    exact_keys(
        value,
        {
            "schema_id", "spec_id", "spec_version", "activation_state",
            "overall_status", "reason_codes", "gates",
        },
        "readiness",
    )
    if value["spec_id"] != SPEC_ID or value["spec_version"] != SPEC_VERSION:
        fail("readiness.identity", f"{value['spec_id']}:{value['spec_version']}")
    if value["activation_state"] != "PROPOSED_INERT":
        fail("readiness.activation_state", str(value["activation_state"]))
    if value["overall_status"] != "NOT_AUTHORIZED":
        fail("readiness.current_state", str(value["overall_status"]))
    ids = tuple(gate.get("gate_id") for gate in value["gates"])
    if ids != GATE_IDS:
        fail("readiness.gate_set", repr(ids))
    by_id: dict[str, dict[str, Any]] = {}
    for gate in value["gates"]:
        exact_keys(
            gate,
            {"gate_id", "status", "reason_code", "evidence_refs"},
            str(gate.get("gate_id")),
        )
        by_id[gate["gate_id"]] = gate
        for reference in gate["evidence_refs"]:
            rel = reference.split("#", 1)[0]
            canonical_rel(rel)
            if not (root / rel).is_file():
                fail("readiness.evidence_missing", reference)
    if by_id["SPEC-READY-03-MANIFEST"]["status"] != "PASS":
        fail("readiness.manifest_truth", "self-check must be PASS")
    if by_id["SPEC-READY-04-BOOTSTRAP"]["status"] != "FAIL":
        fail("readiness.bootstrap_truth", "collector remains absent")
    if by_id["SPEC-READY-12-EXECUTION"]["status"] != "NOT_AUTHORIZED":
        fail("readiness.execution_truth", "generation 1 is inert")
    return value


def verify_bootstrap(root: Path, cards: dict[str, Any]) -> None:
    value = load_json(root / SPEC_DIR / "bootstrap-command.json")
    validate_profile(value)
    exact_keys(
        value,
        {
            "schema_id", "spec_id", "spec_version", "agent_entry_path",
            "card_generation", "execution_profile", "draft_card_id",
            "install_card_id", "formal_card_id", "minimum_successor_generation",
            "collector_id", "collector_repo_id", "collector_path",
            "collector_expected_state", "collector_sha256", "readiness_status",
            "reason_code", "python_flags", "subcommand", "required_flags",
            "forbidden_flags", "write_scope",
            "expected_process_exit_before_back_validation",
            "activation_requirement",
        },
        "bootstrap-command",
    )
    identity = (value["spec_id"], value["spec_version"])
    if identity != (SPEC_ID, SPEC_VERSION):
        fail("bootstrap.identity", repr(identity))
    card_binding = (
        value["card_generation"], value["execution_profile"],
        value["draft_card_id"], value["install_card_id"], value["formal_card_id"],
    )
    if card_binding != (
        cards["generation"], cards["profile"], "COL-0D", "COL-0F", "GOV-0A"
    ):
        fail("bootstrap.card_binding", repr(card_binding))
    collector = root / value["collector_path"]
    if value["collector_expected_state"] == "absent":
        if collector.exists() or value["collector_sha256"] is not None:
            fail("bootstrap.absent_truth", value["collector_path"])
        if value["readiness_status"] != "FAIL":
            fail("bootstrap.readiness_truth", str(value["readiness_status"]))
    elif value["collector_expected_state"] == "installed":
        if not collector.is_file():
            fail("bootstrap.installed_truth", value["collector_path"])
        digest = value["collector_sha256"] or ""
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            fail("bootstrap.collector_digest", repr(digest))
        if sha256(collector.read_bytes()) != digest:
            fail("bootstrap.collector_digest", value["collector_path"])
    else:
        fail("bootstrap.expected_state", str(value["collector_expected_state"]))


def verify_entry(root: Path, cards: dict[str, Any]) -> None:
    entry = load_json(root / SPEC_DIR / "agent-entry.json")
    validate_profile(entry)
    exact_keys(
        entry,
        {
            "schema_id", "spec_id", "spec_version", "activation_state",
            "authority_state", "current_generation", "execution_profile",
            "manifest_path", "cards_path", "readiness_path", "next_card_id",
            "allowed_action_ids", "actions", "generation_advance_contract",
            "stop_conditions", "completion_predicates",
        },
        "agent-entry",
    )
    identity = (entry["spec_id"], entry["spec_version"])
    if identity != (SPEC_ID, SPEC_VERSION):
        fail("entry.identity", repr(identity))
    state = (entry["activation_state"], entry["authority_state"])
    if state != ("PROPOSED_INERT", "NOT_AUTHORIZED"):
        fail("entry.state", repr(state))
    if (entry["current_generation"], entry["execution_profile"]) != (
        cards["generation"],
        cards["profile"],
    ):
        fail("entry.card_generation", repr(entry["current_generation"]))
    if entry["next_card_id"] != "COL-0D":
        fail("entry.next_card", str(entry["next_card_id"]))
    if entry["allowed_action_ids"] != ["verify-spec", "inspect-current-gates"]:
        fail("entry.allowed_actions", repr(entry["allowed_action_ids"]))
    action_ids: list[str] = []
    action_commands: dict[str, tuple[tuple[str, tuple[str, ...], tuple[int, ...]], ...]] = {}
    for action in entry["actions"]:
        exact_keys(action, {"action_id", "mutates_worktree", "commands"}, "entry.action")
        action_ids.append(action["action_id"])
        if action["mutates_worktree"] or not action["commands"]:
            fail("entry.action_mutation", action["action_id"])
        normalized: list[tuple[str, tuple[str, ...], tuple[int, ...]]] = []
        for command in action["commands"]:
            exact_keys(command, {"cwd_repo", "argv", "expected_exit"}, "entry.command")
            safe_argv(command["argv"], action["action_id"])
            normalized.append(
                (
                    command["cwd_repo"],
                    tuple(command["argv"]),
                    tuple(command["expected_exit"]),
                )
            )
        action_commands[action["action_id"]] = tuple(normalized)
    if action_ids != entry["allowed_action_ids"]:
        fail("entry.action_order", repr(action_ids))
    if action_commands != ENTRY_ACTION_COMMANDS:
        fail("entry.command_binding", repr(action_commands))
    contract = entry["generation_advance_contract"]
    exact_keys(
        contract,
        {
            "schema_id", "minimum_successor_generation", "preconditions",
            "atomic_changes", "cas_keys", "failure_state",
        },
        "entry.generation_advance_contract",
    )
    if contract["minimum_successor_generation"] != 2:
        fail("entry.successor_generation", repr(contract["minimum_successor_generation"]))
    if contract["cas_keys"] != [
        "from_generation", "from_manifest_sha256", "authority_record_refs"
    ]:
        fail("entry.cas_keys", repr(contract["cas_keys"]))
    if contract["failure_state"] != "NOT_AUTHORIZED":
        fail("entry.failure_state", str(contract["failure_state"]))


def verify_prose(root: Path) -> str:
    main = (root / MAIN_REL).read_text(encoding="utf-8")
    names = (
        "authority-and-readiness.md",
        "evidence-and-determinism.md",
        "runtime-contract-and-security.md",
        "skill-context-and-evaluation.md",
        "operations-and-execution.md",
    )
    joined = "\n".join(
        [main] + [(root / SPEC_DIR / name).read_text(encoding="utf-8") for name in names]
    )
    if SPEC_ID not in main or SPEC_VERSION not in main or "CANDIDATE_NOT_ACTIVE" not in main:
        fail("prose.identity", "main metadata missing")
    if re.search(r"numeric confidence\s*`?\[0,1\]`?", joined, re.IGNORECASE):
        fail("prose.float_confidence", "legacy confidence contract remains")
    if "pullwise-server/contracts/reviewer-worker/v2/" in joined:
        fail("prose.split_contract_root", "legacy split canonical root remains")
    if "generation 1 的 18 张" in joined:
        fail("prose.stale_card_count", "18")
    required = (
        "agent-entry.json", "generation 1 的 24 张", "COL-0D", "COL-0F",
        "REL-1", "CAN-5", "CAN-25", "PROM-1", "stable-projection/v1",
        "CONTEXT_BUDGET_EXCEEDED", "instruction_effect_denied",
        "K = (C * p + 99) // 100",
    )
    for token in required:
        if token not in joined:
            fail("prose.required_rule_missing", token)
    return main


def verify_fixtures(root: Path) -> None:
    base = root / SPEC_DIR / "fixtures/spec-verifier"
    validate_profile(load_json(base / "manifest.json"))
    valid = load_json(base / "valid/scalar-profile.json")
    validate_profile(valid)
    if valid.get("confidence_bps") != 7000:
        fail("fixture.valid_scalar", "confidence_bps")
    try:
        load_json(base / "invalid/float-confidence.json")
    except SpecError as exc:
        if exc.code != "json.float_forbidden":
            raise
    else:
        fail("fixture.float_not_rejected", "invalid float fixture")
    cycle = load_json(base / "invalid/dependency-cycle.json")
    graph = {card["id"]: card["dependencies"] for card in cycle["cards"]}
    if not graph_has_cycle(graph):
        fail("fixture.cycle_not_detected", "invalid dependency fixture")
    escaped = load_json(base / "invalid/path-escape.json")
    try:
        canonical_rel(escaped["path"])
    except SpecError as exc:
        if exc.code != "path.noncanonical":
            raise
    else:
        fail("fixture.path_not_rejected", "invalid path fixture")


def verify_source_limits(root: Path) -> None:
    for relative in REQUIRED_FILES:
        if not relative.endswith(".py"):
            continue
        count = len((root / relative).read_text(encoding="utf-8").splitlines())
        if count > 400:
            fail("source.line_limit", f"{relative}:{count}")


def verify(root: Path) -> dict[str, Any]:
    root = root.resolve()
    manifest = verify_manifest(root)
    main = verify_prose(root)
    readiness = verify_readiness(root)
    cards = verify_cards(root, main)
    verify_bootstrap(root, cards)
    verify_entry(root, cards)
    verify_fixtures(root)
    verify_source_limits(root)
    return {
        "schema_id": "reviewer-refactor-spec-verification/v1",
        "status": "PASS",
        "spec_id": manifest["spec_id"],
        "spec_version": manifest["spec_version"],
        "activation_state": readiness["activation_state"],
        "file_count": len(manifest["files"]),
        "card_count": len(CARD_IDS),
        "gate_count": len(GATE_IDS),
    }
