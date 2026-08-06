from __future__ import annotations

import re
from pathlib import Path, PurePosixPath
from typing import Any

from reviewer_spec_json import (
    SpecError,
    canonical_rel,
    exact_keys,
    fail,
    load_json,
    sha256,
    validate_profile,
    validate_schema,
)
from reviewer_spec_model import (
    CARD_IDS,
    COMMAND_FIELDS,
    GATE_IDS,
    LINE_POLICY,
    MAIN_REL,
    MANIFEST_REL,
    REQUIRED_CARD_PATHS,
    REQUIRED_FILES,
    SPEC_DIR,
    SPEC_ID,
    SPEC_VERSION,
    TRANSITION_TRANSACTION,
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
            "schema_id",
            "spec_id",
            "spec_version",
            "activation_state",
            "overall_status",
            "reason_codes",
            "gates",
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


def _graph_has_cycle(graph: dict[str, list[str]]) -> bool:
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str) -> bool:
        if node in visiting:
            return True
        if node in visited:
            return False
        visiting.add(node)
        if any(visit(item) for item in graph.get(node, [])):
            return True
        visiting.remove(node)
        visited.add(node)
        return False

    return any(visit(node) for node in graph)


def _ancestors(graph: dict[str, list[str]], node: str) -> set[str]:
    result: set[str] = set()
    stack = list(graph[node])
    while stack:
        current = stack.pop()
        if current not in result:
            result.add(current)
            stack.extend(graph[current])
    return result


def _path_overlap(left: dict[str, str], right: dict[str, str]) -> bool:
    if left["repo_id"] != right["repo_id"]:
        return False
    a = PurePosixPath(left["path"]).parts
    b = PurePosixPath(right["path"]).parts
    common = min(len(a), len(b))
    if a[:common] != b[:common]:
        return False
    if len(a) == len(b):
        return True
    shorter = left if len(a) < len(b) else right
    return shorter["path_kind"] == "tree"


def _safe_argv(argv: list[str], location: str) -> None:
    forbidden_tokens = {"&&", "||", "|", ";", "latest"}
    for argument in argv:
        lowered = argument.casefold()
        if (
            argument in forbidden_tokens
            or lowered == "latest"
            or any(char in argument for char in "<>{}")
        ):
            fail("cards.command_placeholder", location)


def _verify_transition(value: dict[str, Any], bound_count: int) -> None:
    generation = value["generation"]
    profile = value["profile"]
    transition = value["transition"]
    if tuple(transition["required_transaction"]) != TRANSITION_TRANSACTION:
        fail("cards.transition_transaction", repr(transition["required_transaction"]))
    if generation == 1:
        expected = (None, None, [], "absent", "inert_catalog", 0)
        actual = (
            transition["from_generation"],
            transition["from_manifest_sha256"],
            transition["authority_record_refs"],
            transition["command_binding_state"],
            profile,
            bound_count,
        )
        if actual != expected:
            fail("cards.generation_one", repr(actual))
        return
    if profile != "stage_bound":
        fail("cards.successor_profile", profile)
    if transition["from_generation"] != generation - 1:
        fail("cards.transition_generation", repr(transition["from_generation"]))
    if not re.fullmatch(r"[0-9a-f]{64}", transition["from_manifest_sha256"] or ""):
        fail("cards.transition_manifest", repr(transition["from_manifest_sha256"]))
    if not transition["authority_record_refs"]:
        fail("cards.transition_authority", "empty")
    binding = transition["command_binding_state"]
    if binding not in ("partial", "complete") or bound_count == 0:
        fail("cards.transition_binding", f"{binding}:{bound_count}")
    if binding == "complete" and bound_count != len(CARD_IDS):
        fail("cards.transition_binding", f"complete:{bound_count}")


