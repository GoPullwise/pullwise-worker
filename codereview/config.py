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


@dataclass
class ReviewConfig:
    mode: str = "standard"
    codegraph: CodeGraphConfig = field(default_factory=CodeGraphConfig)
    codex: CodexConfig = field(default_factory=CodexConfig)
    finders: FinderConfig = field(default_factory=FinderConfig)
    repro: ReproConfig = field(default_factory=ReproConfig)

    @property
    def max_slices(self) -> int:
        return MODE_BUDGETS.get(self.mode, MODE_BUDGETS["standard"])["max_slices"]

    @property
    def max_repro(self) -> int:
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
        ),
    )


def resolve_command(command: str) -> str:
    text = str(command or "").strip()
    if not text:
        return ""
    resolved = shutil.which(text)
    return resolved or text
