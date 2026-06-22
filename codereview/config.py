from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field, replace
from pathlib import Path


MODE_BUDGETS = {
    "fast": {"max_repro": 8},
    "standard": {"max_repro": 20},
    "deep": {"max_repro": 50},
}


@dataclass
class ContextConfig:
    enabled: bool = True
    timeout_seconds: int = 300


@dataclass
class CodexConfig:
    command: str = "codex"
    timeout_seconds: int = 600
    model: str = ""
    reasoning_effort: str = "high"
    env: dict[str, str] = field(default_factory=dict)


@dataclass
class FinderConfig:
    enabled: bool = True
    timeout_seconds: int = 600
    max_workers: int = 6
    turn_parallel: int = 2


@dataclass
class ReproConfig:
    enabled: bool = True
    timeout_seconds: int = 900
    max_workers: int = 2
    max_repro: int = 0
    require_red_green: bool = False


@dataclass
class ScoringConfig:
    min_score_for_repro: int = 8
    always_repro_severities: set[str] = field(default_factory=lambda: {"critical", "high"})


@dataclass
class ScanConfig:
    mode: str = "full-cached"
    include_untracked: bool = True
    fail_on_source_change: bool = True
    confirmed_only: bool = True


@dataclass
class ScopeConfig:
    exclude: list[str] = field(default_factory=lambda: [
        ".git/**",
        ".codereview/**",
        "node_modules/**",
        "vendor/**",
        "dist/**",
        "build/**",
        "coverage/**",
        "target/**",
    ])
    max_text_file_bytes: int = 1000000
    inventory_excluded_files: bool = True


@dataclass
class GraphConfig:
    schema_version: str = "3"
    prompt_version: str = "graph-v3"
    full_inventory: bool = True
    incremental: bool = True
    target_shards: int = 6
    max_shard_files: int = 25
    max_shard_bytes: int = 500000
    large_file_bytes: int = 120000
    double_map_high_risk: bool = True
    max_repair_rounds: int = 2
    use_sqlite_index: bool = True
    codex_census: bool = True
    codex_mappers: bool = True
    mapper_subagent_limit: int = 6
    map_parallel: int = 6
    graph_timeout_seconds: int = 480


@dataclass
class ReviewUnitConfig:
    require_baseline_for_every_unit: bool = True
    require_boundary_review: bool = True
    require_global_review: bool = True
    max_context_repair_rounds: int = 1
    max_candidates_per_finder: int = 3
    default_upstream_depth: int = 1
    default_downstream_depth: int = 1
    high_risk_upstream_depth: int = 2
    high_risk_downstream_depth: int = 2
    max_unit_nodes: int = 100
    max_unit_paths: int = 30
    max_context_chars: int = 80000


@dataclass
class CandidateConfig:
    max_per_finder_per_unit: int = 3
    max_total_for_verification: int = 60
    max_total_for_reproduction: int = 20
    require_expected_behavior_source: bool = True


@dataclass
class ReviewConfig:
    mode: str = "standard"
    scan: ScanConfig = field(default_factory=ScanConfig)
    scope: ScopeConfig = field(default_factory=ScopeConfig)
    graph: GraphConfig = field(default_factory=GraphConfig)
    units: ReviewUnitConfig = field(default_factory=ReviewUnitConfig)
    candidates: CandidateConfig = field(default_factory=CandidateConfig)
    context: ContextConfig = field(default_factory=ContextConfig)
    codex: CodexConfig = field(default_factory=CodexConfig)
    finders: FinderConfig = field(default_factory=FinderConfig)
    repro: ReproConfig = field(default_factory=ReproConfig)
    scoring: ScoringConfig = field(default_factory=ScoringConfig)

    @property
    def max_review_units(self) -> int:
        # Full-repository review profiles must not cap review unit coverage.
        # Keep this property for older callers; 0 means unlimited.
        return 0

    @property
    def max_repro(self) -> int:
        if self.repro.max_repro > 0:
            return self.repro.max_repro
        return MODE_BUDGETS.get(self.mode, MODE_BUDGETS["standard"])["max_repro"]


