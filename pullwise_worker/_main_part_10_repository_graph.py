from __future__ import annotations

# Loaded by main.py; keep definitions in that module's globals for compatibility.

import ast
import hashlib

REPOSITORY_GRAPH_PROTOCOL_VERSION = "repository-graph/0.1"
REPOSITORY_SEMANTIC_GRAPH_PROTOCOL_VERSION = "semantic-code-graph/0.1"
REPOSITORY_GRAPH_MAX_NODES = 120
REPOSITORY_GRAPH_MAX_EDGES = 240
REPOSITORY_GRAPH_MAX_SEMANTIC_NODES = 120
REPOSITORY_GRAPH_MAX_SEMANTIC_EDGES = 240
REPOSITORY_GRAPH_MAX_PROMPT_CHARS = 2048
REPOSITORY_GRAPH_IGNORED_DIRS = {
    ".git",
    ".hg",
    ".svn",
    "node_modules",
    ".venv",
    "venv",
    "env",
    "__pycache__",
    "dist",
    "build",
    "coverage",
    ".next",
    ".nuxt",
    ".cache",
}
REPOSITORY_GRAPH_SOURCE_PREFIX_BYTES = 16 * 1024
REPOSITORY_GRAPH_MAX_FILES = 2000

_REPOSITORY_GRAPH_SOURCE_EXTENSIONS = {
    ".c",
    ".cc",
    ".cpp",
    ".cs",
    ".css",
    ".go",
    ".html",
    ".java",
    ".js",
    ".jsx",
    ".kt",
    ".mjs",
    ".php",
    ".py",
    ".rb",
    ".rs",
    ".svelte",
    ".swift",
    ".ts",
    ".tsx",
    ".vue",
}
_REPOSITORY_GRAPH_CONFIG_EXTENSIONS = {".json", ".toml", ".yaml", ".yml"}
_REPOSITORY_GRAPH_JS_EXTENSIONS = (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs")
_REPOSITORY_GRAPH_MANIFEST_FILES = {
    "Cargo.toml",
    "Dockerfile",
    "Gemfile",
    "go.mod",
    "package-lock.json",
    "package.json",
    "pnpm-lock.yaml",
    "poetry.lock",
    "pyproject.toml",
    "requirements.txt",
    "setup.cfg",
    "setup.py",
    "tsconfig.json",
    "vite.config.js",
    "vite.config.ts",
    "yarn.lock",
}
_REPOSITORY_GRAPH_ENTRYPOINT_FILES = {
    "app.py",
    "main.py",
    "manage.py",
    "server.py",
    "src/App.js",
    "src/App.jsx",
    "src/App.ts",
    "src/App.tsx",
    "src/index.js",
    "src/index.jsx",
    "src/index.ts",
    "src/index.tsx",
    "src/main.js",
    "src/main.jsx",
    "src/main.ts",
    "src/main.tsx",
}
_REPOSITORY_GRAPH_LANGUAGE_BY_EXTENSION = {
    ".go": "Go",
    ".js": "JavaScript",
    ".jsx": "JavaScript",
    ".mjs": "JavaScript",
    ".py": "Python",
    ".rs": "Rust",
    ".ts": "TypeScript",
    ".tsx": "TypeScript",
}
_REPOSITORY_GRAPH_NODE_TYPE_PRIORITY = {
    "entrypoint": 0,
    "workflow": 1,
    "manifest": 2,
    "module": 3,
    "test": 4,
    "file": 5,
}
_REPOSITORY_SEMANTIC_NODE_TYPE_PRIORITY = {
    "route": 0,
    "component": 1,
    "class": 2,
    "function": 3,
    "method": 4,
    "variable": 5,
}
_REPOSITORY_GRAPH_JS_SYMBOL_NAME_RE = r"[A-Za-z_$][\w$]*"
_REPOSITORY_GRAPH_ROUTE_METHODS = {"delete", "get", "patch", "post", "put"}
_REPOSITORY_GRAPH_JS_CALL_EXCLUDES = {
    "catch",
    "describe",
    "expect",
    "for",
    "function",
    "if",
    "import",
    "require",
    "return",
    "switch",
    "test",
    "while",
}
_REPOSITORY_SEMANTIC_LANGUAGE_TAG_BY_EXTENSION = {
    ".c": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".cs": "csharp",
    ".go": "go",
    ".java": "java",
    ".kt": "kotlin",
    ".php": "php",
    ".rb": "ruby",
    ".rs": "rust",
    ".swift": "swift",
}
_REPOSITORY_SEMANTIC_SYMBOL_NAME_RE = r"[A-Za-z_][A-Za-z0-9_]*[!?]?"
_REPOSITORY_SEMANTIC_CALL_EXCLUDES = {
    "catch",
    "class",
    "default",
    "describe",
    "do",
    "else",
    "expect",
    "for",
    "if",
    "import",
    "new",
    "return",
    "sizeof",
    "switch",
    "test",
    "throw",
    "typeof",
    "while",
}


def build_repository_graph_bundle(config: WorkerConfig, job: dict, checkout_dir: Path, preflight: dict) -> tuple[dict, dict]:
    files = repository_graph_files(checkout_dir)
    semantic_graph = build_repository_semantic_graph(config, job, files, checkout_dir)
    nodes = repository_graph_nodes(files, checkout_dir, preflight)
    edges = repository_graph_edges(files, checkout_dir, nodes)
    nodes, edges, truncated = cap_repository_graph(nodes, edges)
    summary = repository_graph_summary(job, nodes, edges, truncated, semantic_graph)
    payload = {
        "version": REPOSITORY_GRAPH_PROTOCOL_VERSION,
        "generatedAt": repository_graph_generated_at(job),
        "repo": repository_graph_text(job.get("repo")),
        "branch": repository_graph_text(job.get("branch")) or "main",
        "commit": repository_graph_text(job.get("commit")) or repository_graph_text(job.get("resolved_commit")) or "pending",
        "summary": summary["summary"],
        "stats": repository_graph_stats(files, nodes, edges, preflight, truncated),
        "nodes": nodes,
        "edges": edges,
        "architectureSummary": summary,
    }
    return payload, semantic_graph


def build_repository_graph(config: WorkerConfig, job: dict, checkout_dir: Path, preflight: dict) -> dict:
    repository_graph, _semantic_graph = build_repository_graph_bundle(config, job, checkout_dir, preflight)
    return repository_graph


def repository_graph_generated_at(job: dict) -> int:
    for key in ("generatedAt", "generated_at", "startedAt", "started_at"):
        try:
            value = int(job.get(key) or 0)
        except (TypeError, ValueError):
            value = 0
        if value > 0:
            return value
    return 0


def repository_graph_files(checkout_dir: Path) -> list[Path]:
    if not checkout_dir.is_dir():
        return []
    files: list[Path] = []
    stack = [checkout_dir]
    while stack and len(files) < REPOSITORY_GRAPH_MAX_FILES:
        directory = stack.pop()
        try:
            entries = sorted(os.scandir(directory), key=lambda entry: entry.name)
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    if entry.name not in REPOSITORY_GRAPH_IGNORED_DIRS:
                        stack.append(Path(entry.path))
                    continue
                if not entry.is_file(follow_symlinks=False):
                    continue
            except OSError:
                continue
            path = Path(entry.path)
            relative_path = repository_graph_relative_path(path, checkout_dir)
            if relative_path and repository_graph_include_file(relative_path):
                files.append(path)
                if len(files) >= REPOSITORY_GRAPH_MAX_FILES:
                    break
    return sorted(files, key=lambda path: repository_graph_relative_path(path, checkout_dir))


def repository_graph_include_file(relative_path: str) -> bool:
    parts = relative_path.split("/")
    if any(part in REPOSITORY_GRAPH_IGNORED_DIRS for part in parts):
        return False
    name = parts[-1]
    suffix = PurePosixPath(relative_path).suffix
    if name in _REPOSITORY_GRAPH_MANIFEST_FILES:
        return True
    if relative_path.startswith(".github/workflows/") and suffix in {".yml", ".yaml"}:
        return True
    return suffix in _REPOSITORY_GRAPH_SOURCE_EXTENSIONS or suffix in _REPOSITORY_GRAPH_CONFIG_EXTENSIONS


def repository_graph_nodes(files: list[Path], checkout_dir: Path, preflight: dict) -> list[dict]:
    del preflight
    file_paths = [repository_graph_relative_path(path, checkout_dir) for path in files]
    file_paths = [path for path in file_paths if path]
    nodes_by_id: dict[str, dict] = {}
    for file_path in file_paths:
        parts = file_path.split("/")[:-1]
        for depth in (1, 2):
            if len(parts) >= depth:
                module_path = "/".join(parts[:depth])
                node_id = f"dir:{module_path}"
                nodes_by_id[node_id] = {
                    "id": node_id,
                    "label": module_path,
                    "type": "module",
                    "path": module_path,
                    "importance": 0.7 if depth == 1 else 0.62,
                    "tags": ["module"],
                }
    for file_path in file_paths:
        node_type = repository_graph_file_node_type(file_path)
        tags = repository_graph_file_tags(file_path, node_type)
        node_id = f"file:{file_path}"
        nodes_by_id[node_id] = {
            "id": node_id,
            "label": PurePosixPath(file_path).name,
            "type": node_type,
            "path": file_path,
            "importance": repository_graph_node_importance(node_type),
            "tags": tags,
        }
    return sorted(nodes_by_id.values(), key=repository_graph_node_sort_key)


def repository_graph_edges(files: list[Path], checkout_dir: Path, nodes: list[dict]) -> list[dict]:
    node_ids = {str(node.get("id") or "") for node in nodes}
    file_paths = {repository_graph_relative_path(path, checkout_dir) for path in files}
    file_paths.discard("")
    edges_by_key: dict[tuple[str, str, str], dict] = {}
    for file_path in sorted(file_paths):
        parent_parts = file_path.split("/")[:-1]
        if parent_parts:
            module_path = "/".join(parent_parts[:2] if len(parent_parts) > 1 else parent_parts)
            repository_graph_add_edge(
                edges_by_key,
                f"dir:{module_path}",
                f"file:{file_path}",
                "contains",
                node_ids,
            )
    for path in sorted(files, key=lambda item: repository_graph_relative_path(item, checkout_dir)):
        source_path = repository_graph_relative_path(path, checkout_dir)
        suffix = PurePosixPath(source_path).suffix
        if suffix in _REPOSITORY_GRAPH_JS_EXTENSIONS:
            for target_path in repository_graph_js_import_targets(path, checkout_dir, source_path, file_paths):
                repository_graph_add_edge(
                    edges_by_key,
                    f"file:{source_path}",
                    f"file:{target_path}",
                    "imports",
                    node_ids,
                )
        elif suffix == ".py":
            for target_path in repository_graph_python_import_targets(path, checkout_dir, source_path, file_paths):
                repository_graph_add_edge(
                    edges_by_key,
                    f"file:{source_path}",
                    f"file:{target_path}",
                    "imports",
                    node_ids,
                )
    return sorted(edges_by_key.values(), key=lambda edge: (edge["type"], edge["source"], edge["target"]))


def repository_graph_add_edge(
    edges_by_key: dict[tuple[str, str, str], dict],
    source: str,
    target: str,
    edge_type: str,
    node_ids: set[str],
) -> None:
    if source not in node_ids or target not in node_ids or source == target:
        return
    key = (source, target, edge_type)
    if key in edges_by_key:
        edges_by_key[key]["weight"] = int(edges_by_key[key].get("weight") or 1) + 1
        return
    edge_id = f"{edge_type}:{source}->{target}"
    edges_by_key[key] = {
        "id": edge_id[:240],
        "source": source,
        "target": target,
        "type": edge_type,
        "weight": 1,
    }


def build_repository_semantic_graph(config: WorkerConfig | None, job: dict, files: list[Path], checkout_dir: Path) -> dict:
    static_graph = build_repository_static_semantic_graph(files, checkout_dir)
    if repository_semantic_agent_fallback_enabled(config, static_graph):
        agent_graph = build_repository_agent_semantic_graph(config, job, files, checkout_dir, static_graph)
        if agent_graph:
            return agent_graph
    return static_graph


def build_repository_static_semantic_graph(files: list[Path], checkout_dir: Path) -> dict:
    symbols: list[dict] = []
    pending_edges: list[dict] = []
    source_files = 0
    for path in sorted(files, key=lambda item: repository_graph_relative_path(item, checkout_dir)):
        relative_path = repository_graph_relative_path(path, checkout_dir)
        if not relative_path:
            continue
        suffix = PurePosixPath(relative_path).suffix
        if suffix == ".py":
            file_symbols, file_edges = repository_semantic_python_items(path, relative_path)
        elif suffix in _REPOSITORY_GRAPH_JS_EXTENSIONS:
            file_symbols, file_edges = repository_semantic_js_items(path, relative_path)
        elif suffix in _REPOSITORY_GRAPH_SOURCE_EXTENSIONS:
            file_symbols, file_edges = repository_semantic_generic_items(path, relative_path)
        else:
            continue
        source_files += 1
        symbols.extend(file_symbols)
        pending_edges.extend(file_edges)
    if not symbols:
        return {}

    symbols_by_id = {str(symbol.get("id") or ""): symbol for symbol in symbols if symbol.get("id")}
    node_ids = set(symbols_by_id)
    name_index = repository_semantic_name_index(symbols)
    edges_by_key: dict[tuple[str, str, str], dict] = {}
    for symbol in symbols:
        parent_id = str(symbol.get("_parent_id") or "")
        if parent_id:
            repository_semantic_add_edge(edges_by_key, parent_id, str(symbol.get("id") or ""), "defines", node_ids)
    for edge in pending_edges:
        source = str(edge.get("source") or "")
        target = str(edge.get("target") or "")
        if not target:
            target = repository_semantic_resolve_symbol(
                str(edge.get("targetName") or ""),
                name_index,
                source_path=str(edge.get("sourcePath") or ""),
            )
        repository_semantic_add_edge(edges_by_key, source, target, str(edge.get("type") or "calls"), node_ids)

    public_nodes_by_id: dict[str, dict] = {}
    for symbol in symbols:
        node = repository_semantic_public_node(symbol)
        if node and node["id"] not in public_nodes_by_id:
            public_nodes_by_id[node["id"]] = node
    public_nodes = list(public_nodes_by_id.values())
    public_edges = sorted(edges_by_key.values(), key=lambda edge: (edge["type"], edge["source"], edge["target"]))
    public_nodes, public_edges, truncated = cap_repository_semantic_graph(public_nodes, public_edges)
    summary, review_hints = repository_semantic_summary(public_nodes, public_edges, source_files, truncated)
    return {
        "version": REPOSITORY_SEMANTIC_GRAPH_PROTOCOL_VERSION,
        "summary": summary,
        "stats": repository_semantic_stats(public_nodes, public_edges, source_files, truncated, "static"),
        "nodes": public_nodes,
        "edges": public_edges,
        "reviewHints": review_hints,
    }


def repository_semantic_agent_fallback_enabled(config: WorkerConfig | None, static_graph: dict) -> bool:
    if not config or not getattr(config, "semantic_graph_agent_fallback", False):
        return False
    static_nodes = static_graph.get("nodes") if isinstance(static_graph, dict) else []
    static_count = len(static_nodes) if isinstance(static_nodes, list) else 0
    return static_count < int(getattr(config, "semantic_graph_agent_min_symbols", 8) or 0)


def build_repository_agent_semantic_graph(
    config: WorkerConfig | None,
    job: dict,
    files: list[Path],
    checkout_dir: Path,
    static_graph: dict,
) -> dict:
    if not config:
        return {}
    prompt = repository_semantic_agent_prompt(job, files, checkout_dir, static_graph)
    command = repository_semantic_agent_command(config, prompt)
    if not command:
        return {}
    try:
        completed = subprocess.run(
            command,
            cwd=str(checkout_dir),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=int(getattr(config, "semantic_graph_agent_timeout_seconds", 180) or 180),
        )
    except (OSError, subprocess.TimeoutExpired):
        return {}
    if completed.returncode != 0:
        return {}
    return repository_semantic_graph_from_agent_output(completed.stdout, files, checkout_dir, static_graph)


def repository_semantic_agent_command(config: WorkerConfig, prompt: str) -> list[str]:
    provider_chain = list(getattr(config, "provider_chain", []) or [getattr(config, "provider", "codex")])
    provider = provider_chain[0] if provider_chain else "codex"
    if provider == "opencode":
        command = [getattr(config, "opencode_command", "opencode"), "run"]
        opencode_model = getattr(config, "opencode_model", "")
        opencode_variant = getattr(config, "opencode_variant", "")
        if opencode_model:
            command.extend(["--model", opencode_model])
        if opencode_variant:
            command.extend(["--variant", opencode_variant])
        command.append(prompt)
        return command
    command = [getattr(config, "codex_command", "codex"), "exec"]
    if _CODEX_SKIP_GIT_REPO_CHECK_ARG:
        command.append(_CODEX_SKIP_GIT_REPO_CHECK_ARG)
    codex_model = getattr(config, "codex_model", "")
    codex_reasoning_effort = getattr(config, "codex_reasoning_effort", "")
    if codex_model:
        command.extend(["--model", codex_model])
    if codex_reasoning_effort:
        command.extend(["--reasoning-effort", codex_reasoning_effort])
    command.append(prompt)
    return command


def repository_semantic_agent_prompt(job: dict, files: list[Path], checkout_dir: Path, static_graph: dict) -> str:
    candidates = []
    for path in files[:80]:
        relative_path = repository_graph_relative_path(path, checkout_dir)
        if relative_path and PurePosixPath(relative_path).suffix in _REPOSITORY_GRAPH_SOURCE_EXTENSIONS:
            candidates.append(relative_path)
        if len(candidates) >= 60:
            break
    static_count = len(static_graph.get("nodes") or []) if isinstance(static_graph, dict) else 0
    return (
        "Build a semantic code graph for this repository. Return only JSON with top-level "
        "`version`, `summary`, `nodes`, `edges`, and optional `reviewHints`. "
        f"`version` must be `{REPOSITORY_SEMANTIC_GRAPH_PROTOCOL_VERSION}`. "
        "Nodes must use repository-relative `path`, `id`, `label`, `type`, optional `line`, "
        "`signature`, `importance`, and `tags`. Valid node types: class, component, function, "
        "method, route, variable. Edges must use `source`, `target`, `type`, optional `weight`; "
        "valid edge types: calls, defines, extends, handles, imports, implements, uses. "
        "Use stable ids prefixed with `symbol:` and no absolute paths. Prefer high-value public "
        "entrypoints, routes, handlers, classes, and cross-module calls. Cap at 120 nodes and 240 edges. "
        f"The static extractor found only {static_count} symbols, so infer missing semantics from code. "
        f"Repository: {repository_graph_text(job.get('repo')) or 'unknown'}; "
        f"branch: {repository_graph_text(job.get('branch')) or 'main'}; "
        f"commit: {repository_graph_text(job.get('commit')) or repository_graph_text(job.get('resolved_commit')) or 'pending'}. "
        f"Candidate source files: {', '.join(candidates) if candidates else 'not detected'}."
    )


def repository_semantic_graph_from_agent_output(output: str, files: list[Path], checkout_dir: Path, static_graph: dict) -> dict:
    parsed = repository_semantic_agent_json(output)
    if not isinstance(parsed, dict):
        return {}
    if repository_graph_text(parsed.get("version")) != REPOSITORY_SEMANTIC_GRAPH_PROTOCOL_VERSION:
        return {}
    file_paths = {repository_graph_relative_path(path, checkout_dir) for path in files}
    file_paths.discard("")
    raw_nodes = parsed.get("nodes") if isinstance(parsed.get("nodes"), list) else []
    nodes = []
    seen_node_ids = set()
    for item in raw_nodes:
        node = repository_semantic_agent_node(item, file_paths)
        node_id = node.get("id")
        if not node or node_id in seen_node_ids:
            continue
        seen_node_ids.add(node_id)
        nodes.append(node)
        if len(nodes) >= REPOSITORY_GRAPH_MAX_SEMANTIC_NODES:
            break
    if not nodes:
        return {}
    raw_edges = parsed.get("edges") if isinstance(parsed.get("edges"), list) else []
    edges = []
    seen_edge_ids = set()
    for item in raw_edges:
        edge = repository_semantic_agent_edge(item, seen_node_ids)
        edge_id = edge.get("id")
        if not edge or edge_id in seen_edge_ids:
            continue
        seen_edge_ids.add(edge_id)
        edges.append(edge)
        if len(edges) >= REPOSITORY_GRAPH_MAX_SEMANTIC_EDGES:
            break
    nodes, edges, truncated = cap_repository_semantic_graph(nodes, edges)
    summary = repository_graph_text(parsed.get("summary"), limit=500)
    if not summary:
        summary = f"Agent semantic code graph: {len(nodes)} symbols, {len(edges)} relationships."
    review_hints = [repository_graph_text(item, limit=160) for item in parsed.get("reviewHints") or []]
    review_hints = [item for item in review_hints if item][:6]
    if not review_hints:
        _summary, review_hints = repository_semantic_summary(nodes, edges, len(file_paths), truncated)
    return {
        "version": REPOSITORY_SEMANTIC_GRAPH_PROTOCOL_VERSION,
        "summary": summary,
        "stats": repository_semantic_stats(nodes, edges, len(file_paths), truncated, "agent_fallback"),
        "nodes": nodes,
        "edges": edges,
        "reviewHints": review_hints,
    }


def repository_semantic_agent_json(output: str) -> object:
    decoder = json.JSONDecoder()
    text = str(output or "").strip()
    candidates = [text]
    first = text.find("{")
    last = text.rfind("}")
    if first >= 0 and last > first:
        candidates.append(text[first : last + 1])
    for candidate in candidates:
        try:
            return decoder.decode(candidate)
        except json.JSONDecodeError:
            continue
    return None


def repository_semantic_agent_node(value: object, file_paths: set[str]) -> dict:
    if not isinstance(value, dict):
        return {}
    node_id = repository_graph_text(value.get("id"), limit=180)
    if not node_id or not re.match(r"^[A-Za-z0-9_.:/@-]{1,180}$", node_id):
        return {}
    node_type = repository_graph_text(value.get("type"), limit=40)
    if node_type not in _REPOSITORY_SEMANTIC_NODE_TYPE_PRIORITY:
        return {}
    path = repository_graph_normalize_posix_path(str(value.get("path") or ""))
    if not path or path not in file_paths:
        return {}
    label = repository_graph_text(value.get("label"), limit=80) or PurePosixPath(path).name
    try:
        line = max(1, int(value.get("line") or 1)) if not isinstance(value.get("line"), bool) else 1
    except (TypeError, ValueError):
        line = 1
    node = {
        "id": node_id,
        "label": label,
        "type": node_type,
        "path": path,
        "line": line,
        "importance": repository_semantic_agent_importance(value.get("importance"), node_type),
    }
    signature = repository_graph_text(value.get("signature"), limit=180)
    if signature:
        node["signature"] = signature
    tags = [repository_graph_text(tag, limit=40) for tag in value.get("tags") or []]
    tags = [tag for tag in tags if tag]
    if tags:
        node["tags"] = sorted(dict.fromkeys(tags))[:10]
    return node


def repository_semantic_agent_edge(value: object, node_ids: set[str]) -> dict:
    if not isinstance(value, dict):
        return {}
    source = repository_graph_text(value.get("source"), limit=180)
    target = repository_graph_text(value.get("target"), limit=180)
    edge_type = repository_graph_text(value.get("type"), limit=40)
    if source not in node_ids or target not in node_ids or source == target:
        return {}
    if edge_type not in {"calls", "defines", "extends", "handles", "imports", "implements", "uses"}:
        return {}
    edge_id = repository_graph_text(value.get("id"), limit=180)
    if not edge_id or not re.match(r"^[A-Za-z0-9_.:/@-]{1,180}$", edge_id):
        edge_id = repository_semantic_edge_id(edge_type, source, target)
    edge = {"id": edge_id, "source": source, "target": target, "type": edge_type}
    try:
        weight = int(value.get("weight") or 0)
    except (TypeError, ValueError):
        weight = 0
    if weight > 0:
        edge["weight"] = min(weight, 100)
    return edge


def repository_semantic_agent_importance(value: object, node_type: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return repository_semantic_node_importance(node_type, "")
    if not math.isfinite(number):
        return repository_semantic_node_importance(node_type, "")
    return round(max(0.0, min(1.0, number)), 3)


def repository_semantic_python_items(path: Path, relative_path: str) -> tuple[list[dict], list[dict]]:
    text = repository_graph_read_source_prefix(path)
    if not text.strip():
        return [], []
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return repository_semantic_python_regex_items(text, relative_path)

    symbols: list[dict] = []
    pending_edges: list[dict] = []

    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            class_symbol = repository_semantic_symbol(
                relative_path,
                node.name,
                node.name,
                "class",
                getattr(node, "lineno", 0),
                signature=f"class {node.name}",
                tags=["python"],
            )
            symbols.append(class_symbol)
            for base_name in repository_semantic_python_base_names(node):
                pending_edges.append(
                    {
                        "source": class_symbol["id"],
                        "sourcePath": relative_path,
                        "targetName": base_name,
                        "type": "extends",
                    }
                )
            for child in node.body:
                if isinstance(child, (ast.AsyncFunctionDef, ast.FunctionDef)):
                    method_symbol = repository_semantic_symbol(
                        relative_path,
                        child.name,
                        f"{node.name}.{child.name}",
                        "method",
                        getattr(child, "lineno", 0),
                        signature=repository_semantic_python_signature(child, f"{node.name}.{child.name}"),
                        tags=["python"],
                        parent_id=class_symbol["id"],
                    )
                    symbols.append(method_symbol)
                    for call_name in repository_semantic_python_call_names(child):
                        pending_edges.append(
                            {
                                "source": method_symbol["id"],
                                "sourcePath": relative_path,
                                "targetName": call_name,
                                "type": "calls",
                            }
                        )
            continue
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
            function_symbol = repository_semantic_symbol(
                relative_path,
                node.name,
                node.name,
                "function",
                getattr(node, "lineno", 0),
                signature=repository_semantic_python_signature(node, node.name),
                tags=["python"],
            )
            symbols.append(function_symbol)
            for route_label, route_qualname in repository_semantic_python_routes(node):
                route_symbol = repository_semantic_symbol(
                    relative_path,
                    route_label,
                    route_qualname,
                    "route",
                    getattr(node, "lineno", 0),
                    signature=route_label,
                    tags=["python", "route"],
                )
                symbols.append(route_symbol)
                pending_edges.append({"source": route_symbol["id"], "target": function_symbol["id"], "type": "handles"})
            for call_name in repository_semantic_python_call_names(node):
                pending_edges.append(
                    {
                        "source": function_symbol["id"],
                        "sourcePath": relative_path,
                        "targetName": call_name,
                        "type": "calls",
                    }
                )
    return symbols, pending_edges


def repository_semantic_python_regex_items(text: str, relative_path: str) -> tuple[list[dict], list[dict]]:
    symbols = []
    for line_number, line in enumerate(text.splitlines()[:500], start=1):
        class_match = re.match(r"^\s*class\s+([A-Za-z_]\w*)\b", line)
        if class_match:
            name = class_match.group(1)
            symbols.append(
                repository_semantic_symbol(
                    relative_path,
                    name,
                    name,
                    "class",
                    line_number,
                    signature=f"class {name}",
                    tags=["python"],
                )
            )
            continue
        function_match = re.match(r"^\s*(?:async\s+)?def\s+([A-Za-z_]\w*)\s*\(", line)
        if function_match:
            name = function_match.group(1)
            symbols.append(
                repository_semantic_symbol(
                    relative_path,
                    name,
                    name,
                    "function",
                    line_number,
                    signature=f"{name}(...)",
                    tags=["python"],
                )
            )
    return symbols, []


def repository_semantic_python_signature(node: ast.AsyncFunctionDef | ast.FunctionDef, qualname: str) -> str:
    args = []
    for arg in [*getattr(node.args, "posonlyargs", []), *node.args.args]:
        args.append(arg.arg)
    if node.args.vararg:
        args.append(f"*{node.args.vararg.arg}")
    for arg in node.args.kwonlyargs:
        args.append(arg.arg)
    if node.args.kwarg:
        args.append(f"**{node.args.kwarg.arg}")
    visible_args = args[:5]
    if len(args) > len(visible_args):
        visible_args.append("...")
    return f"{qualname}({', '.join(visible_args)})"


def repository_semantic_python_call_names(node: ast.AST) -> list[str]:
    names = []
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        call_name = repository_semantic_python_expr_name(child.func)
        if call_name:
            names.append(call_name)
    return sorted(dict.fromkeys(names))


def repository_semantic_python_base_names(node: ast.ClassDef) -> list[str]:
    names = []
    for base in node.bases:
        base_name = repository_semantic_python_expr_name(base)
        if base_name:
            names.append(base_name)
    return sorted(dict.fromkeys(names))


def repository_semantic_python_expr_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def repository_semantic_python_routes(node: ast.AsyncFunctionDef | ast.FunctionDef) -> list[tuple[str, str]]:
    routes = []
    for decorator in node.decorator_list:
        if not isinstance(decorator, ast.Call) or not isinstance(decorator.func, ast.Attribute):
            continue
        method = decorator.func.attr.lower()
        if method not in _REPOSITORY_GRAPH_ROUTE_METHODS or not decorator.args:
            continue
        first_arg = decorator.args[0]
        if isinstance(first_arg, ast.Constant) and isinstance(first_arg.value, str):
            route_path = first_arg.value.strip()[:80]
            if route_path:
                label = f"{method.upper()} {route_path}"
                routes.append((label, f"route.{method}.{route_path}"))
    return routes


def repository_semantic_js_items(path: Path, relative_path: str) -> tuple[list[dict], list[dict]]:
    text = repository_graph_read_source_prefix(path)
    if not text.strip():
        return [], []
    lines = text.splitlines()
    symbols: list[dict] = []
    pending_edges: list[dict] = []
    class_context: dict | None = None
    class_depth = 0

    for line_number, line in enumerate(lines[:600], start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("//"):
            continue
        if class_context and class_depth > 0:
            method_match = re.match(
                rf"^(?:async\s+)?({_REPOSITORY_GRAPH_JS_SYMBOL_NAME_RE})\s*\([^)]*\)\s*(?:\{{|=>)",
                stripped,
            )
            if method_match and method_match.group(1) not in {"constructor", "if", "for", "while", "switch"}:
                method_name = method_match.group(1)
                method_symbol = repository_semantic_symbol(
                    relative_path,
                    method_name,
                    f"{class_context['_qualname']}.{method_name}",
                    "method",
                    line_number,
                    signature=f"{class_context['_qualname']}.{method_name}(...)",
                    tags=[repository_semantic_js_language_tag(relative_path)],
                    parent_id=class_context["id"],
                )
                symbols.append(method_symbol)

        route_match = re.search(
            rf"\b(?:app|router|server)\.(get|post|put|patch|delete)\(\s*['\"]([^'\"]+)['\"]\s*(?:,\s*({_REPOSITORY_GRAPH_JS_SYMBOL_NAME_RE}))?",
            line,
        )
        if route_match:
            method = route_match.group(1).upper()
            route_path = route_match.group(2).strip()[:80]
            handler_name = route_match.group(3) or ""
            route_label = f"{method} {route_path}"
            route_symbol = repository_semantic_symbol(
                relative_path,
                route_label,
                f"route.{method.lower()}.{route_path}",
                "route",
                line_number,
                signature=route_label,
                tags=[repository_semantic_js_language_tag(relative_path), "route"],
            )
            symbols.append(route_symbol)
            if handler_name:
                pending_edges.append(
                    {
                        "source": route_symbol["id"],
                        "sourcePath": relative_path,
                        "targetName": handler_name,
                        "type": "handles",
                    }
                )

        class_match = re.search(
            rf"\bclass\s+({_REPOSITORY_GRAPH_JS_SYMBOL_NAME_RE})(?:\s+extends\s+({_REPOSITORY_GRAPH_JS_SYMBOL_NAME_RE}(?:\.{_REPOSITORY_GRAPH_JS_SYMBOL_NAME_RE})?))?",
            line,
        )
        if class_match:
            class_name = class_match.group(1)
            class_symbol = repository_semantic_symbol(
                relative_path,
                class_name,
                class_name,
                "class",
                line_number,
                signature=f"class {class_name}",
                tags=[repository_semantic_js_language_tag(relative_path)],
            )
            symbols.append(class_symbol)
            class_context = class_symbol
            class_depth = max(1, line.count("{") - line.count("}"))
            if class_match.group(2):
                pending_edges.append(
                    {
                        "source": class_symbol["id"],
                        "sourcePath": relative_path,
                        "targetName": class_match.group(2).split(".")[-1],
                        "type": "extends",
                    }
                )
            continue

        symbol = repository_semantic_js_symbol_from_line(stripped, relative_path, line_number)
        if symbol:
            symbols.append(symbol)

        if class_context and not class_match:
            class_depth += line.count("{") - line.count("}")
            if class_depth <= 0:
                class_context = None
                class_depth = 0

    callable_symbols = [
        symbol for symbol in symbols if symbol.get("type") in {"component", "function", "method"}
    ]
    callable_symbols.sort(key=lambda symbol: int(symbol.get("line") or 0))
    for index, symbol in enumerate(callable_symbols):
        start_line = int(symbol.get("line") or 1)
        next_line = (
            int(callable_symbols[index + 1].get("line") or start_line + 80)
            if index + 1 < len(callable_symbols)
            else start_line + 80
        )
        end_line = max(start_line, min(len(lines), next_line - 1))
        body = "\n".join(lines[start_line - 1 : end_line])
        for call_name in repository_semantic_js_call_names(body):
            if call_name != symbol.get("_name"):
                pending_edges.append(
                    {
                        "source": symbol["id"],
                        "sourcePath": relative_path,
                        "targetName": call_name,
                        "type": "calls",
                    }
                )
    return symbols, pending_edges


def repository_semantic_js_symbol_from_line(stripped: str, relative_path: str, line_number: int) -> dict:
    function_match = re.search(
        rf"\b(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+({_REPOSITORY_GRAPH_JS_SYMBOL_NAME_RE})\s*\(",
        stripped,
    )
    if function_match:
        name = function_match.group(1)
        return repository_semantic_symbol(
            relative_path,
            name,
            name,
            repository_semantic_js_function_type(name, relative_path),
            line_number,
            signature=f"{name}(...)",
            tags=[repository_semantic_js_language_tag(relative_path)],
        )
    arrow_match = re.search(
        rf"\b(?:export\s+)?(?:const|let|var)\s+({_REPOSITORY_GRAPH_JS_SYMBOL_NAME_RE})\s*=\s*(?:async\s*)?(?:\([^)]*\)|{_REPOSITORY_GRAPH_JS_SYMBOL_NAME_RE})\s*=>",
        stripped,
    )
    if arrow_match:
        name = arrow_match.group(1)
        return repository_semantic_symbol(
            relative_path,
            name,
            name,
            repository_semantic_js_function_type(name, relative_path),
            line_number,
            signature=f"{name}(...)",
            tags=[repository_semantic_js_language_tag(relative_path)],
        )
    function_value_match = re.search(
        rf"\b(?:export\s+)?(?:const|let|var)\s+({_REPOSITORY_GRAPH_JS_SYMBOL_NAME_RE})\s*=\s*(?:async\s+)?function\b",
        stripped,
    )
    if function_value_match:
        name = function_value_match.group(1)
        return repository_semantic_symbol(
            relative_path,
            name,
            name,
            repository_semantic_js_function_type(name, relative_path),
            line_number,
            signature=f"{name}(...)",
            tags=[repository_semantic_js_language_tag(relative_path)],
        )
    variable_match = re.search(
        rf"\bexport\s+(?:const|let|var)\s+({_REPOSITORY_GRAPH_JS_SYMBOL_NAME_RE})\s*=",
        stripped,
    )
    if variable_match:
        name = variable_match.group(1)
        return repository_semantic_symbol(
            relative_path,
            name,
            name,
            "variable",
            line_number,
            signature=name,
            tags=[repository_semantic_js_language_tag(relative_path), "exported"],
        )
    return {}


def repository_semantic_js_call_names(text: str) -> list[str]:
    names = []
    for match in re.finditer(rf"\b({_REPOSITORY_GRAPH_JS_SYMBOL_NAME_RE})\s*\(", text):
        name = match.group(1)
        if name not in _REPOSITORY_GRAPH_JS_CALL_EXCLUDES:
            names.append(name)
    return sorted(dict.fromkeys(names))


def repository_semantic_js_function_type(name: str, relative_path: str) -> str:
    if name[:1].isupper() and PurePosixPath(relative_path).suffix in {".jsx", ".tsx"}:
        return "component"
    return "function"


def repository_semantic_js_language_tag(relative_path: str) -> str:
    suffix = PurePosixPath(relative_path).suffix
    return "typescript" if suffix in {".ts", ".tsx"} else "javascript"


def repository_semantic_generic_items(path: Path, relative_path: str) -> tuple[list[dict], list[dict]]:
    text = repository_graph_read_source_prefix(path)
    if not text.strip():
        return [], []
    language_tag = repository_semantic_language_tag(relative_path)
    lines = text.splitlines()
    symbols: list[dict] = []
    pending_edges: list[dict] = []
    class_context: dict | None = None
    class_depth = 0
    pending_route: tuple[str, str, int] | None = None

    for line_number, line in enumerate(lines[:700], start=1):
        stripped = line.strip()
        if not stripped or repository_semantic_generic_comment_line(stripped):
            continue
        annotation_route = repository_semantic_generic_annotation_route(stripped, line_number)
        if annotation_route:
            pending_route = annotation_route

        inline_route = repository_semantic_generic_inline_route(stripped, relative_path, line_number, language_tag)
        if inline_route:
            route_symbol, handler_name = inline_route
            symbols.append(route_symbol)
            if handler_name:
                pending_edges.append(
                    {
                        "source": route_symbol["id"],
                        "sourcePath": relative_path,
                        "targetName": handler_name,
                        "type": "handles",
                    }
                )

        class_symbol, inherited_name = repository_semantic_generic_class_symbol(
            stripped,
            relative_path,
            line_number,
            language_tag,
        )
        if class_symbol:
            symbols.append(class_symbol)
            class_context = class_symbol
            class_depth = max(1, line.count("{") - line.count("}"))
            if inherited_name:
                pending_edges.append(
                    {
                        "source": class_symbol["id"],
                        "sourcePath": relative_path,
                        "targetName": inherited_name,
                        "type": "extends",
                    }
                )
            continue

        function_symbol = repository_semantic_generic_function_symbol(
            stripped,
            relative_path,
            line_number,
            language_tag,
            class_context=class_context if class_depth > 0 else None,
        )
        if function_symbol:
            symbols.append(function_symbol)
            if pending_route:
                route_label, route_qualname, route_line = pending_route
                route_symbol = repository_semantic_symbol(
                    relative_path,
                    route_label,
                    route_qualname,
                    "route",
                    route_line,
                    signature=route_label,
                    tags=[language_tag, "route"],
                )
                symbols.append(route_symbol)
                pending_edges.append({"source": route_symbol["id"], "target": function_symbol["id"], "type": "handles"})
                pending_route = None

        if class_context:
            class_depth += line.count("{") - line.count("}")
            if class_depth <= 0:
                class_context = None
                class_depth = 0

    callable_symbols = [
        symbol for symbol in symbols if symbol.get("type") in {"function", "method", "route"}
    ]
    callable_symbols.sort(key=lambda symbol: int(symbol.get("line") or 0))
    for index, symbol in enumerate(callable_symbols):
        if symbol.get("type") == "route":
            continue
        start_line = int(symbol.get("line") or 1)
        next_line = (
            int(callable_symbols[index + 1].get("line") or start_line + 80)
            if index + 1 < len(callable_symbols)
            else start_line + 80
        )
        end_line = max(start_line, min(len(lines), next_line - 1))
        body = "\n".join(lines[start_line - 1 : end_line])
        for call_name in repository_semantic_generic_call_names(body):
            if call_name != symbol.get("_name"):
                pending_edges.append(
                    {
                        "source": symbol["id"],
                        "sourcePath": relative_path,
                        "targetName": call_name,
                        "type": "calls",
                    }
                )
    return symbols, pending_edges


def repository_semantic_generic_comment_line(stripped: str) -> bool:
    return stripped.startswith(("//", "#", "/*", "*", "--"))


def repository_semantic_generic_annotation_route(stripped: str, line_number: int) -> tuple[str, str, int] | None:
    patterns = (
        r"^@(?:Get|Post|Put|Patch|Delete)Mapping\s*\(\s*(?:value\s*=\s*)?['\"]([^'\"]+)['\"]",
        r"^@\s*(GET|POST|PUT|PATCH|DELETE)\s*\(\s*['\"]([^'\"]+)['\"]",
        r"^\[(?:Http)?(Get|Post|Put|Patch|Delete)\s*\(\s*['\"]([^'\"]+)['\"]",
    )
    for pattern in patterns:
        match = re.search(pattern, stripped, flags=re.IGNORECASE)
        if not match:
            continue
        if len(match.groups()) == 1:
            method = re.sub(r"[^A-Za-z]", "", stripped.split("(", 1)[0]).replace("Mapping", "")
            route_path = match.group(1)
        else:
            method = match.group(1)
            route_path = match.group(2)
        method = method.upper().replace("HTTP", "") or "GET"
        route_path = str(route_path or "").strip()[:80]
        if route_path:
            label = f"{method} {route_path}"
            return label, f"route.{method.lower()}.{route_path}", line_number
    return None


def repository_semantic_generic_inline_route(
    stripped: str,
    relative_path: str,
    line_number: int,
    language_tag: str,
) -> tuple[dict, str] | None:
    methods = "|".join(sorted(_REPOSITORY_GRAPH_ROUTE_METHODS))
    match = re.search(
        rf"\b(?:app|router|routes|server)\.({methods})\s*\(\s*['\"]([^'\"]+)['\"]\s*,?\s*({_REPOSITORY_SEMANTIC_SYMBOL_NAME_RE})?",
        stripped,
        flags=re.IGNORECASE,
    )
    if not match:
        match = re.search(
            rf"\bRoute::({methods})\s*\(\s*['\"]([^'\"]+)['\"]\s*,?\s*({_REPOSITORY_SEMANTIC_SYMBOL_NAME_RE})?",
            stripped,
            flags=re.IGNORECASE,
        )
    if not match:
        return None
    method = match.group(1).upper()
    route_path = match.group(2).strip()[:80]
    handler_name = (match.group(3) or "").strip()
    if not route_path:
        return None
    label = f"{method} {route_path}"
    route_symbol = repository_semantic_symbol(
        relative_path,
        label,
        f"route.{method.lower()}.{route_path}",
        "route",
        line_number,
        signature=label,
        tags=[language_tag, "route"],
    )
    return route_symbol, handler_name


def repository_semantic_generic_class_symbol(
    stripped: str,
    relative_path: str,
    line_number: int,
    language_tag: str,
) -> tuple[dict, str]:
    go_type_match = re.search(
        rf"\btype\s+({_REPOSITORY_SEMANTIC_SYMBOL_NAME_RE})\s+(struct|interface)\b",
        stripped,
    )
    if go_type_match:
        name = go_type_match.group(1)
        kind = go_type_match.group(2)
        return (
            repository_semantic_symbol(
                relative_path,
                name,
                name,
                "class",
                line_number,
                signature=f"type {name} {kind}",
                tags=[language_tag, kind],
            ),
            "",
        )
    match = re.search(
        rf"\b(class|interface|struct|enum|trait|protocol|object|module)\s+({_REPOSITORY_SEMANTIC_SYMBOL_NAME_RE})(?:\s+(?:extends|implements|:)\s+({_REPOSITORY_SEMANTIC_SYMBOL_NAME_RE}))?",
        stripped,
    )
    if not match:
        return {}, ""
    kind = match.group(1)
    name = match.group(2)
    inherited = match.group(3) or ""
    node_type = "class" if kind in {"class", "interface", "object", "module", "protocol", "trait"} else "class"
    return (
        repository_semantic_symbol(
            relative_path,
            name,
            name,
            node_type,
            line_number,
            signature=f"{kind} {name}",
            tags=[language_tag, kind],
        ),
        inherited,
    )


def repository_semantic_generic_function_symbol(
    stripped: str,
    relative_path: str,
    line_number: int,
    language_tag: str,
    *,
    class_context: dict | None,
) -> dict:
    patterns = (
        (rf"\bfunc\s+\([^)]*\)\s*({_REPOSITORY_SEMANTIC_SYMBOL_NAME_RE})\s*\(", "method"),
        (rf"\bfunc\s+({_REPOSITORY_SEMANTIC_SYMBOL_NAME_RE})\s*\(", "function"),
        (rf"\b(?:pub\s+)?(?:async\s+)?fn\s+({_REPOSITORY_SEMANTIC_SYMBOL_NAME_RE})\s*\(", "function"),
        (rf"\b(?:fun|func|function)\s+({_REPOSITORY_SEMANTIC_SYMBOL_NAME_RE})\s*\(", "function"),
        (rf"^\s*def\s+(?:self\.)?({_REPOSITORY_SEMANTIC_SYMBOL_NAME_RE})\s*(?:\(|$)", "function"),
        (
            rf"\b(?:public|private|protected|internal|static|final|open|override|virtual|async|inline|extern|export|const|constexpr|mutating|throws|\s)+\s+[A-Za-z_][A-Za-z0-9_<>,:&*\[\]\s?]*\s+({_REPOSITORY_SEMANTIC_SYMBOL_NAME_RE})\s*\(",
            "function",
        ),
        (
            rf"^\s*[A-Za-z_][A-Za-z0-9_<>,:&*\[\]\s?]*\s+({_REPOSITORY_SEMANTIC_SYMBOL_NAME_RE})\s*\([^;]*\)\s*(?:\{{|=>|where\b)",
            "function",
        ),
    )
    for pattern, default_type in patterns:
        match = re.search(pattern, stripped)
        if not match:
            continue
        name = match.group(1)
        if repository_semantic_skip_symbol_name(name):
            continue
        parent_id = str(class_context.get("id") or "") if class_context else ""
        parent_name = str(class_context.get("_qualname") or "") if class_context else ""
        qualname = f"{parent_name}.{name}" if parent_name else name
        node_type = "method" if parent_id or default_type == "method" else "function"
        return repository_semantic_symbol(
            relative_path,
            name,
            qualname,
            node_type,
            line_number,
            signature=f"{qualname}(...)",
            tags=[language_tag],
            parent_id=parent_id,
        )
    return {}


def repository_semantic_generic_call_names(text: str) -> list[str]:
    names = []
    for match in re.finditer(rf"\b({_REPOSITORY_SEMANTIC_SYMBOL_NAME_RE})\s*\(", text):
        name = match.group(1)
        if not repository_semantic_skip_symbol_name(name):
            names.append(name)
    return sorted(dict.fromkeys(names))


def repository_semantic_skip_symbol_name(name: str) -> bool:
    normalized = str(name or "").strip().rstrip("!?").lower()
    return not normalized or normalized in _REPOSITORY_SEMANTIC_CALL_EXCLUDES


def repository_semantic_language_tag(relative_path: str) -> str:
    suffix = PurePosixPath(relative_path).suffix
    return _REPOSITORY_SEMANTIC_LANGUAGE_TAG_BY_EXTENSION.get(suffix, suffix.lstrip(".") or "code")


def repository_semantic_symbol(
    relative_path: str,
    name: str,
    qualname: str,
    node_type: str,
    line: int,
    *,
    signature: str = "",
    tags: list[str] | None = None,
    parent_id: str = "",
) -> dict:
    clean_name = repository_graph_text(name, limit=80)
    clean_qualname = repository_graph_text(qualname, limit=160) or clean_name
    node_id = repository_semantic_symbol_id(relative_path, clean_qualname)
    symbol_tags = list(tags or [])
    if repository_graph_is_entrypoint(relative_path):
        symbol_tags.append("entrypoint")
    return {
        "id": node_id,
        "label": clean_name or clean_qualname,
        "type": node_type,
        "path": relative_path,
        "line": max(1, int(line or 1)),
        "signature": repository_graph_text(signature, limit=180),
        "importance": repository_semantic_node_importance(node_type, relative_path),
        "tags": sorted(dict.fromkeys(tag for tag in symbol_tags if tag)),
        "_name": clean_name,
        "_qualname": clean_qualname,
        "_parent_id": parent_id,
    }


def repository_semantic_symbol_id(relative_path: str, qualname: str) -> str:
    safe_path = repository_semantic_safe_id_part(relative_path)[:90]
    safe_name = repository_semantic_safe_id_part(qualname)[:70] or "symbol"
    base = f"symbol:{safe_path}:{safe_name}"
    if len(base) <= 180:
        return base
    digest = hashlib.sha1(base.encode("utf-8")).hexdigest()[:10]
    return f"symbol:{safe_path[:70]}:{safe_name[:60]}:{digest}"[:180]


def repository_semantic_safe_id_part(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.:/@-]+", "_", str(value or "").replace("\\", "/")).strip("_")


def repository_semantic_node_importance(node_type: str, relative_path: str) -> float:
    base = {
        "route": 0.94,
        "component": 0.86,
        "class": 0.82,
        "function": 0.76,
        "method": 0.68,
        "variable": 0.44,
    }.get(node_type, 0.5)
    if repository_graph_is_entrypoint(relative_path):
        base += 0.08
    return round(min(base, 1.0), 3)


def repository_semantic_public_node(symbol: dict) -> dict:
    node = {
        "id": str(symbol.get("id") or ""),
        "label": repository_graph_text(symbol.get("label"), limit=80),
        "type": str(symbol.get("type") or "function"),
        "path": str(symbol.get("path") or ""),
        "line": max(1, int(symbol.get("line") or 1)),
        "importance": float(symbol.get("importance") or 0.5),
    }
    signature = repository_graph_text(symbol.get("signature"), limit=180)
    if signature:
        node["signature"] = signature
    tags = [repository_graph_text(tag, limit=40) for tag in symbol.get("tags") or []]
    tags = [tag for tag in tags if tag]
    if tags:
        node["tags"] = tags[:10]
    return node


def repository_semantic_name_index(symbols: list[dict]) -> dict[str, list[dict]]:
    index: dict[str, list[dict]] = {}
    for symbol in symbols:
        for key in (symbol.get("_name"), symbol.get("_qualname")):
            text = str(key or "")
            if text:
                index.setdefault(text, []).append(symbol)
    return index


def repository_semantic_resolve_symbol(target_name: str, index: dict[str, list[dict]], *, source_path: str) -> str:
    raw_name = str(target_name or "").strip()
    if not raw_name:
        return ""
    names = [raw_name]
    if "." in raw_name:
        names.append(raw_name.rsplit(".", 1)[-1])
    candidates = []
    for name in names:
        candidates.extend(index.get(name) or [])
        if candidates:
            break
    if not candidates:
        return ""
    sorted_candidates = sorted(
        candidates,
        key=lambda symbol: (
            0 if symbol.get("path") == source_path else 1,
            _REPOSITORY_SEMANTIC_NODE_TYPE_PRIORITY.get(str(symbol.get("type") or ""), 99),
            str(symbol.get("id") or ""),
        ),
    )
    return str(sorted_candidates[0].get("id") or "")


def repository_semantic_add_edge(
    edges_by_key: dict[tuple[str, str, str], dict],
    source: str,
    target: str,
    edge_type: str,
    node_ids: set[str],
) -> None:
    if source not in node_ids or target not in node_ids or source == target:
        return
    key = (source, target, edge_type)
    if key in edges_by_key:
        edges_by_key[key]["weight"] = int(edges_by_key[key].get("weight") or 1) + 1
        return
    edge_id = repository_semantic_edge_id(edge_type, source, target)
    edges_by_key[key] = {
        "id": edge_id,
        "source": source,
        "target": target,
        "type": edge_type,
        "weight": 1,
    }


def repository_semantic_edge_id(edge_type: str, source: str, target: str) -> str:
    base = f"{repository_semantic_safe_id_part(edge_type)}:{source}->{target}"
    if len(base) <= 180:
        return base
    digest = hashlib.sha1(base.encode("utf-8")).hexdigest()[:10]
    return f"{repository_semantic_safe_id_part(edge_type)}:{source[:70]}->{target[:70]}:{digest}"[:180]


def cap_repository_semantic_graph(nodes: list[dict], edges: list[dict]) -> tuple[list[dict], list[dict], bool]:
    sorted_nodes = sorted(nodes, key=repository_semantic_node_sort_key)
    capped_nodes = sorted_nodes[:REPOSITORY_GRAPH_MAX_SEMANTIC_NODES]
    kept_ids = {str(node.get("id") or "") for node in capped_nodes}
    filtered_edges = [
        edge
        for edge in sorted(edges, key=lambda item: (item.get("type", ""), item.get("source", ""), item.get("target", "")))
        if edge.get("source") in kept_ids and edge.get("target") in kept_ids
    ]
    capped_edges = filtered_edges[:REPOSITORY_GRAPH_MAX_SEMANTIC_EDGES]
    truncated = len(sorted_nodes) > len(capped_nodes) or len(edges) > len(capped_edges)
    return capped_nodes, capped_edges, truncated


def repository_semantic_node_sort_key(node: dict) -> tuple[int, str, str]:
    node_type = str(node.get("type") or "function")
    return (
        _REPOSITORY_SEMANTIC_NODE_TYPE_PRIORITY.get(node_type, 99),
        str(node.get("path") or ""),
        str(node.get("id") or ""),
    )


def repository_semantic_stats(
    nodes: list[dict],
    edges: list[dict],
    files: int,
    truncated: bool,
    source: str,
) -> dict:
    return {
        "files": files,
        "symbols": len(nodes),
        "relationships": len(edges),
        "routes": sum(1 for node in nodes if node.get("type") == "route"),
        "truncated": truncated,
        "source": source,
    }


def repository_semantic_summary(
    nodes: list[dict],
    edges: list[dict],
    files: int,
    truncated: bool,
) -> tuple[str, list[str]]:
    route_count = sum(1 for node in nodes if node.get("type") == "route")
    call_count = sum(1 for edge in edges if edge.get("type") == "calls")
    define_count = sum(1 for edge in edges if edge.get("type") == "defines")
    summary = f"Static semantic code graph: {len(nodes)} symbols, {len(edges)} relationships across {files} files."
    hints = []
    if route_count:
        hints.append(f"Routes detected: {route_count}.")
    if call_count:
        hints.append(f"Call relationships detected: {call_count}.")
    if define_count:
        hints.append(f"Class/member definitions detected: {define_count}.")
    if truncated:
        hints.append("Semantic graph was capped; prioritize routes, components, classes, and visible call edges.")
    return summary, hints[:6]


def repository_semantic_prompt_lines(semantic_graph: dict) -> list[str]:
    nodes = semantic_graph.get("nodes") if isinstance(semantic_graph.get("nodes"), list) else []
    edges = semantic_graph.get("edges") if isinstance(semantic_graph.get("edges"), list) else []
    node_by_id = {str(node.get("id") or ""): node for node in nodes if isinstance(node, dict)}
    top_nodes = sorted(nodes, key=repository_semantic_node_sort_key)[:10]
    lines = []
    if top_nodes:
        symbols = []
        for node in top_nodes:
            label = repository_graph_text(node.get("signature") or node.get("label"), limit=120)
            path = repository_graph_text(node.get("path"), limit=90)
            line = int(node.get("line") or 0)
            node_type = repository_graph_text(node.get("type"), limit=30)
            location = f"{path}:{line}" if line > 0 else path
            if label and location:
                symbols.append(f"{label} [{node_type} at {location}]")
        if symbols:
            lines.append(f"- Code symbols: {'; '.join(symbols[:8])}.")
    relationship_lines = []
    for edge in sorted(edges, key=lambda item: (item.get("type") != "handles", item.get("type") != "calls", item.get("type", ""))):
        edge_type = repository_graph_text(edge.get("type"), limit=30)
        if edge_type not in {"calls", "extends", "handles", "imports"}:
            continue
        source = node_by_id.get(str(edge.get("source") or "")) or {}
        target = node_by_id.get(str(edge.get("target") or "")) or {}
        source_label = repository_graph_text(source.get("label") or source.get("signature"), limit=60)
        target_label = repository_graph_text(target.get("label") or target.get("signature"), limit=60)
        if source_label and target_label:
            relationship_lines.append(f"{source_label} -{edge_type}-> {target_label}")
        if len(relationship_lines) >= 10:
            break
    if relationship_lines:
        lines.append(f"- Code relationships: {'; '.join(relationship_lines)}.")
    hints = semantic_graph.get("reviewHints") if isinstance(semantic_graph.get("reviewHints"), list) else []
    if hints:
        hint_text = " ".join(repository_graph_text(hint, limit=120) for hint in hints[:4] if repository_graph_text(hint))
        if hint_text:
            lines.append(f"- Semantic graph hints: {hint_text}")
    return lines


def cap_repository_graph(nodes: list[dict], edges: list[dict]) -> tuple[list[dict], list[dict], bool]:
    sorted_nodes = sorted(nodes, key=repository_graph_node_sort_key)
    capped_nodes = sorted_nodes[:REPOSITORY_GRAPH_MAX_NODES]
    kept_ids = {str(node.get("id") or "") for node in capped_nodes}
    filtered_edges = [
        edge
        for edge in sorted(edges, key=lambda item: (item.get("type", ""), item.get("source", ""), item.get("target", "")))
        if edge.get("source") in kept_ids and edge.get("target") in kept_ids
    ]
    capped_edges = filtered_edges[:REPOSITORY_GRAPH_MAX_EDGES]
    truncated = len(sorted_nodes) > len(capped_nodes) or len(edges) > len(capped_edges)
    return capped_nodes, capped_edges, truncated


def repository_graph_summary(
    job: dict,
    nodes: list[dict],
    edges: list[dict],
    truncated: bool,
    semantic_graph: dict | None = None,
) -> dict:
    entrypoints = [str(node.get("path") or "") for node in nodes if node.get("type") == "entrypoint"][:8]
    modules = [str(node.get("path") or "") for node in nodes if node.get("type") == "module"][:12]
    tests = [str(node.get("path") or "") for node in nodes if node.get("type") == "test"][:6]
    workflows = [str(node.get("path") or "") for node in nodes if node.get("type") == "workflow"][:4]
    imports_count = sum(1 for edge in edges if edge.get("type") == "imports")
    semantic_nodes_count = len(semantic_graph.get("nodes") or []) if isinstance(semantic_graph, dict) else 0
    semantic_edges_count = len(semantic_graph.get("edges") or []) if isinstance(semantic_graph, dict) else 0
    review_hints = []
    if entrypoints:
        review_hints.append(f"Start review from entrypoints: {', '.join(entrypoints[:4])}.")
    if tests:
        review_hints.append(f"Tests detected: {', '.join(tests[:4])}.")
    if workflows:
        review_hints.append(f"CI workflows detected: {', '.join(workflows[:3])}.")
    if imports_count:
        review_hints.append(f"Static local import graph has {imports_count} dependency edges.")
    if semantic_nodes_count:
        review_hints.append(
            f"Static semantic code graph has {semantic_nodes_count} symbols and {semantic_edges_count} relationships."
        )
    if truncated:
        review_hints.append("Graph was capped; prioritize high-importance nodes and visible import edges.")
    summary = (
        f"Static repository graph for {repository_graph_text(job.get('repo')) or 'repository'}: "
        f"{len(nodes)} nodes, {len(edges)} edges."
    )
    prompt_lines = [
        "Repository architecture:",
        f"- Repo: {repository_graph_text(job.get('repo')) or 'unknown'}; branch: {repository_graph_text(job.get('branch')) or 'main'}; commit: {repository_graph_text(job.get('commit')) or 'pending'}.",
        f"- Entrypoints: {', '.join(entrypoints) if entrypoints else 'not detected'}.",
        f"- Modules: {', '.join(modules[:8]) if modules else 'not detected'}.",
        f"- Tests: {', '.join(tests[:4]) if tests else 'not detected'}.",
        f"- Graph: {len(nodes)} nodes, {len(edges)} edges, {imports_count} local import edges.",
    ]
    if semantic_nodes_count:
        semantic_summary = repository_graph_text(semantic_graph.get("summary"), limit=400)
        prompt_lines.append(
            f"- Code semantics: {semantic_summary or f'{semantic_nodes_count} symbols, {semantic_edges_count} relationships'}."
        )
        prompt_lines.extend(repository_semantic_prompt_lines(semantic_graph))
    if review_hints:
        prompt_lines.append(f"- Review hints: {' '.join(review_hints)}")
    prompt_text = "\n".join(prompt_lines)
    if len(prompt_text) > REPOSITORY_GRAPH_MAX_PROMPT_CHARS:
        prompt_text = prompt_text[: REPOSITORY_GRAPH_MAX_PROMPT_CHARS - 3].rstrip() + "..."
    return {
        "summary": summary,
        "entrypoints": entrypoints,
        "modules": modules,
        "tests": tests,
        "workflows": workflows,
        "reviewHints": review_hints,
        "promptText": prompt_text,
    }


def repository_graph_stats(
    files: list[Path],
    nodes: list[dict],
    edges: list[dict],
    preflight: dict,
    truncated: bool,
) -> dict:
    return {
        "files": len(files),
        "nodes": len(nodes),
        "edges": len(edges),
        "languages": repository_graph_languages(files, preflight),
        "truncated": truncated,
    }


def repository_graph_languages(files: list[Path], preflight: dict) -> list[str]:
    languages = []
    raw_languages = preflight.get("languages") if isinstance(preflight, dict) else []
    if isinstance(raw_languages, list):
        for item in raw_languages:
            text = repository_graph_text(item, limit=80)
            if not text:
                continue
            languages.append(text)
            if "JavaScript" in text and "JavaScript" not in languages:
                languages.append("JavaScript")
            if "TypeScript" in text and "TypeScript" not in languages:
                languages.append("TypeScript")
    for path in files:
        label = _REPOSITORY_GRAPH_LANGUAGE_BY_EXTENSION.get(path.suffix)
        if label:
            languages.append(label)
    return sorted(dict.fromkeys(languages))[:8]


def repository_graph_file_node_type(relative_path: str) -> str:
    name = PurePosixPath(relative_path).name
    suffix = PurePosixPath(relative_path).suffix
    if repository_graph_is_entrypoint(relative_path):
        return "entrypoint"
    if relative_path.startswith(".github/workflows/") and suffix in {".yml", ".yaml"}:
        return "workflow"
    if repository_graph_is_test(relative_path):
        return "test"
    if name in _REPOSITORY_GRAPH_MANIFEST_FILES:
        return "manifest"
    return "file"


def repository_graph_file_tags(relative_path: str, node_type: str) -> list[str]:
    tags = [node_type]
    language = _REPOSITORY_GRAPH_LANGUAGE_BY_EXTENSION.get(PurePosixPath(relative_path).suffix)
    if language:
        tags.append(language.lower())
    if relative_path.startswith("src/") and "source" not in tags:
        tags.append("source")
    return sorted(dict.fromkeys(tags))


def repository_graph_node_importance(node_type: str) -> float:
    if node_type == "entrypoint":
        return 1.0
    if node_type == "workflow":
        return 0.82
    if node_type == "manifest":
        return 0.78
    if node_type == "test":
        return 0.66
    return 0.5


def repository_graph_node_sort_key(node: dict) -> tuple[int, str]:
    node_type = str(node.get("type") or "file")
    return (_REPOSITORY_GRAPH_NODE_TYPE_PRIORITY.get(node_type, 99), str(node.get("id") or ""))


def repository_graph_is_entrypoint(relative_path: str) -> bool:
    if relative_path in _REPOSITORY_GRAPH_ENTRYPOINT_FILES:
        return True
    name = PurePosixPath(relative_path).name
    return name in {"app.py", "main.py", "server.py"} and relative_path.count("/") <= 2


def repository_graph_is_test(relative_path: str) -> bool:
    name = PurePosixPath(relative_path).name
    return (
        relative_path.startswith("tests/")
        or relative_path.startswith("test/")
        or name.startswith("test_")
        or ".test." in name
        or ".spec." in name
    )


def repository_graph_js_import_targets(
    path: Path,
    checkout_dir: Path,
    source_path: str,
    file_paths: set[str],
) -> list[str]:
    text = repository_graph_read_source_prefix(path)
    targets = []
    for line in text.splitlines()[:300]:
        specs = []
        specs.extend(re.findall(r"\bfrom\s+['\"]([^'\"]+)['\"]", line))
        specs.extend(re.findall(r"^\s*import\s+['\"]([^'\"]+)['\"]", line))
        specs.extend(re.findall(r"\brequire\(\s*['\"]([^'\"]+)['\"]\s*\)", line))
        for specifier in specs:
            target = repository_graph_resolve_js_import(source_path, specifier, file_paths)
            if target:
                targets.append(target)
    return sorted(dict.fromkeys(targets))


def repository_graph_resolve_js_import(source_path: str, specifier: str, file_paths: set[str]) -> str:
    specifier = str(specifier or "").strip()
    if not specifier or not (specifier.startswith(".") or specifier.startswith("/")):
        return ""
    source_parent = PurePosixPath(source_path).parent
    raw_target = specifier.lstrip("/") if specifier.startswith("/") else f"{source_parent.as_posix()}/{specifier}"
    normalized = repository_graph_normalize_posix_path(raw_target)
    if not normalized:
        return ""
    candidates = [normalized]
    if not PurePosixPath(normalized).suffix:
        candidates.extend(f"{normalized}{extension}" for extension in _REPOSITORY_GRAPH_JS_EXTENSIONS)
        candidates.extend(f"{normalized}/index{extension}" for extension in _REPOSITORY_GRAPH_JS_EXTENSIONS)
    for candidate in candidates:
        if candidate in file_paths:
            return candidate
    return ""


def repository_graph_python_import_targets(
    path: Path,
    checkout_dir: Path,
    source_path: str,
    file_paths: set[str],
) -> list[str]:
    del checkout_dir, source_path
    text = repository_graph_read_source_prefix(path)
    targets = []
    for line in text.splitlines()[:300]:
        from_match = re.match(r"^\s*from\s+([A-Za-z_][\w.]*)\s+import\s+(.+)$", line)
        if from_match:
            package = from_match.group(1)
            imported_names = [
                part.strip().split(" ", 1)[0]
                for part in from_match.group(2).split(",")
                if part.strip() and part.strip() != "*"
            ]
            targets.extend(repository_graph_resolve_python_import(package, imported_names, file_paths))
            continue
        import_match = re.match(r"^\s*import\s+(.+)$", line)
        if import_match:
            for imported in import_match.group(1).split(","):
                module = imported.strip().split(" ", 1)[0]
                targets.extend(repository_graph_resolve_python_import(module, [], file_paths))
    return sorted(dict.fromkeys(targets))


def repository_graph_resolve_python_import(package: str, imported_names: list[str], file_paths: set[str]) -> list[str]:
    targets = []
    package_path = package.replace(".", "/")
    candidates = [f"{package_path}.py", f"{package_path}/__init__.py"]
    for name in imported_names:
        public_name = str(name or "").strip()
        if not public_name or not re.match(r"^[A-Za-z_]\w*$", public_name):
            continue
        candidates.extend([f"{package_path}/{public_name}.py", f"{package_path}/{public_name}/__init__.py"])
    for candidate in candidates:
        if candidate in file_paths:
            targets.append(candidate)
    return targets


def repository_graph_read_source_prefix(path: Path) -> str:
    try:
        with path.open("rb") as handle:
            data = handle.read(REPOSITORY_GRAPH_SOURCE_PREFIX_BYTES)
    except OSError:
        return ""
    return data.decode("utf-8", errors="ignore")


def repository_graph_relative_path(path: Path, checkout_dir: Path) -> str:
    try:
        relative = path.relative_to(checkout_dir)
    except ValueError:
        return ""
    return repository_graph_normalize_posix_path(relative.as_posix())


def repository_graph_normalize_posix_path(path: str) -> str:
    parts = []
    for part in str(path or "").replace("\\", "/").split("/"):
        if not part or part == ".":
            continue
        if part == "..":
            if not parts:
                return ""
            parts.pop()
            continue
        parts.append(part)
    return "/".join(parts)


def repository_graph_text(value: object, limit: int = 200) -> str:
    text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    if _WINDOWS_DRIVE_RE.match(text) or text.startswith("/"):
        text = PureWindowsPath(text).name or PurePosixPath(text).name
    return text[:limit]