def verify_cards(root: Path, main_text: str) -> dict[str, Any]:
    schema = load_json(root / SPEC_DIR / "execution-card.schema.json")
    value = load_json(root / SPEC_DIR / "execution-cards.json")
    validate_profile(schema)
    validate_profile(value)
    validate_schema(value, schema)
    if value["schema_id"] != "reviewer-refactor-execution-cards/v2":
        fail("cards.schema_id", str(value["schema_id"]))
    if value["spec_id"] != SPEC_ID:
        fail("cards.spec_id", str(value["spec_id"]))
    cards = value["cards"]
    ids = tuple(card["id"] for card in cards)
    if ids != CARD_IDS:
        fail("cards.id_order", repr(ids))
    graph: dict[str, list[str]] = {}
    by_id: dict[str, dict[str, Any]] = {}
    artifact_owner: dict[str, str] = {}
    bound_count = 0
    for card in cards:
        card_id = card["id"]
        graph[card_id] = card["dependencies"]
        by_id[card_id] = card
        if any(dependency not in by_id for dependency in card["dependencies"]):
            fail("cards.dependency_order_or_unknown", card_id)
        if f"| {card_id} |" not in main_text:
            fail("cards.main_ledger_missing", card_id)
        if card["line_policy"] != LINE_POLICY:
            fail("cards.line_policy", card_id)
        repositories = set(card["repositories"])
        path_markers: set[tuple[str, str]] = set()
        for item in card["write_set"]:
            path = canonical_rel(item["path"])
            marker = (item["repo_id"], path.casefold())
            if marker in path_markers:
                fail("cards.duplicate_write", f"{card_id}:{path}")
            path_markers.add(marker)
            if item["repo_id"] not in repositories:
                fail("cards.write_repo", f"{card_id}:{item['repo_id']}")
            if "release-change-set" in path or any(char in path for char in "<>{}"):
                fail("cards.synthetic_path", f"{card_id}:{path}")
        state = card["execution_state"]
        if state == "blocked":
            if card["authority_state"] != "NOT_AUTHORIZED":
                fail("cards.blocked_authority", card_id)
            if any(card[field] for field in COMMAND_FIELDS):
                fail("cards.blocked_command", card_id)
        else:
            bound_count += 1
            if card["authority_state"] == "NOT_AUTHORIZED":
                fail("cards.bound_authority", card_id)
            command_ids: set[str] = set()
            for field in COMMAND_FIELDS:
                if not card[field]:
                    fail("cards.bound_commands", f"{card_id}:{field}")
                for command in card[field]:
                    if command["command_id"] in command_ids:
                        fail("cards.command_id_duplicate", f"{card_id}:{command['command_id']}")
                    command_ids.add(command["command_id"])
                    if command["cwd_repo"] not in repositories:
                        fail("cards.command_repo", f"{card_id}:{command['cwd_repo']}")
                    _safe_argv(command["argv"], f"{card_id}:{command['command_id']}")
        for output in card["outputs"]:
            artifact_id = output["artifact_id"]
            if artifact_id in artifact_owner:
                fail("cards.artifact_duplicate", artifact_id)
            artifact_owner[artifact_id] = card_id
            has_path = output["path"] is not None
            has_binding = output["path_binding_artifact"] is not None
            if has_path == has_binding:
                fail("cards.output_binding", f"{card_id}:{artifact_id}")
            if output["repo_id"] not in repositories:
                fail("cards.output_repo", f"{card_id}:{output['repo_id']}")
            if has_path:
                path = canonical_rel(output["path"])
                if "release/" in path or any(char in path for char in "<>{}"):
                    fail("cards.synthetic_output", f"{card_id}:{path}")
            else:
                expected = (
                    f"generation-path-bindings.json#/cards/{card_id}/outputs/{artifact_id}"
                )
                if output["path_binding_artifact"] != expected:
                    fail("cards.output_binding_ref", f"{card_id}:{artifact_id}")
    if _graph_has_cycle(graph):
        fail("cards.dependency_cycle", "execution cards")
    reach = {card_id: _ancestors(graph, card_id) for card_id in CARD_IDS}
    for card in cards:
        card_id = card["id"]
        for artifact in card["input_artifacts"]:
            producer = artifact_owner.get(artifact)
            if producer is not None and producer not in reach[card_id]:
                fail("cards.artifact_dependency", f"{card_id}<-{producer}:{artifact}")
    for card_id, required in REQUIRED_CARD_PATHS.items():
        actual = {item["path"] for item in by_id[card_id]["write_set"]}
        if not required <= actual:
            fail("cards.required_surface", f"{card_id}:{sorted(required-actual)}")
    for index, left_id in enumerate(CARD_IDS):
        for right_id in CARD_IDS[index + 1 :]:
            if left_id in reach[right_id] or right_id in reach[left_id]:
                continue
            for left in by_id[left_id]["write_set"]:
                for right in by_id[right_id]["write_set"]:
                    if _path_overlap(left, right):
                        fail("cards.parallel_write_overlap", f"{left_id}<->{right_id}")
    _verify_transition(value, bound_count)
    return value


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
    for action in entry["actions"]:
        exact_keys(action, {"action_id", "mutates_worktree", "commands"}, "entry.action")
        action_ids.append(action["action_id"])
        if action["mutates_worktree"] or not action["commands"]:
            fail("entry.action_mutation", action["action_id"])
        for command in action["commands"]:
            exact_keys(command, {"cwd_repo", "argv", "expected_exit"}, "entry.command")
            _safe_argv(command["argv"], action["action_id"])
    if action_ids != entry["allowed_action_ids"]:
        fail("entry.action_order", repr(action_ids))
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
        "agent-entry.json",
        "generation 1 的 24 张",
        "COL-0D",
        "COL-0F",
        "REL-1",
        "CAN-5",
        "CAN-25",
        "PROM-1",
        "stable-projection/v1",
        "CONTEXT_BUDGET_EXCEEDED",
        "instruction_effect_denied",
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
    if not _graph_has_cycle(graph):
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