AUXILIARY_CODEX_REASONING_EFFORT = "medium"


def auxiliary_codex_config(config: ReviewConfig) -> CodexConfig:
    return replace(config.codex, reasoning_effort=AUXILIARY_CODEX_REASONING_EFFORT)


def _section(source: dict, key: str) -> dict:
    value = source.get(key)
    return value if isinstance(value, dict) else {}


def load_config(checkout: Path, mode: str = "", scan_mode: str = "") -> ReviewConfig:
    path = checkout / ".codereview" / "config.json"
    raw = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    if not isinstance(raw, dict):
        raw = {}
    review_units = _section(raw, "review")
    selected_mode = mode or str(raw.get("mode") or review_units.get("mode") or "standard")
    if selected_mode not in MODE_BUDGETS:
        raise ValueError("mode must be one of fast, standard, deep")
    graph = _section(raw, "graph")
    agents = _section(raw, "agents")
    scan = _section(raw, "scan")
    scope = _section(raw, "scope")
    units = review_units
    candidates = _section(raw, "candidates")
    context = _section(raw, "context")
    codex = _section(raw, "codex")
    codex_env = _string_map(codex.get("env"))
    finders = _section(raw, "finders")
    repro = _section(raw, "repro")
    scoring = _section(raw, "scoring")
    always_repro = scoring.get("always_repro_severities", ["critical", "high"])
    if isinstance(always_repro, str):
        always_repro = [always_repro]
    selected_scan_mode = str(scan_mode or scan.get("mode") or "full-cached")
    if selected_scan_mode not in {"full-cached", "full-strict"}:
        raise ValueError("scan mode must be one of full-cached, full-strict")
    codex_mappers_enabled = bool(graph.get("codex_mappers", True))
    mapper_subagent_limit = max(1, int(agents.get("graph_mapper_subagents") or graph.get("mapper_subagent_limit") or 6))
    return ReviewConfig(
        mode=selected_mode,
        scan=ScanConfig(
            mode=selected_scan_mode,
            include_untracked=bool(scan.get("include_untracked", True)),
            fail_on_source_change=bool(scan.get("fail_on_source_change", True)),
            confirmed_only=bool(scan.get("confirmed_only", True)),
        ),
        scope=ScopeConfig(
            exclude=[str(item) for item in scope.get("exclude", []) if str(item)] or ScopeConfig().exclude,
            max_text_file_bytes=max(1, int(scope.get("max_text_file_bytes") or 1000000)),
            inventory_excluded_files=bool(scope.get("inventory_excluded_files", True)),
        ),
        graph=GraphConfig(
            schema_version=str(graph.get("schema_version") or "3"),
            prompt_version=str(graph.get("prompt_version") or "graph-v3"),
            full_inventory=bool(graph.get("full_inventory", True)),
            incremental=selected_scan_mode != "full-strict" and bool(graph.get("incremental", True)),
            target_shards=max(1, int(graph.get("target_shards") or agents.get("graph_target_shards") or mapper_subagent_limit)),
            max_shard_files=max(1, int(graph.get("max_shard_files") or 25)),
            max_shard_bytes=max(1, int(graph.get("max_shard_bytes") or 500000)),
            large_file_bytes=max(1, int(graph.get("large_file_bytes") or graph.get("max_large_file_bytes") or 120000)),
            double_map_high_risk=bool(graph.get("double_map_high_risk", True)),
            max_repair_rounds=max(0, int(graph.get("max_repair_rounds") or 2)),
            use_sqlite_index=bool(graph.get("use_sqlite_index", True)),
            codex_census=bool(graph.get("codex_census", codex_mappers_enabled)),
            codex_mappers=codex_mappers_enabled,
            mapper_subagent_limit=mapper_subagent_limit,
            map_parallel=max(1, int(agents.get("graph_map_parallel") or graph.get("map_parallel") or 6)),
            graph_timeout_seconds=max(30, int(agents.get("graph_timeout_seconds") or graph.get("graph_timeout_seconds") or 480)),
        ),
        units=ReviewUnitConfig(
            require_baseline_for_every_unit=bool(units.get("require_baseline_for_every_unit", True)),
            require_boundary_review=bool(units.get("require_boundary_review", True)),
            require_global_review=bool(units.get("require_global_review", True)),
            max_context_repair_rounds=max(0, int(units.get("max_context_repair_rounds") or 1)),
            max_candidates_per_finder=max(1, int(units.get("max_candidates_per_finder") or candidates.get("max_per_finder_per_unit") or 3)),
            default_upstream_depth=max(0, int(units.get("default_upstream_depth") or 1)),
            default_downstream_depth=max(0, int(units.get("default_downstream_depth") or 1)),
            high_risk_upstream_depth=max(0, int(units.get("high_risk_upstream_depth") or 2)),
            high_risk_downstream_depth=max(0, int(units.get("high_risk_downstream_depth") or 2)),
            max_unit_nodes=max(1, int(units.get("max_unit_nodes") or 100)),
            max_unit_paths=max(1, int(units.get("max_unit_paths") or 30)),
            max_context_chars=max(1000, int(units.get("max_context_chars") or 80000)),
        ),
        candidates=CandidateConfig(
            max_per_finder_per_unit=max(1, int(candidates.get("max_per_finder_per_unit") or 3)),
            max_total_for_verification=max(1, int(candidates.get("max_total_for_verification") or 60)),
            max_total_for_reproduction=max(1, int(candidates.get("max_total_for_reproduction") or 20)),
            require_expected_behavior_source=bool(candidates.get("require_expected_behavior_source", True)),
        ),
        context=ContextConfig(
            enabled=bool(context.get("enabled", True)),
            timeout_seconds=int(context.get("timeout_seconds") or 300),
        ),
        codex=CodexConfig(
            command=resolve_command(str(codex.get("command") or "codex")),
            timeout_seconds=int(codex.get("timeout_seconds") or 600),
            model=str(codex.get("model") or ""),
            reasoning_effort=str(codex.get("reasoning_effort") or "high"),
            env=codex_env,
        ),
        finders=FinderConfig(
            enabled=bool(finders.get("enabled", True)),
            timeout_seconds=int(finders.get("timeout_seconds") or agents.get("finder_timeout_seconds") or 600),
            max_workers=max(1, int(finders.get("max_workers") or agents.get("finder_parallel") or 6)),
            turn_parallel=max(1, min(6, int(finders.get("turn_parallel") or agents.get("finder_turn_parallel") or 2))),
        ),
        repro=ReproConfig(
            enabled=bool(repro.get("enabled", True)),
            timeout_seconds=int(repro.get("timeout_seconds") or agents.get("repro_timeout_seconds") or 900),
            max_workers=max(1, int(repro.get("max_workers") or agents.get("repro_parallel") or 2)),
            max_repro=max(0, int(repro.get("max_repro") or candidates.get("max_total_for_reproduction") or 0)),
            require_red_green=bool(repro.get("require_red_green")),
        ),
        scoring=ScoringConfig(
            min_score_for_repro=int(scoring.get("min_score_for_repro") or 8),
            always_repro_severities={
                str(item).lower()
                for item in always_repro
                if str(item).strip()
            },
        ),
    )


def _string_map(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    items: dict[str, str] = {}
    for key, item in value.items():
        text_key = str(key or "").strip()
        if text_key and item is not None:
            items[text_key] = str(item)
    return items


def resolve_command(command: str) -> str:
    text = str(command or "").strip()
    if not text:
        return ""
    resolved = shutil.which(text)
    return resolved or text
