from __future__ import annotations

from pathlib import Path


SOURCE_EXTENSIONS = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".json": "json",
    ".toml": "toml",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".sql": "sql",
    ".sh": "shell",
    ".html": "html",
    ".css": "css",
    ".md": "markdown",
}
HIGH_RISK_KEYWORDS = {
    "auth": "authorization",
    "permission": "authorization",
    "tenant": "authorization",
    "login": "authentication",
    "password": "secret-handling",
    "token": "secret-handling",
    "secret": "secret-handling",
    "payment": "payment",
    "billing": "payment",
    "delete": "state-write",
    "update": "state-write",
    "insert": "state-write",
    "transaction": "transaction",
    "cache": "cache",
    "worker": "job-handler",
    "route": "public-entrypoint",
    "api": "public-entrypoint",
}
ENTRYPOINT_KINDS = {"http_route", "cli_command", "job_handler", "event_handler"}
STATE_EDGE_TYPES = {"writes_database", "writes_file", "writes_cache", "publishes_message", "calls_external"}
CALL_EDGE_TYPES = {"calls", "route_to", "cli_to", "job_to", "dispatches_to"}


def language_for_path(path: str) -> str:
    return SOURCE_EXTENSIONS.get(Path(path).suffix.lower(), "text")


def risk_tags_for_path(path: str, name: str = "") -> list[str]:
    haystack = f"{path} {name}".lower()
    tags = [tag for keyword, tag in HIGH_RISK_KEYWORDS.items() if keyword in haystack]
    if any(part in haystack for part in ("test", "spec")):
        tags.append("test")
    if not tags:
        tags.append("source")
    return sorted(dict.fromkeys(tags))


def confidence_for_evidence(evidence_kind: str) -> str:
    if evidence_kind in {"direct_syntax", "unique_resolution"}:
        return "high"
    if evidence_kind in {"repository_convention", "dynamic_dispatch"}:
        return "medium"
    if evidence_kind == "unresolved":
        return "none"
    return "low"
