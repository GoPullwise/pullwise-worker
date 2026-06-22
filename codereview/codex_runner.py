from __future__ import annotations

import os
from pathlib import Path

from .app_server_runner import run_codex_app_server_turn
from .config import CodexConfig
from .utils.process import ProcessResult


def base_env(checkout: Path, config: CodexConfig | None = None) -> dict[str, str]:
    del checkout
    if config is not None and config.env:
        return dict(config.env)
    return os.environ.copy()


def run_codex_turn(
    *,
    cd: Path,
    prompt: str,
    output_schema: Path,
    output_file: Path,
    sandbox: str,
    timeout_seconds: int,
    config: CodexConfig,
    env: dict[str, str] | None = None,
    events_file: Path | None = None,
) -> ProcessResult:
    return run_codex_app_server_turn(
        cd=cd,
        prompt=prompt,
        output_schema=output_schema,
        output_file=output_file,
        sandbox=sandbox,
        timeout_seconds=timeout_seconds,
        config=config,
        env=env,
        events_file=events_file,
    )
