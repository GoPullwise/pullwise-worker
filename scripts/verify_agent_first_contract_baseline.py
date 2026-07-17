#!/usr/bin/env python3
"""Check the frozen Server/Web review-worker-protocol/v1 baseline."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

try:
    from scripts.agent_first_contract_candidate import (
        candidate_payload,
        create_candidate as build_candidate,
    )
    from scripts.agent_first_contract_files import (
        BaselineEnvironmentError,
        canonical_text,
        python_collection_values_from_text,
        repository_roots,
        surface_path,
        text_sha256,
    )
    from scripts.agent_first_contract_manifest import (
        ManifestError,
        RunnerCatalog,
        load_manifest,
        render_appendix,
        validate_manifest,
    )
    from scripts.agent_first_contract_probes import (
        RUNNER_CATALOG,
        build_test_argv,
        runner_catalog_sha256,
        run_probe,
    )
    from scripts.agent_first_contract_observation import (
        git_head,
        input_snapshot,
        input_snapshot_sha256,
    )
except ModuleNotFoundError:
    from agent_first_contract_candidate import (  # type: ignore[no-redef]
        candidate_payload,
        create_candidate as build_candidate,
    )
    from agent_first_contract_files import (  # type: ignore[no-redef]
        BaselineEnvironmentError,
        canonical_text,
        python_collection_values_from_text,
        repository_roots,
        surface_path,
        text_sha256,
    )
    from agent_first_contract_manifest import (  # type: ignore[no-redef]
        ManifestError,
        RunnerCatalog,
        load_manifest,
        render_appendix,
        validate_manifest,
    )
    from agent_first_contract_probes import (  # type: ignore[no-redef]
        RUNNER_CATALOG,
        build_test_argv,
        runner_catalog_sha256,
        run_probe,
    )
    from agent_first_contract_observation import (  # type: ignore[no-redef]
        git_head,
        input_snapshot,
        input_snapshot_sha256,
    )


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "contracts" / "agent-first" / "legacy-v1-contract-baseline.json"
REPORT_SCHEMA_ID = "pullwise-contract-baseline-report/v1"


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ManifestError("invalid_cli_arguments")


def _appendix_matches(
    manifest: dict[str, Any], roots: dict[str, Path], runner_catalog: RunnerCatalog
) -> bool:
    appendix = manifest["appendix"]
    path = surface_path(roots[appendix["repo"]], appendix["path"])
    if path is None:
        return False
    text = canonical_text(path)
    start = appendix["start_marker"]
    end = appendix["end_marker"]
    if text.count(start) != 1 or text.count(end) != 1:
        return False
    start_index = text.index(start) + len(start)
    end_index = text.index(end)
    if start_index >= end_index:
        return False
    actual = text[start_index:end_index].strip("\n")
    return actual == render_appendix(manifest, runner_catalog=runner_catalog)


def _issue(code: str, **identifiers: Any) -> dict[str, Any]:
    return {"code": code, **identifiers}


def verify_baseline(
    manifest: dict[str, Any],
    workspace_root: Path,
    *,
    run_tests: bool,
    runner_catalog: RunnerCatalog = RUNNER_CATALOG,
) -> dict[str, Any]:
    validate_manifest(manifest, runner_catalog=runner_catalog)
    roots = repository_roots(workspace_root)
    initial_inputs = input_snapshot(manifest, roots)
    failures: list[dict[str, Any]] = []
    indeterminate: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    for repo in manifest["repositories"]:
        repo_id = repo["id"]
        if (
            initial_inputs[f"head:{repo_id}"] is None
            or initial_inputs[f"worktree:{repo_id}"] is None
        ):
            indeterminate.append(
                _issue("repository_observation_unavailable", repo=repo_id)
            )

    repositories = []
    for repo in manifest["repositories"]:
        current = git_head(roots[repo["id"]])
        repositories.append(
            {
                "id": repo["id"],
                "frozen_head": repo["frozen_head"],
                "current_head": current,
                "head_matches": current == repo["frozen_head"] if current else None,
                "head_status": "informational",
            }
        )

    drifted_watched: list[dict[str, Any]] = []
    surface_reports: list[dict[str, Any]] = []
    surface_texts: dict[tuple[str, str], str] = {}
    hashes_match = True
    for surface in manifest["surfaces"]:
        path = surface_path(roots[surface["repo"]], surface["path"])
        actual_sha: str | None = None
        missing_anchors: list[str] = []
        if path is not None:
            text = canonical_text(path)
            surface_texts[(surface["repo"], surface["path"])] = text
            actual_sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
            missing_anchors = [
                anchor for anchor in surface["anchors"] if anchor not in text
            ]
        drifted = path is None or actual_sha != surface["sha256"] or bool(missing_anchors)
        if drifted:
            hashes_match = False
            if surface["enforcement"] == "blocking":
                failures.append(_issue("blocking_surface_drift", surface_id=surface["id"]))
            else:
                drifted_watched.append(surface)
        surface_reports.append(
            {
                "id": surface["id"],
                "enforcement": surface["enforcement"],
                "status": "drift" if drifted else "match",
                "actual_sha256": actual_sha,
                "missing_anchors": missing_anchors,
            }
        )

    registry_reports: list[dict[str, Any]] = []
    for registry in manifest["registries"]:
        text = surface_texts.get((registry["repo"], registry["path"]))
        actual_values = (
            python_collection_values_from_text(
                text, registry["symbol"], ordered=registry["ordered"]
            )
            if text is not None
            else None
        )
        matches = actual_values == registry["values"]
        if not matches:
            failures.append(_issue("registry_mismatch", registry_id=registry["id"]))
        registry_reports.append(
            {
                "id": registry["id"],
                "status": "match" if matches else "drift",
                "actual_values": actual_values,
            }
        )

    appendix_matches = _appendix_matches(manifest, roots, runner_catalog)
    if not appendix_matches:
        failures.append(_issue("appendix_drift", repo="worker"))

    test_reports: list[dict[str, Any]] = []
    before_inputs = input_snapshot(manifest, roots)
    if initial_inputs != before_inputs:
        indeterminate.append(_issue("inputs_changed_during_inspection"))
    test_statuses: dict[str, str] = {}
    for test in manifest["tests"]:
        if not run_tests:
            result = {
                "id": test["id"],
                "runner_id": test["runner_id"],
                "status": "not_run",
                "returncode": None,
                "output_sha256": None,
                "observed_tests": None,
                "observed_skips": None,
            }
            indeterminate.append(_issue("probe_not_run", test_id=test["id"]))
        else:
            spec = runner_catalog[test["runner_id"]]
            result = run_probe(
                test["runner_id"],
                roots[str(spec["repo"])],
                runner_catalog=runner_catalog,
            )
            result["id"] = test["id"]
            if result["status"] == "failed":
                failures.append(_issue("probe_failed", test_id=test["id"]))
            elif result["status"] == "indeterminate":
                indeterminate.append(
                    _issue(
                        "probe_indeterminate",
                        test_id=test["id"],
                        reason=result["reason"],
                    )
                )
        test_reports.append(result)
        test_statuses[test["id"]] = result["status"]

    after_inputs = input_snapshot(manifest, roots)
    if run_tests and before_inputs != after_inputs:
        indeterminate.append(_issue("inputs_changed_during_probe"))

    for surface in drifted_watched:
        probe_statuses = [test_statuses[probe_id] for probe_id in surface["probe_ids"]]
        if probe_statuses and all(status == "passed" for status in probe_statuses):
            warnings.append(_issue("watched_surface_drift", surface_id=surface["id"]))
        elif not any(status == "failed" for status in probe_statuses):
            code = "probe_not_run" if not run_tests else "watched_surface_unverified"
            item = _issue(code, surface_id=surface["id"])
            if item not in indeterminate:
                indeterminate.append(item)

    failures.sort(key=lambda item: json.dumps(item, sort_keys=True))
    indeterminate.sort(key=lambda item: json.dumps(item, sort_keys=True))
    warnings.sort(key=lambda item: json.dumps(item, sort_keys=True))
    if failures:
        status = "incompatible"
    elif indeterminate:
        status = "indeterminate"
    else:
        status = "compatible"
    return {
        "schema_id": REPORT_SCHEMA_ID,
        "baseline_id": manifest["baseline_id"],
        "protocol_version": manifest["protocol_version"],
        "input_snapshot_sha256": input_snapshot_sha256(initial_inputs),
        "runner_catalog_sha256": runner_catalog_sha256(runner_catalog),
        "status": status,
        "compatible": status == "compatible",
        "hashes_match": hashes_match,
        "appendix_matches": appendix_matches,
        "tests_run": run_tests,
        "repositories": repositories,
        "surfaces": surface_reports,
        "registries": registry_reports,
        "tests": test_reports,
        "warnings": warnings,
        "failures": failures,
        "indeterminate_reasons": indeterminate,
    }


def create_candidate(
    manifest: dict[str, Any],
    workspace_root: Path,
    *,
    runner_catalog: RunnerCatalog = RUNNER_CATALOG,
    evidence_report: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return build_candidate(
        manifest,
        workspace_root,
        verifier=verify_baseline,
        runner_catalog=runner_catalog,
        snapshotter=input_snapshot,
        evidence_report=evidence_report,
    )


def _candidate_payload(
    candidate: dict[str, Any], evidence: dict[str, Any]
) -> dict[str, Any]:
    return candidate_payload(candidate, evidence)


def _error_report(kind: str) -> dict[str, Any]:
    return {
        "schema_id": REPORT_SCHEMA_ID,
        "status": "indeterminate",
        "compatible": False,
        "error_kind": kind,
    }


def main(
    argv: list[str] | None = None,
    *,
    runner_catalog: RunnerCatalog = RUNNER_CATALOG,
) -> int:
    parser = JsonArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("check", "candidate"):
        sub = subparsers.add_parser(command)
        sub.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
        sub.add_argument("--workspace-root", type=Path, required=True)
    render = subparsers.add_parser("render-appendix")
    render.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    try:
        args = parser.parse_args(argv)
        manifest = load_manifest(args.manifest, runner_catalog=runner_catalog)
        if args.command == "check":
            report = verify_baseline(
                manifest,
                args.workspace_root,
                run_tests=True,
                runner_catalog=runner_catalog,
            )
            print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
            return {"compatible": 0, "incompatible": 1, "indeterminate": 2}[
                report["status"]
            ]
        if args.command == "candidate":
            evidence = verify_baseline(
                manifest,
                args.workspace_root,
                run_tests=True,
                runner_catalog=runner_catalog,
            )
            candidate = create_candidate(
                manifest,
                args.workspace_root,
                runner_catalog=runner_catalog,
                evidence_report=evidence,
            )
            print(
                json.dumps(
                    _candidate_payload(candidate, evidence),
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
        print(render_appendix(manifest, runner_catalog=runner_catalog))
        return 0
    except ManifestError:
        print(json.dumps(_error_report("manifest_invalid"), indent=2, sort_keys=True))
        return 2
    except (BaselineEnvironmentError, OSError, UnicodeError):
        print(json.dumps(_error_report("environment_invalid"), indent=2, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
