from __future__ import annotations

import re
from pathlib import Path, PurePosixPath
from typing import Any

from reviewer_spec_json import canonical_rel, fail, load_json, validate_profile, validate_schema
from reviewer_spec_model import (
    CARD_IDS,
    COMMAND_FIELDS,
    LINE_POLICY,
    REQUIRED_CARD_PATHS,
    SPEC_DIR,
    SPEC_ID,
    TRANSITION_TRANSACTION,
)


def graph_has_cycle(graph: dict[str, list[str]]) -> bool:
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


def safe_argv(argv: list[str], location: str) -> None:
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
        write_repositories = {item["repo_id"] for item in card["write_set"]}
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
                    safe_argv(command["argv"], f"{card_id}:{command['command_id']}")
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
            if output["repo_id"] not in write_repositories:
                fail("cards.output_write_repo", f"{card_id}:{output['repo_id']}")
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
    if graph_has_cycle(graph):
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
