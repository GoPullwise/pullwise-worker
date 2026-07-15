from __future__ import annotations

import os
import shlex
import shutil
from pathlib import Path
from typing import Any, Callable, Iterable


EXECUTION_DESCRIPTOR_NAMES = {
    "Cargo.toml",
    "Gemfile",
    "Makefile",
    "build.gradle",
    "build.gradle.kts",
    "composer.json",
    "deno.json",
    "deno.jsonc",
    "go.mod",
    "package.json",
    "pom.xml",
    "pyproject.toml",
    "requirements.txt",
    "setup.py",
}

# These are capability queries, not execution recipes.  Agent proposals remain
# authoritative for the command, cwd, and test strategy used for a repository.
BASELINE_EXECUTABLE_QUERIES = (
    "bun",
    "cargo",
    "cmake",
    "composer",
    "deno",
    "dotnet",
    "go",
    "gradle",
    "java",
    "make",
    "mvn",
    "node",
    "npm",
    "php",
    "pnpm",
    "python",
    "python3",
    "ruby",
    "swift",
    "yarn",
)

IGNORED_DISCOVERY_DIRECTORIES = {
    ".codex-review",
    ".git",
    ".hg",
    ".svn",
    "__pycache__",
    "node_modules",
    "target",
    "vendor",
}

COMMAND_KEYS = (
    "command",
    "test_command",
    "testCommand",
    "run_command",
    "runCommand",
    "runnable_command",
    "runnableCommand",
)

CANDIDATE_KEYS = (
    "execution_candidates",
    "executionCandidates",
    "candidate_commands",
    "candidateCommands",
    "test_commands",
    "testCommands",
)


def _command(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(part) for part in value if str(part).strip()]
    if isinstance(value, str) and value.strip():
        try:
            return shlex.split(value)
        except ValueError:
            return []
    return []


def _target_id(value: dict[str, Any], fallback: str) -> str:
    return str(
        value.get("test_id")
        or value.get("testId")
        or value.get("target_id")
        or value.get("targetId")
        or value.get("id")
        or fallback
    ).strip()


def _candidate_from_value(
    value: object,
    *,
    test_id: str,
    inherited_cwd: str,
) -> dict[str, Any] | None:
    if isinstance(value, dict):
        command: list[str] = []
        for key in COMMAND_KEYS:
            command = _command(value.get(key))
            if command:
                break
        cwd = str(value.get("cwd") or value.get("working_directory") or inherited_cwd or ".").strip() or "."
        required_paths = value.get("required_paths") or value.get("requiredPaths") or []
        if not isinstance(required_paths, list):
            required_paths = [required_paths]
    else:
        command = _command(value)
        cwd = inherited_cwd or "."
        required_paths = []
    if not command:
        return None
    return {
        "test_id": test_id,
        "command": command,
        "cwd": cwd,
        "required_paths": [str(path) for path in required_paths if str(path).strip()],
        "source": "agent_proposal",
    }


def collect_execution_candidates(*sources: object) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    seen: set[tuple[str, tuple[str, ...], str]] = set()

    def add(candidate: dict[str, Any] | None) -> None:
        if candidate is None:
            return
        key = (
            str(candidate.get("test_id") or ""),
            tuple(str(part) for part in candidate.get("command") or []),
            str(candidate.get("cwd") or "."),
        )
        if key in seen:
            return
        seen.add(key)
        candidates.append(candidate)

    def visit(value: object, inherited_test_id: str = "", inherited_cwd: str = ".") -> None:
        if isinstance(value, list):
            for index, item in enumerate(value):
                visit(item, inherited_test_id or f"candidate-{index + 1}", inherited_cwd)
            return
        if not isinstance(value, dict):
            return
        test_id = _target_id(value, inherited_test_id)
        cwd = str(value.get("cwd") or value.get("working_directory") or inherited_cwd or ".").strip() or "."
        for key in CANDIDATE_KEYS:
            raw_candidates = value.get(key)
            if not isinstance(raw_candidates, list):
                continue
            for raw_candidate in raw_candidates:
                add(_candidate_from_value(raw_candidate, test_id=test_id, inherited_cwd=cwd))
        for key in COMMAND_KEYS:
            if value.get(key) is not None:
                add(_candidate_from_value(value, test_id=test_id, inherited_cwd=cwd))
                break
        for key, child in value.items():
            if key in CANDIDATE_KEYS or key in COMMAND_KEYS:
                continue
            if isinstance(child, (dict, list)):
                visit(child, test_id, cwd)

    for source in sources:
        visit(source)
    return candidates


