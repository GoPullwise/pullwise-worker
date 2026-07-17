#!/usr/bin/env python3
"""Check the frozen Server/Web review-worker-protocol/v1 baseline."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path
import subprocess
from typing import Any

try:
    from scripts.agent_first_contract_files import (
        BaselineEnvironmentError,
        canonical_text,
        repository_roots,
        surface_path,
        text_sha256,
    )
    from scripts.agent_first_contract_manifest import (
        GIT_SHA_PATTERN,
        ManifestError,
        RunnerCatalog,
        load_manifest,
        render_appendix,
        validate_manifest,
    )
    from scripts.agent_first_contract_probes import (
        RUNNER_CATALOG,
        build_test_argv,
        run_probe,
    )
except ModuleNotFoundError:
    from agent_first_contract_files import (  # type: ignore[no-redef]
        BaselineEnvironmentError,
        canonical_text,
        repository_roots,
        surface_path,
        text_sha256,
    )
    from agent_first_contract_manifest import (  # type: ignore[no-redef]
        GIT_SHA_PATTERN,
        ManifestError,
        RunnerCatalog,
        load_manifest,
        render_appendix,
        validate_manifest,
    )
    from agent_first_contract_probes import (  # type: ignore[no-redef]
        RUNNER_CATALOG,
        build_test_argv,
        run_probe,
    )


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "contracts" / "agent-first" / "legacy-v1-contract-baseline.json"
REPORT_SCHEMA_ID = "pullwise-contract-baseline-report/v1"


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ManifestError("invalid_cli_arguments")


def _git_output(repo_root: Path, arguments: list[str], timeout: int = 15) -> bytes | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), *arguments],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout if result.returncode == 0 else None


def _git_head(repo_root: Path) -> str | None:
    output = _git_output(repo_root, ["rev-parse", "HEAD"], timeout=5)
    if output is None:
        return None
    head = output.decode("ascii", errors="ignore").strip().lower()
    return head if GIT_SHA_PATTERN.fullmatch(head) else None


def _git_worktree_digest(repo_root: Path) -> str | None:
    status = _git_output(
        repo_root,
        ["status", "--porcelain=v1", "-z", "--untracked-files=all"],
    )
    diff = _git_output(
        repo_root,
        ["diff", "--binary", "--no-ext-diff", "--no-textconv", "HEAD", "--"],
    )
    if status is None or diff is None:
        return None
    return hashlib.sha256(status + b"\0" + diff).hexdigest()


def _input_snapshot(
    manifest: dict[str, Any], roots: dict[str, Path]
) -> dict[str, str | None]:
    snapshot: dict[str, str | None] = {}
    for surface in manifest["surfaces"]:
        path = surface_path(roots[surface["repo"]], surface["path"])
        snapshot[f"surface:{surface['id']}"] = text_sha256(path) if path else None
    appendix = manifest["appendix"]
    appendix_path = surface_path(roots[appendix["repo"]], appendix["path"])
    snapshot["appendix"] = text_sha256(appendix_path) if appendix_path else None
    for repo_id in sorted(roots):
        snapshot[f"head:{repo_id}"] = _git_head(roots[repo_id])
        snapshot[f"worktree:{repo_id}"] = _git_worktree_digest(roots[repo_id])
    return snapshot


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
    failures: list[dict[str, Any]] = []
    indeterminate: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []

    repositories = []
    for repo in manifest["repositories"]:
        current = _git_head(roots[repo["id"]])
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
    hashes_match = True
    for surface in manifest["surfaces"]:
        path = surface_path(roots[surface["repo"]], surface["path"])
        actual_sha: str | None = None
        missing_anchors: list[str] = []
        if path is not None:
            text = canonical_text(path)
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

    appendix_matches = _appendix_matches(manifest, roots, runner_catalog)
    if not appendix_matches:
        failures.append(_issue("appendix_drift", repo="worker"))

    test_reports: list[dict[str, Any]] = []
    before_inputs = _input_snapshot(manifest, roots)
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

    after_inputs = _input_snapshot(manifest, roots)
    if run_tests and before_inputs != after_inputs:
        indeterminate.append(_issue("inputs_changed_during_probe"))

    for surface in drifted_watched:
        probe_statuses = [test_statuses[probe_id] for probe_id in surface["probe_ids"]]
        if probe_statuses and all(status == "passed" for status in probe_statuses):
            warnings.append(_issue("watched_surface_drift", surface_id=surface["id"]))
            indeterminate.append(
                _issue("watched_surface_drift", surface_id=surface["id"])
            )
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
        "status": status,
        "compatible": status == "compatible",
        "hashes_match": hashes_match,
        "appendix_matches": appendix_matches,
        "tests_run": run_tests,
        "repositories": repositories,
        "surfaces": surface_reports,
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
) -> dict[str, Any]:
    validate_manifest(manifest, runner_catalog=runner_catalog)
    candidate = copy.deepcopy(manifest)
    roots = repository_roots(workspace_root)
    for repo in candidate["repositories"]:
        current = _git_head(roots[repo["id"]])
        if current:
            repo["frozen_head"] = current
    for surface in candidate["surfaces"]:
        path = surface_path(roots[surface["repo"]], surface["path"])
        if path is None:
            raise BaselineEnvironmentError(f"candidate_surface_missing:{surface['id']}")
        text = canonical_text(path)
        if any(anchor not in text for anchor in surface["anchors"]):
            raise BaselineEnvironmentError(f"candidate_anchor_missing:{surface['id']}")
        surface["sha256"] = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return candidate


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
            candidate = create_candidate(
                manifest, args.workspace_root, runner_catalog=runner_catalog
            )
            print(json.dumps(candidate, ensure_ascii=False, indent=2))
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
