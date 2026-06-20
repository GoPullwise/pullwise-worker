from __future__ import annotations


RISK_KEYWORDS = {
    "auth": {"auth", "login", "token", "permission", "role", "session"},
    "authorization": {"authorize", "authorization", "acl", "policy"},
    "input-validation": {"validate", "parse", "deserialize", "schema"},
    "filesystem": {"file", "path", "fs", "open", "write", "read"},
    "network": {"http", "request", "fetch", "url", "socket"},
    "public-entrypoint": {"route", "api", "handler", "main", "cli", "worker"},
    "api-contract": {"request", "response", "payload", "contract"},
    "serialization": {"json", "serialize", "schema"},
    "deserialization": {"parse", "loads", "decode"},
    "db-write": {"insert", "update", "delete", "commit", "transaction"},
    "transaction": {"transaction", "rollback", "commit"},
    "cache": {"cache", "ttl", "memo"},
    "async": {"async", "await", "thread", "future"},
    "concurrency": {"lock", "thread", "parallel", "concurrent"},
    "resource": {"cleanup", "timeout", "limit", "quota"},
}


def risk_tags_for_symbol(item: dict) -> list[str]:
    haystack = " ".join(str(item.get(key) or "") for key in ("file", "symbol")).lower()
    tags = []
    for tag, keywords in RISK_KEYWORDS.items():
        if any(keyword in haystack for keyword in keywords):
            tags.append(tag)
    if not tags:
        tags.append("isolated-helper")
    return sorted(dict.fromkeys(tags))


def choose_finders(risk_tags: set[str]) -> list[str]:
    finders = ["correctness"]
    if {"auth", "authorization", "input-validation", "filesystem", "network"} & risk_tags:
        finders.append("security_auth_dataflow")
    if {"public-entrypoint", "api-contract", "serialization", "deserialization"} & risk_tags:
        finders.append("api_contract")
    if {"db-write", "transaction", "cache", "async", "concurrency", "resource"} & risk_tags:
        finders.append("state_concurrency_resource")
    finders.append("test_repro")
    return list(dict.fromkeys(finders))