def discover_execution_workspaces(repo_root: Path, *, max_descriptors: int = 256) -> list[dict[str, Any]]:
    root = repo_root.resolve(strict=False)
    workspaces: dict[str, dict[str, Any]] = {}
    descriptors_seen = 0
    if not root.is_dir():
        return []
    for current, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        dirnames[:] = [
            name
            for name in dirnames
            if name not in IGNORED_DISCOVERY_DIRECTORIES and not (current_path / name).is_symlink()
        ]
        descriptors = sorted(name for name in filenames if name in EXECUTION_DESCRIPTOR_NAMES)
        if not descriptors:
            continue
        rel_root = current_path.relative_to(root).as_posix()
        if rel_root == ".":
            rel_root = "."
        workspace = workspaces.setdefault(
            rel_root,
            {
                "root": rel_root,
                "descriptors": [],
                "dependency_state": {},
            },
        )
        for name in descriptors:
            if descriptors_seen >= max_descriptors:
                break
            workspace["descriptors"].append(name)
            descriptors_seen += 1
        workspace["dependency_state"] = {
            "node_modules": (current_path / "node_modules").is_dir(),
            "python_venv": any((current_path / name).is_dir() for name in (".venv", "venv")),
            "ruby_bundle": (current_path / "vendor" / "bundle").is_dir(),
            "php_vendor": (current_path / "vendor").is_dir(),
            "rust_target": (current_path / "target").is_dir(),
        }
        if descriptors_seen >= max_descriptors:
            break
    return [workspaces[key] for key in sorted(workspaces)]


def _resolved_executable(
    executable: str,
    resolver: Callable[[str], str | None],
) -> str | None:
    path = Path(executable)
    if path.is_absolute():
        return str(path.resolve(strict=False)) if path.is_file() else None
    resolved = resolver(executable)
    return str(resolved) if resolved else None


def build_execution_capabilities(
    repo_root: Path,
    *,
    proposal_sources: Iterable[object] = (),
    executable_resolver: Callable[[str], str | None] = shutil.which,
    sandbox_available: bool,
) -> dict[str, Any]:
    candidates = collect_execution_candidates(*tuple(proposal_sources))
    runtime_names = set(BASELINE_EXECUTABLE_QUERIES)
    runtime_names.update(
        str(candidate["command"][0])
        for candidate in candidates
        if candidate.get("command")
    )
    runtimes = []
    resolved_by_name: dict[str, str | None] = {}
    for name in sorted(runtime_names, key=lambda item: (Path(item).name.lower(), item)):
        resolved = _resolved_executable(name, executable_resolver)
        resolved_by_name[name] = resolved
        runtimes.append(
            {
                "name": name,
                "available": resolved is not None,
                "path": resolved or "",
            }
        )
    candidate_payloads = []
    for candidate in candidates:
        command = list(candidate.get("command") or [])
        executable = str(command[0]) if command else ""
        resolved = resolved_by_name.get(executable)
        if executable and executable not in resolved_by_name:
            resolved = _resolved_executable(executable, executable_resolver)
        candidate_payloads.append(
            {
                **candidate,
                "executable": {
                    "name": Path(executable).name if executable else "",
                    "available": resolved is not None,
                    "path": resolved or "",
                },
            }
        )
    return {
        "schema_version": "agentic-execution-capabilities/v1",
        "source": "mechanical_runtime_and_agent_proposals",
        "repository_root": str(repo_root.resolve(strict=False)),
        "constraints": {
            "dependency_install": False,
            "network": False,
            "production_secrets": False,
            "source_modification": False,
            "sandbox_required": True,
            "sandbox_available": bool(sandbox_available),
        },
        "workspaces": discover_execution_workspaces(repo_root),
        "runtimes": runtimes,
        "agent_candidates": candidate_payloads,
    }

