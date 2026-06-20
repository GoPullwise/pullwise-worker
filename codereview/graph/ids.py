from __future__ import annotations

import hashlib
import json


def short_hash(value: object, *, length: int = 10) -> str:
    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()[:length]


def stable_node_id(
    *,
    language: str,
    file: str,
    qualified_name: str,
    kind: str,
    signature: str = "",
) -> str:
    signature_hash = short_hash(signature or qualified_name or file, length=8)
    return f"sym:{language}:{file}::{qualified_name or '<module>'}::{kind}::{signature_hash}"


def file_node_id(file: str) -> str:
    return f"file:{file}"


def directory_node_id(path: str) -> str:
    return f"dir:{path or '.'}"


def config_node_id(name: str) -> str:
    return f"config:{name}"


def env_node_id(name: str) -> str:
    return f"env:{name}"


def dependency_node_id(name: str) -> str:
    return f"dep:{name}"


def route_node_id(method: str, route: str) -> str:
    return f"entry:http:{method.upper()}:{route}"


def table_node_id(name: str) -> str:
    return f"data:table:{name}"


def stable_edge_id(source: str, target: str, edge_type: str, evidence: object = None) -> str:
    return f"edge:sha256:{short_hash({'from': source, 'to': target, 'type': edge_type, 'evidence': evidence or []}, length=20)}"
