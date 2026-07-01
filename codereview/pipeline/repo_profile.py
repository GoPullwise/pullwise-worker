from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from codereview.utils.paths import safe_relative_path

_HIGH_RISK_PATH_TOKENS = {
    "auth": 24,
    "oauth": 24,
    "token": 24,
    "secret": 24,
    "credential": 22,
    "permission": 20,
    "quota": 18,
    "billing": 18,
    "worker": 16,
    "job": 16,
    "queue": 16,
    "claim": 16,
    "result": 16,
    "upload": 14,
    "progress": 12,
    "retry": 14,
    "backoff": 14,
    "timeout": 12,
    "cancel": 16,
    "concurrent": 18,
    "thread": 18,
    "lock": 18,
    "process": 14,
    "subprocess": 18,
    "sandbox": 20,
    "repro": 18,
    "verification": 18,
    "evidence": 18,
    "filesystem": 14,
    "path": 10,
    "symlink": 20,
    "delete": 16,
    "cleanup": 14,
}

_MANIFEST_NAMES = {
    "package.json",
    "pyproject.toml",
    "setup.py",
    "requirements.txt",
    "poetry.lock",
    "Cargo.toml",
    "go.mod",
    "pom.xml",
    "build.gradle",
    "Dockerfile",
    "docker-compose.yml",
    ".github/workflows",
}

_ENTRYPOINT_PATTERNS = (
    re.compile(r"(^|/)main\.(py|js|ts|go|rb|php)$"),
    re.compile(r"(^|/)cli\.(py|js|ts)$"),
    re.compile(r"(^|/)worker(_|\.|/|$)"),
    re.compile(r"(^|/)server(_|\.|/|$)"),
    re.compile(r"(^|/)app\.(py|js|ts)$"),
)

_LANGUAGE_BY_SUFFIX = {
    ".py": "python",
    ".js": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".go": "go",
    ".rs": "rust",
    ".rb": "ruby",
    ".php": "php",
    ".java": "java",
    ".kt": "kotlin",
    ".cs": "csharp",
    ".sh": "shell",
    ".bash": "shell",
    ".yml": "yaml",
    ".yaml": "yaml",
    ".toml": "toml",
    ".json": "json",
}


@dataclass(frozen=True)
class RepoProfile:
    file_count: int
    analyzable_bytes: int
    languages: tuple[str, ...]
    manifests: tuple[str, ...]
    entrypoints: tuple[str, ...]
    high_risk_paths: dict[str, int]
    risk_areas: tuple[str, ...]

    def risk_for_path(self, path: object) -> int:
        safe = safe_relative_path(path)
        if not safe:
            return 0
        return max(0, int(self.high_risk_paths.get(safe, 0)))

    def to_dict(self) -> dict:
        return {
            "schemaVersion": "pullwise-repo-profile/1",
            "fileCount": self.file_count,
            "analyzableBytes": self.analyzable_bytes,
            "languages": list(self.languages),
            "manifests": list(self.manifests),
            "entrypoints": list(self.entrypoints),
            "riskAreas": list(self.risk_areas),
            "highRiskPaths": [
                {"path": path, "score": score}
                for path, score in sorted(self.high_risk_paths.items(), key=lambda item: (-item[1], item[0]))[:200]
            ],
        }


def build_repo_profile(checkout: Path, inventory_files: Iterable[dict]) -> RepoProfile:
    del checkout
    files = [item for item in inventory_files if isinstance(item, dict)]
    language_counts: dict[str, int] = {}
    manifests: list[str] = []
    entrypoints: list[str] = []
    risk_by_path: dict[str, int] = {}
    risk_area_counts: dict[str, int] = {}
    total_bytes = 0

    for item in files:
        path = safe_relative_path(item.get("path"))
        if not path:
            continue
        try:
            total_bytes += max(0, int(item.get("size_bytes") or 0))
        except (TypeError, ValueError, OverflowError):
            pass
        suffix = Path(path).suffix.lower()
        language = _LANGUAGE_BY_SUFFIX.get(suffix)
        if language:
            language_counts[language] = language_counts.get(language, 0) + 1
        if _is_manifest_path(path):
            manifests.append(path)
        if _is_entrypoint_path(path):
            entrypoints.append(path)
        risk, areas = path_risk_score(path)
        if risk > 0:
            risk_by_path[path] = risk
            for area in areas:
                risk_area_counts[area] = risk_area_counts.get(area, 0) + 1

    languages = tuple(
        key for key, _count in sorted(language_counts.items(), key=lambda item: (-item[1], item[0]))
    )
    risk_areas = tuple(
        key for key, _count in sorted(risk_area_counts.items(), key=lambda item: (-item[1], item[0]))[:24]
    )
    return RepoProfile(
        file_count=len(files),
        analyzable_bytes=total_bytes,
        languages=languages,
        manifests=tuple(sorted(dict.fromkeys(manifests))),
        entrypoints=tuple(sorted(dict.fromkeys(entrypoints))[:80]),
        high_risk_paths=dict(sorted(risk_by_path.items(), key=lambda item: (-item[1], item[0]))[:500]),
        risk_areas=risk_areas,
    )


def path_risk_score(path: str) -> tuple[int, tuple[str, ...]]:
    normalized = path.lower().replace("-", "_").replace(".", "_")
    score = 0
    areas: list[str] = []
    for token, weight in _HIGH_RISK_PATH_TOKENS.items():
        if token in normalized:
            score += weight
            areas.append(token)
    if path.startswith(".github/workflows/"):
        score += 18
        areas.append("workflow")
    if "/tests/" in f"/{path}" or path.startswith("tests/"):
        score -= 10
    if path.endswith((".md", ".txt")):
        score -= 8
    return max(0, min(100, score)), tuple(dict.fromkeys(areas))


def _is_manifest_path(path: str) -> bool:
    if path.startswith(".github/workflows/"):
        return True
    name = Path(path).name
    return name in _MANIFEST_NAMES or path in _MANIFEST_NAMES


def _is_entrypoint_path(path: str) -> bool:
    return any(pattern.search(path) for pattern in _ENTRYPOINT_PATTERNS)


def repo_profile_from_json(value: object) -> RepoProfile | None:
    if not isinstance(value, dict):
        return None
    try:
        high_risk_paths = {
            str(item.get("path") or ""): max(0, int(item.get("score") or 0))
            for item in value.get("highRiskPaths", [])
            if isinstance(item, dict) and item.get("path")
        }
        return RepoProfile(
            file_count=max(0, int(value.get("fileCount") or 0)),
            analyzable_bytes=max(0, int(value.get("analyzableBytes") or 0)),
            languages=tuple(str(item) for item in value.get("languages", []) if str(item)),
            manifests=tuple(str(item) for item in value.get("manifests", []) if str(item)),
            entrypoints=tuple(str(item) for item in value.get("entrypoints", []) if str(item)),
            high_risk_paths=high_risk_paths,
            risk_areas=tuple(str(item) for item in value.get("riskAreas", []) if str(item)),
        )
    except (TypeError, ValueError, OverflowError):
        return None


def dumps_profile(profile: RepoProfile) -> str:
    return json.dumps(profile.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
