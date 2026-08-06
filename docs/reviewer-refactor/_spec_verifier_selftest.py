from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path
from typing import Any, Callable

from reviewer_spec_json import SpecError, fail, load_json, sha256
from reviewer_spec_model import COMMAND_FIELDS, MANIFEST_REL


Verify = Callable[[Path], dict[str, Any]]


def _copy_unit(root: Path, target: Path) -> dict[str, Any]:
    manifest = load_json(root / MANIFEST_REL)
    for entry in manifest["files"]:
        source = root / entry["path"]
        destination = target / entry["path"]
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
    destination = target / MANIFEST_REL
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(root / MANIFEST_REL, destination)
    return manifest


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _refresh_manifest(root: Path, relative: str) -> None:
    manifest_path = root / MANIFEST_REL
    manifest = load_json(manifest_path)
    data = (root / relative).read_bytes()
    entry = next(item for item in manifest["files"] if item["path"] == relative)
    entry["size_bytes"] = len(data)
    entry["sha256"] = sha256(data)
    _write_json(manifest_path, manifest)


def _expect_failure(
    root: Path,
    verify: Verify,
    mutation: Callable[[Path], None],
    expected_code: str,
) -> None:
    with tempfile.TemporaryDirectory(prefix="reviewer-refactor-spec-") as directory:
        target = Path(directory)
        _copy_unit(root, target)
        mutation(target)
        try:
            verify(target)
        except SpecError as exc:
            if exc.code != expected_code:
                raise
        else:
            fail("self_test.failure_not_detected", expected_code)


def _expect_success(
    root: Path,
    verify: Verify,
    mutation: Callable[[Path], None],
) -> None:
    with tempfile.TemporaryDirectory(prefix="reviewer-refactor-spec-") as directory:
        target = Path(directory)
        _copy_unit(root, target)
        mutation(target)
        verify(target)


def _bind_first_card(target: Path, manifest_digest: str | None) -> None:
    relative = "docs/reviewer-refactor/execution-cards.json"
    path = target / relative
    value = load_json(path)
    value["generation"] = 2
    value["profile"] = "stage_bound"
    value["transition"].update(
        {
            "from_generation": 1,
            "from_manifest_sha256": manifest_digest,
            "authority_record_refs": ["RR-GOV-COLLECTOR-DRAFT-A"],
            "command_binding_state": "partial",
        }
    )
    card = value["cards"][0]
    card["execution_state"] = "bound"
    card["authority_state"] = "READY"
    for index, field in enumerate(COMMAND_FIELDS, start=1):
        card[field] = [
            {
                "command_id": f"COL-0D.TEST-{index}",
                "cwd_repo": "worker",
                "argv": ["python", "-c", "pass"],
                "timeout_seconds": 30,
                "expected_exit": [0],
                "evidence_outputs": [f"command-{index}.json"],
            }
        ]
    _write_json(path, value)
    _refresh_manifest(target, relative)


def _accept_successor(target: Path) -> None:
    _bind_first_card(target, "a" * 64)

    relative = "docs/reviewer-refactor/agent-entry.json"
    path = target / relative
    entry = load_json(path)
    entry.update(
        {
            "activation_state": "GOVERNANCE_DRAFTED",
            "authority_state": "READY",
            "current_generation": 2,
            "execution_profile": "stage_bound",
        }
    )
    entry["allowed_action_ids"].append("execute-next-card")
    entry["actions"].append(
        {
            "action_id": "execute-next-card",
            "action_kind": "card",
            "card_id": "COL-0D",
            "mutates_state": True,
            "command_refs": [f"COL-0D:{field}" for field in COMMAND_FIELDS],
            "commands": [],
        }
    )
    _write_json(path, entry)
    _refresh_manifest(target, relative)

    relative = "docs/reviewer-refactor/readiness.json"
    path = target / relative
    readiness = load_json(path)
    readiness["activation_state"] = "GOVERNANCE_DRAFTED"
    execution_gate = next(
        gate for gate in readiness["gates"]
        if gate["gate_id"] == "SPEC-READY-12-EXECUTION"
    )
    execution_gate["status"] = "READY"
    execution_gate["reason_code"] = "execution.col_0d_bound"
    _write_json(path, readiness)
    _refresh_manifest(target, relative)

    relative = "docs/reviewer-refactor/bootstrap-command.json"
    path = target / relative
    bootstrap = load_json(path)
    bootstrap["card_generation"] = 2
    bootstrap["execution_profile"] = "stage_bound"
    _write_json(path, bootstrap)
    _refresh_manifest(target, relative)


def self_test(root: Path, verify: Verify) -> dict[str, Any]:
    root = root.resolve()
    result = verify(root)

    def tamper(target: Path) -> None:
        path = target / "docs/reviewer-refactor/authority-and-readiness.md"
        path.write_bytes(path.read_bytes() + b"\n")

    _expect_failure(root, verify, tamper, "manifest.size_mismatch")

    def violate_schema(target: Path) -> None:
        relative = "docs/reviewer-refactor/execution-cards.json"
        path = target / relative
        value = load_json(path)
        value["cards"][0]["owner_role"] = ""
        _write_json(path, value)
        _refresh_manifest(target, relative)

    _expect_failure(root, verify, violate_schema, "schema.minLength")

    def violate_lifecycle(target: Path) -> None:
        relative = "docs/reviewer-refactor/execution-cards.json"
        path = target / relative
        value = load_json(path)
        value["cards"][0]["execution_state"] = "bound"
        value["cards"][0]["authority_state"] = "READY"
        _write_json(path, value)
        _refresh_manifest(target, relative)

    _expect_failure(root, verify, violate_lifecycle, "cards.bound_commands")

    def violate_transition(target: Path) -> None:
        _bind_first_card(target, None)

    _expect_failure(root, verify, violate_transition, "cards.transition_manifest")
    _expect_success(root, verify, _accept_successor)
    result["self_test"] = "PASS"
    result["tamper_detection"] = "PASS"
    result["schema_enforcement"] = "PASS"
    result["lifecycle_enforcement"] = "PASS"
    result["transition_enforcement"] = "PASS"
    result["successor_acceptance"] = "PASS"
    return result
