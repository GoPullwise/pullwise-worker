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


SECURITY_RISK_TAGS = {
    "auth",
    "authentication",
    "authorization",
    "input-validation",
    "validation",
    "secret-handling",
    "trust-boundary",
    "filesystem",
    "network",
    "tenant-isolation",
}
API_CONTRACT_RISK_TAGS = {"public-entrypoint", "api-contract", "serialization", "deserialization", "cross-boundary"}
STATE_RISK_TAGS = {"state", "db-write", "transaction", "cache", "async", "concurrency", "resource", "resource-lifecycle"}


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
    if SECURITY_RISK_TAGS & risk_tags:
        finders.append("security_auth_dataflow")
    elif API_CONTRACT_RISK_TAGS & risk_tags:
        finders.append("api_contract")
    elif STATE_RISK_TAGS & risk_tags:
        finders.append("state_concurrency_resource")
    return list(dict.fromkeys(finders))
