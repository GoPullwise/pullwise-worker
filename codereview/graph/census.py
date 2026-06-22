from __future__ import annotations

import json
from pathlib import Path

from ..codex_runner import base_env, run_codex_turn
from ..config import ReviewConfig, auxiliary_codex_config
from ..inventory.git_inventory import analyzable_files
from ..utils.jsonl import write_json, write_text
from ..utils.process import compact_process_output
from .contracts import language_for_path, risk_tags_for_path


MANIFEST_NAMES = {"package.json", "pyproject.toml", "setup.py", "Cargo.toml", "go.mod", "pom.xml", "build.gradle"}


def run_repository_census(checkout: Path, run: Path, inventory: dict, config: ReviewConfig) -> dict:
    deterministic = build_repository_census(inventory, config)
    if not config.graph.codex_census:
        return {**deterministic, "census_source": "deterministic"}

    prompt_file = checkout / ".codereview" / "prompts" / "repo-census.md"
    schema = checkout / ".codereview" / "schemas" / "repo-census.schema.json"
    if not prompt_file.is_file() or not schema.is_file():
        raise RuntimeError("repository census prompt or schema missing")

    worker = run / "workers" / "census-0001"
    worker.mkdir(parents=True, exist_ok=True)
    task = {
        "inventory_summary": inventory.get("summary") or {},
        "files": [
            {
                "path": item.get("path"),
                "size_bytes": item.get("size_bytes"),
                "line_count": item.get("line_count"),
                "content_hash": item.get("content_hash"),
                "extension": item.get("extension"),
                "scope": item.get("scope"),
                "reason": item.get("reason"),
                "git_status": item.get("git_status"),
            }
            for item in inventory.get("files", [])
            if isinstance(item, dict)
        ],
        "deterministic_seed": deterministic,
        "shard_policy": _shard_policy(config),
        "manifest_previews": _manifest_previews(checkout, inventory),
    }
    prompt = "\n\n".join(
        [
            prompt_file.read_text(encoding="utf-8"),
            "Repository census input JSON:",
            "```json",
            json.dumps(task, ensure_ascii=False, indent=2, sort_keys=True),
            "```",
        ]
    )
    write_json(worker / "task.json", task)
    write_text(worker / "prompt.md", prompt)
    output = worker / "result.json"
    events = worker / "events.jsonl"
    codex_config = auxiliary_codex_config(config)
    process = run_codex_turn(
        cd=checkout,
        prompt=prompt,
        output_schema=schema,
        output_file=output,
        sandbox="read-only",
        timeout_seconds=config.graph.graph_timeout_seconds,
        config=codex_config,
        env=base_env(checkout, codex_config),
        events_file=events,
    )
    process_payload = {**process.to_dict(), "events_path": str(events)}
    write_json(worker / "process.json", process_payload)
    if process.returncode != 0:
        detail = compact_process_output(process)
        raise RuntimeError(f"repository census agent failed with exit code {process.returncode}: {detail}")
    if not output.is_file():
        detail = compact_process_output(process)
        raise RuntimeError(f"repository census agent did not produce an output file: {detail}")
    try:
        parsed = json.loads(output.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"repository census agent produced invalid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError("repository census agent produced non-object JSON")
    errors = validate_census_coverage(parsed, inventory)
    if errors:
        raise RuntimeError(f"repository census agent failed coverage validation: {'; '.join(errors)}")
    parsed["census_source"] = "codex"
    parsed["process"] = process_payload
    return parsed


def build_repository_census(inventory: dict, config: ReviewConfig) -> dict:
    files = analyzable_files(inventory)
    languages = sorted({language_for_path(str(item.get("path") or "")) for item in files})
    manifests = [str(item.get("path") or "") for item in inventory.get("files", []) if Path(str(item.get("path") or "")).name in MANIFEST_NAMES]
    packages = _packages_from_manifests(manifests)
    high_risk_roots = _high_risk_roots(files)
    entrypoints = _entrypoint_candidates(files)
    shards = _shards(files, config)
    return {
        "languages": languages,
        "packages": packages,
        "shard_policy": _shard_policy(config),
        "source_roots": sorted({root for pkg in packages for root in pkg.get("source_roots", [])}),
        "test_roots": sorted({root for pkg in packages for root in pkg.get("test_roots", [])}),
        "manifest_files": manifests,
        "entrypoint_candidates": entrypoints,
        "high_risk_roots": high_risk_roots,
        "shards": shards,
        "coverage": _shard_coverage(files, shards),
    }


def validate_census_coverage(census: dict, inventory: dict) -> list[str]:
    expected = {str(item.get("path") or "") for item in analyzable_files(inventory)}
    assigned: list[str] = []
    for shard in census.get("shards", []):
        if isinstance(shard, dict):
            assigned.extend(str(path) for path in shard.get("files", []) if str(path))
    duplicates = sorted({path for path in assigned if assigned.count(path) > 1})
    missing = sorted(expected - set(assigned))
    extra = sorted(set(assigned) - expected)
    errors = []
    if duplicates:
        errors.append(f"duplicate shard assignment: {', '.join(duplicates[:20])}")
    if missing:
        errors.append(f"missing shard assignment: {', '.join(missing[:20])}")
    if extra:
        errors.append(f"unknown shard assignment: {', '.join(extra[:20])}")
    return errors


def _packages_from_manifests(manifests: list[str]) -> list[dict]:
    roots = sorted({str(Path(path).parent).strip(".") for path in manifests}) or ["."]
    packages = []
    for index, root in enumerate(roots):
        package_root = "." if root in {"", "."} else root
        prefix = "" if package_root == "." else f"{package_root}/"
        packages.append(
            {
                "id": f"pkg:{index + 1}:{package_root}",
                "root": package_root,
                "source_roots": [f"{prefix}src".strip("/")],
                "test_roots": [f"{prefix}tests".strip("/"), f"{prefix}test".strip("/")],
                "manifest_files": [path for path in manifests if path.startswith(prefix) or package_root == "."],
            }
        )
    return packages


def _high_risk_roots(files: list[dict]) -> list[dict]:
    roots: dict[str, set[str]] = {}
    for item in files:
        path = str(item.get("path") or "")
        tags = set(risk_tags_for_path(path))
        high = tags - {"source", "test"}
        if not high:
            continue
        parent = str(Path(path).parent).replace("\\", "/")
        roots.setdefault(parent or ".", set()).update(high)
    return [{"path": path, "tags": sorted(tags)} for path, tags in sorted(roots.items())]


def _entrypoint_candidates(files: list[dict]) -> list[str]:
    candidates = []
    for item in files:
        path = str(item.get("path") or "")
        lower = path.lower()
        if any(token in lower for token in ("/routes", "/api", "server.", "app.", "main.", "cli.", "worker.")):
            candidates.append(path)
    return candidates[:200]


def _shards(files: list[dict], config: ReviewConfig) -> list[dict]:
    shards: list[dict] = []
    current: list[str] = []
    current_bytes = 0
    max_files = _effective_max_shard_files(files, config)
    max_bytes = _effective_max_shard_bytes(files, config)
    for item in sorted(files, key=lambda row: str(row.get("path") or "")):
        path = str(item.get("path") or "")
        size = int(item.get("size_bytes") or 0)
        if current and (len(current) >= max_files or current_bytes + size > max_bytes or size > config.graph.large_file_bytes):
            shards.append(_shard(len(shards) + 1, current))
            current = []
            current_bytes = 0
        current.append(path)
        current_bytes += size
        if size > config.graph.large_file_bytes:
            shards.append(_shard(len(shards) + 1, current, reason="large or high-value file"))
            current = []
            current_bytes = 0
    if current:
        shards.append(_shard(len(shards) + 1, current))
    return shards


def _shard_policy(config: ReviewConfig) -> dict:
    return {
        "target_shards": config.graph.target_shards,
        "mapper_subagent_limit": config.graph.mapper_subagent_limit,
        "max_shard_files": config.graph.max_shard_files,
        "max_shard_bytes": config.graph.max_shard_bytes,
        "large_file_bytes": config.graph.large_file_bytes,
        "note": "Target primary shards at the mapper subagent limit when file and byte budgets allow.",
    }


def _effective_max_shard_files(files: list[dict], config: ReviewConfig) -> int:
    target = max(1, int(getattr(config.graph, "target_shards", 1)))
    target_files = (len(files) + target - 1) // target if files else 1
    return max(config.graph.max_shard_files, target_files)


def _effective_max_shard_bytes(files: list[dict], config: ReviewConfig) -> int:
    target = max(1, int(getattr(config.graph, "target_shards", 1)))
    total_bytes = sum(max(0, int(item.get("size_bytes") or 0)) for item in files)
    target_bytes = (total_bytes + target - 1) // target if files else 1
    return max(config.graph.max_shard_bytes, target_bytes)


def _shard(index: int, files: list[str], reason: str = "bounded file and byte budget") -> dict:
    return {"shard_id": f"shard-{index:04d}", "files": files, "reason": reason}


def _shard_coverage(files: list[dict], shards: list[dict]) -> dict:
    expected = {str(item.get("path") or "") for item in files}
    assigned = {str(path) for shard in shards for path in shard.get("files", [])}
    return {
        "analyzable_files": len(expected),
        "assigned_files": len(assigned & expected),
        "missing_files": sorted(expected - assigned),
        "extra_files": sorted(assigned - expected),
    }


def _manifest_previews(checkout: Path, inventory: dict) -> list[dict]:
    previews = []
    for item in inventory.get("files", []):
        if not isinstance(item, dict):
            continue
        rel = str(item.get("path") or "")
        if Path(rel).name not in MANIFEST_NAMES:
            continue
        path = checkout / rel
        text = path.read_text(encoding="utf-8", errors="replace")[:12000] if path.is_file() else ""
        previews.append({"path": rel, "content": text})
        if len(previews) >= 50:
            break
    return previews
