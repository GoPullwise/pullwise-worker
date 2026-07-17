#!/usr/bin/env python3
"""Verify the frozen cross-repository review-worker-protocol/v1 baseline."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "contracts" / "agent-first" / "legacy-v1-contract-baseline.json"
REPORT_SCHEMA_ID = "pullwise-contract-baseline-report/v1"
MAX_SURFACE_BYTES = 8 * 1024 * 1024
MAX_OUTPUT_TAIL = 16 * 1024
ID_PATTERN = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
GIT_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
UNITTEST_NODE_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+$")
ALLOWED_ROLES = {
    "consumer",
    "fixture",
    "policy_source",
    "producer",
    "projection",
    "registry",
    "storage",
    "validator",
}


class ManifestError(ValueError):
    """The baseline manifest is ambiguous, unsafe, or malformed."""


class BaselineEnvironmentError(RuntimeError):
    """The requested workspace cannot be inspected safely."""


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ManifestError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def load_manifest(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ManifestError) as exc:
        raise ManifestError(f"cannot load manifest {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ManifestError("manifest root must be an object")
    validate_manifest(payload)
    return payload


def _require_exact_keys(value: object, required: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ManifestError(f"{label} must be an object")
    keys = set(value)
    if keys != required:
        missing = sorted(required - keys)
        unknown = sorted(keys - required)
        raise ManifestError(f"{label} keys mismatch; missing={missing}, unknown={unknown}")
    return value


def _require_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ManifestError(f"{label} must be a non-empty string without NUL")
    return value


def _require_id(value: object, label: str) -> str:
    text = _require_text(value, label)
    if not ID_PATTERN.fullmatch(text):
        raise ManifestError(f"{label} is not a canonical id: {text!r}")
    return text


def _require_relative_path(value: object, label: str) -> str:
    text = _require_text(value, label)
    if "\\" in text:
        raise ManifestError(f"{label} must use forward slashes")
    path = PurePosixPath(text)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ManifestError(f"{label} must be a contained relative path: {text!r}")
    return text


def _require_sorted_unique_texts(value: object, label: str) -> list[str]:
    if not isinstance(value, list) or not value:
        raise ManifestError(f"{label} must be a non-empty array")
    texts = [_require_text(item, f"{label}[]") for item in value]
    if texts != sorted(set(texts)):
        raise ManifestError(f"{label} must be sorted and unique")
    return texts


def validate_manifest(payload: object) -> None:
    root = _require_exact_keys(
        payload,
        {
            "schema_id",
            "baseline_id",
            "protocol_version",
            "hash_profile",
            "baseline_owner",
            "appendix",
            "compatibility_policy",
            "repositories",
            "surfaces",
            "tests",
        },
        "manifest",
    )
    if root["schema_id"] != "pullwise-contract-baseline/v1":
        raise ManifestError("schema_id must be pullwise-contract-baseline/v1")
    _require_id(root["baseline_id"], "baseline_id")
    if root["protocol_version"] != "review-worker-protocol/v1":
        raise ManifestError("protocol_version must be review-worker-protocol/v1")
    if root["hash_profile"] != "sha256-raw-bytes/v1":
        raise ManifestError("hash_profile must be sha256-raw-bytes/v1")
    _require_text(root["baseline_owner"], "baseline_owner")

    policy = _require_exact_keys(
        root["compatibility_policy"],
        {"head_drift", "unlisted_path_drift", "surface_hash_drift", "test_failure", "required_review"},
        "compatibility_policy",
    )
    expected_policy = {
        "head_drift": "informational",
        "unlisted_path_drift": "ignored",
        "surface_hash_drift": "incompatible_pending_review",
        "test_failure": "incompatible",
        "required_review": "baseline_owner_and_affected_repo_owner",
    }
    if policy != expected_policy:
        raise ManifestError("compatibility_policy must use the frozen v1 values")

    repositories = root["repositories"]
    if not isinstance(repositories, list) or not repositories:
        raise ManifestError("repositories must be a non-empty array")
    repo_ids: list[str] = []
    repo_paths: set[str] = set()
    for index, item in enumerate(repositories):
        repo = _require_exact_keys(item, {"id", "path", "owner", "frozen_head"}, f"repositories[{index}]")
        repo_id = _require_id(repo["id"], f"repositories[{index}].id")
        repo_path = _require_relative_path(repo["path"], f"repositories[{index}].path")
        _require_text(repo["owner"], f"repositories[{index}].owner")
        if not GIT_SHA_PATTERN.fullmatch(str(repo["frozen_head"])):
            raise ManifestError(f"repositories[{index}].frozen_head must be a lowercase Git SHA-1")
        if repo_path in repo_paths:
            raise ManifestError(f"duplicate repository path: {repo_path}")
        repo_ids.append(repo_id)
        repo_paths.add(repo_path)
    if repo_ids != sorted(set(repo_ids)):
        raise ManifestError("repositories must be sorted by unique id")
    known_repos = set(repo_ids)

    appendix = _require_exact_keys(
        root["appendix"],
        {"repo", "path", "start_marker", "end_marker"},
        "appendix",
    )
    if appendix["repo"] not in known_repos:
        raise ManifestError("appendix.repo must reference a repository")
    _require_relative_path(appendix["path"], "appendix.path")
    start_marker = _require_text(appendix["start_marker"], "appendix.start_marker")
    end_marker = _require_text(appendix["end_marker"], "appendix.end_marker")
    if start_marker == end_marker or "\n" in start_marker + end_marker:
        raise ManifestError("appendix markers must be distinct single lines")

    surfaces = root["surfaces"]
    if not isinstance(surfaces, list) or not surfaces:
        raise ManifestError("surfaces must be a non-empty array")
    surface_ids: list[str] = []
    surface_paths: set[tuple[str, str]] = set()
    for index, item in enumerate(surfaces):
        surface = _require_exact_keys(
            item,
            {"id", "repo", "path", "roles", "anchors", "sha256"},
            f"surfaces[{index}]",
        )
        surface_id = _require_id(surface["id"], f"surfaces[{index}].id")
        repo_id = _require_id(surface["repo"], f"surfaces[{index}].repo")
        if repo_id not in known_repos:
            raise ManifestError(f"surfaces[{index}].repo is unknown")
        relative_path = _require_relative_path(surface["path"], f"surfaces[{index}].path")
        roles = _require_sorted_unique_texts(surface["roles"], f"surfaces[{index}].roles")
        if not set(roles) <= ALLOWED_ROLES:
            raise ManifestError(f"surfaces[{index}].roles contains an unknown role")
        _require_sorted_unique_texts(surface["anchors"], f"surfaces[{index}].anchors")
        if not SHA256_PATTERN.fullmatch(str(surface["sha256"])):
            raise ManifestError(f"surfaces[{index}].sha256 must be lowercase SHA-256")
        key = (repo_id, relative_path)
        if key in surface_paths:
            raise ManifestError(f"duplicate surface path: {repo_id}/{relative_path}")
        surface_ids.append(surface_id)
        surface_paths.add(key)
    if surface_ids != sorted(set(surface_ids)):
        raise ManifestError("surfaces must be sorted by unique id")

    tests = root["tests"]
    if not isinstance(tests, list) or not tests:
        raise ManifestError("tests must be a non-empty array")
    test_ids: list[str] = []
    for index, item in enumerate(tests):
        test = _require_exact_keys(
            item,
            {"id", "repo", "runner", "nodes", "timeout_seconds"},
            f"tests[{index}]",
        )
        test_id = _require_id(test["id"], f"tests[{index}].id")
        if test["repo"] not in known_repos:
            raise ManifestError(f"tests[{index}].repo is unknown")
        if test["runner"] not in {"python_unittest", "npm_test"}:
            raise ManifestError(f"tests[{index}].runner is unsupported")
        nodes = _require_sorted_unique_texts(test["nodes"], f"tests[{index}].nodes")
        if test["runner"] == "python_unittest":
            if any(not UNITTEST_NODE_PATTERN.fullmatch(node) for node in nodes):
                raise ManifestError(f"tests[{index}] contains an unsafe unittest node")
        else:
            for node in nodes:
                _require_relative_path(node, f"tests[{index}].nodes[]")
                if not node.endswith((".js", ".jsx", ".ts", ".tsx")):
                    raise ManifestError(f"tests[{index}] contains an unsupported npm test path")
        timeout = test["timeout_seconds"]
        if isinstance(timeout, bool) or not isinstance(timeout, int) or not 1 <= timeout <= 3600:
            raise ManifestError(f"tests[{index}].timeout_seconds must be 1..3600")
        test_ids.append(test_id)
    if test_ids != sorted(set(test_ids)):
        raise ManifestError("tests must be sorted by unique id")


def _repo_roots(manifest: dict[str, Any], workspace_root: Path) -> dict[str, Path]:
    workspace = workspace_root.resolve(strict=True)
    if not workspace.is_dir():
        raise BaselineEnvironmentError(f"workspace root is not a directory: {workspace}")
    roots: dict[str, Path] = {}
    for repo in manifest["repositories"]:
        candidate = (workspace / repo["path"]).resolve(strict=True)
        try:
            candidate.relative_to(workspace)
        except ValueError as exc:
            raise BaselineEnvironmentError(f"repository escapes workspace: {repo['id']}") from exc
        if not candidate.is_dir() or candidate.is_symlink():
            raise BaselineEnvironmentError(f"repository is not a regular directory: {repo['id']}")
        roots[repo["id"]] = candidate
    return roots


def _git_head(repo_root: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            check=False,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    head = result.stdout.strip().lower()
    return head if result.returncode == 0 and GIT_SHA_PATTERN.fullmatch(head) else None


def _surface_path(repo_root: Path, relative_path: str) -> Path | None:
    lexical = repo_root.joinpath(*PurePosixPath(relative_path).parts)
    try:
        info = lexical.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise BaselineEnvironmentError(f"surface is not a regular non-symlink file: {relative_path}")
    resolved = lexical.resolve(strict=True)
    try:
        resolved.relative_to(repo_root)
    except ValueError as exc:
        raise BaselineEnvironmentError(f"surface escapes repository: {relative_path}") from exc
    return resolved


def _read_surface(path: Path) -> bytes:
    size = path.stat().st_size
    if size > MAX_SURFACE_BYTES:
        raise BaselineEnvironmentError(f"surface exceeds {MAX_SURFACE_BYTES} bytes: {path}")
    return path.read_bytes()


def build_test_argv(
    test: dict[str, Any],
    *,
    python_executable: str,
    npm_executable: str,
) -> list[str]:
    if test["runner"] == "python_unittest":
        return [python_executable, "-B", "-m", "unittest", *test["nodes"]]
    if test["runner"] == "npm_test":
        return [npm_executable, "test", "--", *test["nodes"]]
    raise ManifestError(f"unsupported test runner: {test['runner']}")


def _run_test(test: dict[str, Any], repo_root: Path) -> dict[str, Any]:
    npm = shutil.which("npm") or shutil.which("npm.cmd")
    if test["runner"] == "npm_test" and not npm:
        return {"id": test["id"], "status": "environment_error", "returncode": None, "output_tail": "npm is unavailable"}
    argv = build_test_argv(
        test,
        python_executable=sys.executable,
        npm_executable=npm or "npm",
    )
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["CI"] = "1"
    try:
        result = subprocess.run(
            argv,
            cwd=repo_root,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=test["timeout_seconds"],
            check=False,
            shell=False,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        output = (exc.stdout or "") + (exc.stderr or "")
        return {"id": test["id"], "status": "timeout", "returncode": None, "output_tail": output[-MAX_OUTPUT_TAIL:]}
    except OSError as exc:
        return {"id": test["id"], "status": "environment_error", "returncode": None, "output_tail": str(exc)}
    return {
        "id": test["id"],
        "status": "passed" if result.returncode == 0 else "failed",
        "returncode": result.returncode,
        "output_tail": result.stdout[-MAX_OUTPUT_TAIL:],
        "argv": argv,
    }


def render_appendix(manifest: dict[str, Any]) -> str:
    validate_manifest(manifest)
    lines = [
        f"> Generated from `{manifest['baseline_id']}`. Do not edit this block by hand.",
        "",
        "| Repository | Frozen HEAD (informational only) | Owner |",
        "|---|---|---|",
    ]
    for repo in manifest["repositories"]:
        lines.append(f"| `{repo['path']}` | `{repo['frozen_head']}` | {repo['owner']} |")
    lines.extend(["", "| Contract surface | Repository path | Roles | SHA-256 |", "|---|---|---|---|"])
    repo_paths = {repo["id"]: repo["path"] for repo in manifest["repositories"]}
    for surface in manifest["surfaces"]:
        full_path = f"{repo_paths[surface['repo']]}/{surface['path']}"
        lines.append(
            f"| `{surface['id']}` | `{full_path}` | `{','.join(surface['roles'])}` | `{surface['sha256']}` |"
        )
    lines.extend(["", "Exact executable fixtures:", ""])
    for test in manifest["tests"]:
        if test["runner"] == "python_unittest":
            command = "python -B -m unittest " + " ".join(test["nodes"])
        else:
            command = "npm test -- " + " ".join(test["nodes"])
        lines.append(f"- `{test['id']}` (cwd `{repo_paths[test['repo']]}`): `{command}`")
    lines.extend(
        [
            "",
            "Compatibility rule: repository HEAD drift and unlisted-path drift are informational; "
            "a listed surface hash/anchor drift or fixture failure is incompatible pending baseline-owner "
            "and affected-repository-owner review.",
        ]
    )
    return "\n".join(lines)


def _check_appendix(manifest: dict[str, Any], roots: dict[str, Path]) -> bool:
    appendix = manifest["appendix"]
    path = _surface_path(roots[appendix["repo"]], appendix["path"])
    if path is None:
        return False
    text = _read_surface(path).decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
    start = appendix["start_marker"]
    end = appendix["end_marker"]
    if text.count(start) != 1 or text.count(end) != 1:
        return False
    start_index = text.index(start) + len(start)
    end_index = text.index(end)
    if start_index >= end_index:
        return False
    actual = text[start_index:end_index].strip("\r\n")
    return actual == render_appendix(manifest)


def verify_baseline(
    manifest: dict[str, Any],
    workspace_root: Path,
    *,
    run_tests: bool,
) -> dict[str, Any]:
    validate_manifest(manifest)
    roots = _repo_roots(manifest, workspace_root)
    failures: list[dict[str, Any]] = []
    repositories = []
    frozen_heads = {repo["id"]: repo["frozen_head"] for repo in manifest["repositories"]}
    for repo in manifest["repositories"]:
        current_head = _git_head(roots[repo["id"]])
        repositories.append(
            {
                "id": repo["id"],
                "frozen_head": frozen_heads[repo["id"]],
                "current_head": current_head,
                "head_matches": current_head == frozen_heads[repo["id"]] if current_head else None,
                "head_status": "informational",
            }
        )

    surface_reports = []
    hashes_match = True
    for surface in manifest["surfaces"]:
        try:
            path = _surface_path(roots[surface["repo"]], surface["path"])
        except BaselineEnvironmentError as exc:
            path = None
            failures.append({"code": "surface_unsafe", "surface_id": surface["id"], "message": str(exc)})
        if path is None:
            hashes_match = False
            if not any(failure.get("surface_id") == surface["id"] for failure in failures):
                failures.append({"code": "surface_missing", "surface_id": surface["id"]})
            surface_reports.append({"id": surface["id"], "status": "missing", "actual_sha256": None})
            continue
        raw = _read_surface(path)
        actual_sha256 = hashlib.sha256(raw).hexdigest()
        try:
            source_text = raw.decode("utf-8")
        except UnicodeDecodeError:
            source_text = ""
        missing_anchors = [anchor for anchor in surface["anchors"] if anchor not in source_text]
        for anchor in missing_anchors:
            failures.append({"code": "anchor_missing", "surface_id": surface["id"], "anchor": anchor})
        if actual_sha256 != surface["sha256"]:
            hashes_match = False
            failures.append(
                {
                    "code": "surface_hash_mismatch",
                    "surface_id": surface["id"],
                    "expected_sha256": surface["sha256"],
                    "actual_sha256": actual_sha256,
                }
            )
        surface_reports.append(
            {
                "id": surface["id"],
                "status": "match" if actual_sha256 == surface["sha256"] and not missing_anchors else "drift",
                "actual_sha256": actual_sha256,
                "missing_anchors": missing_anchors,
            }
        )

    appendix_matches = _check_appendix(manifest, roots)
    if not appendix_matches:
        failures.append({"code": "appendix_drift", "repo": manifest["appendix"]["repo"], "path": manifest["appendix"]["path"]})

    test_reports = []
    for test in manifest["tests"]:
        if not run_tests:
            test_reports.append({"id": test["id"], "status": "not_run"})
            continue
        result = _run_test(test, roots[test["repo"]])
        test_reports.append(result)
        if result["status"] != "passed":
            failures.append({"code": "test_failed", "test_id": test["id"], "status": result["status"]})

    return {
        "schema_id": REPORT_SCHEMA_ID,
        "baseline_id": manifest["baseline_id"],
        "protocol_version": manifest["protocol_version"],
        "compatible": not failures,
        "hashes_match": hashes_match,
        "appendix_matches": appendix_matches,
        "tests_run": run_tests,
        "repositories": repositories,
        "surfaces": surface_reports,
        "tests": test_reports,
        "failures": failures,
    }


def create_candidate(manifest: dict[str, Any], workspace_root: Path) -> dict[str, Any]:
    validate_manifest(manifest)
    candidate = copy.deepcopy(manifest)
    roots = _repo_roots(candidate, workspace_root)
    for repo in candidate["repositories"]:
        current_head = _git_head(roots[repo["id"]])
        if current_head:
            repo["frozen_head"] = current_head
    for surface in candidate["surfaces"]:
        path = _surface_path(roots[surface["repo"]], surface["path"])
        if path is None:
            raise BaselineEnvironmentError(f"candidate surface is missing: {surface['id']}")
        raw = _read_surface(path)
        text = raw.decode("utf-8")
        missing = [anchor for anchor in surface["anchors"] if anchor not in text]
        if missing:
            raise BaselineEnvironmentError(f"candidate surface lost anchors {missing}: {surface['id']}")
        surface["sha256"] = hashlib.sha256(raw).hexdigest()
    return candidate


def _error_report(kind: str, message: str) -> dict[str, Any]:
    return {"schema_id": REPORT_SCHEMA_ID, "compatible": False, "error_kind": kind, "message": message}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("check", "candidate"):
        sub = subparsers.add_parser(command)
        sub.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
        sub.add_argument("--workspace-root", type=Path, required=True)
    render = subparsers.add_parser("render-appendix")
    render.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    args = parser.parse_args(argv)
    try:
        manifest = load_manifest(args.manifest)
        if args.command == "check":
            report = verify_baseline(manifest, args.workspace_root, run_tests=True)
            print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
            return 0 if report["compatible"] else 1
        if args.command == "candidate":
            candidate = create_candidate(manifest, args.workspace_root)
            print(json.dumps(candidate, ensure_ascii=False, indent=2) + "")
            return 0
        print(render_appendix(manifest))
        return 0
    except ManifestError as exc:
        print(json.dumps(_error_report("manifest_invalid", str(exc)), ensure_ascii=False, indent=2, sort_keys=True))
        return 2
    except (BaselineEnvironmentError, OSError, UnicodeError) as exc:
        print(json.dumps(_error_report("environment_invalid", str(exc)), ensure_ascii=False, indent=2, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
