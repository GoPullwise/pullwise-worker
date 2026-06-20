from __future__ import annotations

from pathlib import Path

from ..config import ReviewConfig
from ..inventory.git_inventory import analyzable_files
from .contracts import language_for_path, risk_tags_for_path


MANIFEST_NAMES = {"package.json", "pyproject.toml", "setup.py", "Cargo.toml", "go.mod", "pom.xml", "build.gradle"}


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
    max_files = config.graph.max_shard_files
    max_bytes = config.graph.max_shard_bytes
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
