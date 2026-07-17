"""Read-only strict-v1 baseline candidate construction and evidence binding."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any, Callable

try:
    from scripts.agent_first_contract_files import (
        BaselineEnvironmentError,
        canonical_text,
        python_collection_values_from_text,
        repository_roots,
        surface_path,
    )
    from scripts.agent_first_contract_manifest import (
        RunnerCatalog,
        SHA256_PATTERN,
        validate_manifest,
    )
    from scripts.agent_first_contract_observation import (
        input_snapshot,
        input_snapshot_sha256,
    )
    from scripts.agent_first_contract_probes import runner_catalog_sha256
except ModuleNotFoundError:
    from agent_first_contract_files import (  # type: ignore[no-redef]
        BaselineEnvironmentError,
        canonical_text,
        python_collection_values_from_text,
        repository_roots,
        surface_path,
    )
    from agent_first_contract_manifest import (  # type: ignore[no-redef]
        RunnerCatalog,
        SHA256_PATTERN,
        validate_manifest,
    )
    from agent_first_contract_observation import (  # type: ignore[no-redef]
        input_snapshot,
        input_snapshot_sha256,
    )
    from agent_first_contract_probes import runner_catalog_sha256  # type: ignore[no-redef]


CANDIDATE_SCHEMA_ID = "pullwise-contract-baseline-candidate/v1"
Verifier = Callable[..., dict[str, Any]]
Snapshotter = Callable[[dict[str, Any], dict[str, Path]], dict[str, str | None]]


def _json_sha256(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _validate_evidence(
    manifest: dict[str, Any],
    evidence: dict[str, Any],
    runner_catalog: RunnerCatalog,
) -> None:
    if (
        not isinstance(evidence, dict)
        or evidence.get("schema_id") != "pullwise-contract-baseline-report/v1"
        or evidence.get("baseline_id") != manifest["baseline_id"]
        or evidence.get("protocol_version") != manifest["protocol_version"]
        or evidence.get("tests_run") is not True
    ):
        raise BaselineEnvironmentError("candidate_evidence_invalid")
    digest = evidence.get("input_snapshot_sha256")
    if not isinstance(digest, str) or not SHA256_PATTERN.fullmatch(digest):
        raise BaselineEnvironmentError("candidate_evidence_invalid")
    if evidence.get("runner_catalog_sha256") != runner_catalog_sha256(runner_catalog):
        raise BaselineEnvironmentError("candidate_runner_catalog_mismatch")
    tests = evidence.get("tests")
    expected = [
        (test["id"], test["runner_id"], "passed")
        for test in manifest["tests"]
    ]
    if not isinstance(tests, list) or not all(isinstance(test, dict) for test in tests):
        raise BaselineEnvironmentError("candidate_evidence_invalid")
    actual = [
        (test.get("id"), test.get("runner_id"), test.get("status"))
        for test in tests
    ]
    if actual != expected:
        raise BaselineEnvironmentError("candidate_probe_evidence_incomplete")
    reasons = evidence.get("indeterminate_reasons")
    if not isinstance(reasons, list) or not all(
        isinstance(reason, dict) and isinstance(reason.get("code"), str)
        for reason in reasons
    ):
        raise BaselineEnvironmentError("candidate_evidence_invalid")
    unsafe_codes = {
        "inputs_changed_during_inspection",
        "inputs_changed_during_probe",
        "repository_observation_unavailable",
    }
    if unsafe_codes & {reason["code"] for reason in reasons}:
        raise BaselineEnvironmentError("candidate_inputs_unstable")


def create_candidate(
    manifest: dict[str, Any],
    workspace_root: Path,
    *,
    verifier: Verifier,
    runner_catalog: RunnerCatalog,
    snapshotter: Snapshotter = input_snapshot,
    evidence_report: dict[str, Any] | None = None,
) -> dict[str, Any]:
    validate_manifest(manifest, runner_catalog=runner_catalog)
    evidence = evidence_report or verifier(
        manifest,
        workspace_root,
        run_tests=True,
        runner_catalog=runner_catalog,
    )
    _validate_evidence(manifest, evidence, runner_catalog)
    roots = repository_roots(workspace_root)
    before_inputs = snapshotter(manifest, roots)
    for repo in manifest["repositories"]:
        repo_id = repo["id"]
        if (
            before_inputs[f"head:{repo_id}"] is None
            or before_inputs[f"worktree:{repo_id}"] is None
        ):
            raise BaselineEnvironmentError("candidate_repository_observation_unavailable")
    if input_snapshot_sha256(before_inputs) != evidence["input_snapshot_sha256"]:
        raise BaselineEnvironmentError("candidate_evidence_snapshot_mismatch")

    candidate = copy.deepcopy(manifest)
    for repo in candidate["repositories"]:
        repo["frozen_head"] = before_inputs[f"head:{repo['id']}"]

    captured: dict[tuple[str, str], str] = {}

    def captured_text(repo_id: str, relative_path: str, input_id: str) -> str:
        key = (repo_id, relative_path)
        if key not in captured:
            path = surface_path(roots[repo_id], relative_path)
            if path is None:
                raise BaselineEnvironmentError(f"candidate_input_missing:{input_id}")
            captured[key] = canonical_text(path)
        return captured[key]

    for surface in candidate["surfaces"]:
        text = captured_text(surface["repo"], surface["path"], surface["id"])
        if any(anchor not in text for anchor in surface["anchors"]):
            raise BaselineEnvironmentError(f"candidate_anchor_missing:{surface['id']}")
        surface["sha256"] = hashlib.sha256(text.encode("utf-8")).hexdigest()
    for registry in candidate["registries"]:
        text = captured_text(registry["repo"], registry["path"], registry["id"])
        registry["values"] = python_collection_values_from_text(
            text,
            registry["symbol"],
            ordered=registry["ordered"],
        )
    if before_inputs != snapshotter(manifest, roots):
        raise BaselineEnvironmentError("candidate_inputs_changed")
    return candidate


def candidate_payload(
    candidate: dict[str, Any], evidence: dict[str, Any]
) -> dict[str, Any]:
    return {
        "schema_id": CANDIDATE_SCHEMA_ID,
        "candidate_manifest": candidate,
        "candidate_manifest_sha256": _json_sha256(candidate),
        "probe_evidence": {
            "source_status": evidence["status"],
            "input_snapshot_sha256": evidence["input_snapshot_sha256"],
            "runner_catalog_sha256": evidence["runner_catalog_sha256"],
            "report_sha256": _json_sha256(evidence),
            "tests": evidence["tests"],
        },
    }
