from __future__ import annotations

# Loaded by main.py; keep definitions in that module's globals for compatibility.

REPOSITORY_GRAPH_PROTOCOL_VERSION = "repository-graph/0.1"
REPOSITORY_GRAPH_MAX_NODES = 120
REPOSITORY_GRAPH_MAX_EDGES = 240
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


def build_repository_graph(config: WorkerConfig, job: dict, checkout_dir: Path, preflight: dict) -> dict:
    files = repository_graph_files(checkout_dir)
    nodes = repository_graph_nodes(files, checkout_dir, preflight)
    edges = repository_graph_edges(files, checkout_dir, nodes)
    nodes, edges, truncated = cap_repository_graph(nodes, edges)
    summary = repository_graph_summary(job, nodes, edges, truncated)
    return {
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


def repository_graph_summary(job: dict, nodes: list[dict], edges: list[dict], truncated: bool) -> dict:
    entrypoints = [str(node.get("path") or "") for node in nodes if node.get("type") == "entrypoint"][:8]
    modules = [str(node.get("path") or "") for node in nodes if node.get("type") == "module"][:12]
    tests = [str(node.get("path") or "") for node in nodes if node.get("type") == "test"][:6]
    workflows = [str(node.get("path") or "") for node in nodes if node.get("type") == "workflow"][:4]
    imports_count = sum(1 for edge in edges if edge.get("type") == "imports")
    review_hints = []
    if entrypoints:
        review_hints.append(f"Start review from entrypoints: {', '.join(entrypoints[:4])}.")
    if tests:
        review_hints.append(f"Tests detected: {', '.join(tests[:4])}.")
    if workflows:
        review_hints.append(f"CI workflows detected: {', '.join(workflows[:3])}.")
    if imports_count:
        review_hints.append(f"Static local import graph has {imports_count} dependency edges.")
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
