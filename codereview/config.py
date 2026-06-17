from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path


MODE_BUDGETS = {
    "fast": {"max_slices": 12, "max_repro": 8},
    "standard": {"max_slices": 30, "max_repro": 20},
    "deep": {"max_slices": 80, "max_repro": 50},
}


@dataclass
class CodeGraphConfig:
    command: str = "codegraph"
    timeout_seconds: int = 120
    optional_sync: bool = True
    reindex: bool = False


@dataclass
class CodexConfig:
    command: str = "codex"
    timeout_seconds: int = 600
    model: str = ""
    reasoning_effort: str = "high"


@dataclass
class FinderConfig:
    enabled: bool = True
    timeout_seconds: int = 600
    max_workers: int = 4


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
class ReviewConfig:
    mode: str = "standard"
    codegraph: CodeGraphConfig = field(default_factory=CodeGraphConfig)
    codex: CodexConfig = field(default_factory=CodexConfig)
    finders: FinderConfig = field(default_factory=FinderConfig)
    repro: ReproConfig = field(default_factory=ReproConfig)
    scoring: ScoringConfig = field(default_factory=ScoringConfig)

    @property
    def max_slices(self) -> int:
        return MODE_BUDGETS.get(self.mode, MODE_BUDGETS["standard"])["max_slices"]

    @property
    def max_repro(self) -> int:
        if self.repro.max_repro > 0:
            return self.repro.max_repro
        return MODE_BUDGETS.get(self.mode, MODE_BUDGETS["standard"])["max_repro"]


def _section(source: dict, key: str) -> dict:
    value = source.get(key)
    return value if isinstance(value, dict) else {}


def load_config(checkout: Path, mode: str = "") -> ReviewConfig:
    path = checkout / ".codereview" / "config.json"
    raw = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    if not isinstance(raw, dict):
        raw = {}
    selected_mode = mode or str(raw.get("mode") or "standard")
    if selected_mode not in MODE_BUDGETS:
        raise ValueError("mode must be one of fast, standard, deep")
    codegraph = _section(raw, "codegraph")
    codex = _section(raw, "codex")
    finders = _section(raw, "finders")
    repro = _section(raw, "repro")
    scoring = _section(raw, "scoring")
    always_repro = scoring.get("always_repro_severities", ["critical", "high"])
    if isinstance(always_repro, str):
        always_repro = [always_repro]
    return ReviewConfig(
        mode=selected_mode,
        codegraph=CodeGraphConfig(
            command=resolve_command(str(codegraph.get("command") or "codegraph")),
            timeout_seconds=int(codegraph.get("timeout_seconds") or 120),
            optional_sync=bool(codegraph.get("optional_sync", True)),
            reindex=bool(codegraph.get("reindex", False)),
        ),
        codex=CodexConfig(
            command=resolve_command(str(codex.get("command") or "codex")),
            timeout_seconds=int(codex.get("timeout_seconds") or 600),
            model=str(codex.get("model") or ""),
            reasoning_effort=str(codex.get("reasoning_effort") or "high"),
        ),
        finders=FinderConfig(
            enabled=bool(finders.get("enabled", True)),
            timeout_seconds=int(finders.get("timeout_seconds") or 600),
            max_workers=max(1, int(finders.get("max_workers") or 4)),
        ),
        repro=ReproConfig(
            enabled=bool(repro.get("enabled", True)),
            timeout_seconds=int(repro.get("timeout_seconds") or 900),
            max_workers=max(1, int(repro.get("max_workers") or 2)),
            max_repro=max(0, int(repro.get("max_repro") or 0)),
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


def resolve_command(command: str) -> str:
    text = str(command or "").strip()
    if not text:
        return ""
    resolved = shutil.which(text)
    return resolved or text
