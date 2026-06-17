from __future__ import annotations

import os
from pathlib import Path

from .config import CodexConfig
from .utils.process import ProcessResult, run_process


def base_env(checkout: Path, config: CodexConfig | None = None) -> dict[str, str]:
    del checkout
    env = os.environ.copy()
    if config is not None and config.env:
        env.update(config.env)
    env.pop("CODEGRAPH_DIR", None)
    return env


def run_codex_exec(
    *,
    cd: Path,
    prompt: str,
    output_schema: Path,
    output_file: Path,
    sandbox: str,
    timeout_seconds: int,
    config: CodexConfig,
    env: dict[str, str] | None = None,
) -> ProcessResult:
    cmd = [
        config.command,
        "exec",
        "--cd",
        str(cd),
        "--skip-git-repo-check",
        "--sandbox",
        sandbox,
        "--ask-for-approval",
        "never",
        "--output-schema",
        str(output_schema),
        "--output-last-message",
        str(output_file),
        "--json",
    ]
    if config.model:
        cmd.extend(["--model", config.model])
    if config.reasoning_effort:
        cmd.extend(["--config", f'model_reasoning_effort="{config.reasoning_effort}"'])
    cmd.append(prompt)
    return run_process(cmd, cwd=cd, env=env, timeout=timeout_seconds)
