from __future__ import annotations

import os
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .config import CodexConfig
from .utils.process import ProcessResult, run_process

_CODEX_CLI_LOCK = threading.Lock()
_CODEX_CAPABILITIES_LOCK = threading.Lock()
_CODEX_CAPABILITIES_CACHE: dict[tuple[str, str, str], "CodexCliCapabilities"] = {}

_CODEX_KNOWN_OPTIONS = frozenset(
    {
        "--add-dir",
        "--ask-for-approval",
        "--cd",
        "--config",
        "--ephemeral",
        "--ignore-rules",
        "--ignore-user-config",
        "--json",
        "--model",
        "--output-last-message",
        "--output-schema",
        "--sandbox",
        "--skip-git-repo-check",
    }
)


@dataclass(frozen=True)
class CodexCliCapabilities:
    top_level_options: frozenset[str]
    exec_options: frozenset[str]
    lookup_error: str = ""


def base_env(checkout: Path, config: CodexConfig | None = None) -> dict[str, str]:
    del checkout
    env = os.environ.copy()
    if config is not None and config.env:
        env.update(config.env)
    return env


def codex_cli_capabilities(command: str, env: dict[str, str] | None = None) -> CodexCliCapabilities:
    key = (
        str(command or ""),
        str((env or {}).get("PATH") or ""),
        str((env or {}).get("CODEX_HOME") or ""),
    )
    with _CODEX_CAPABILITIES_LOCK:
        cached = _CODEX_CAPABILITIES_CACHE.get(key)
    if cached is not None:
        return cached

    top_options, top_error = _codex_help_options([command, "--help"], env)
    exec_options, exec_error = _codex_help_options([command, "exec", "--help"], env)
    error = "; ".join(item for item in [top_error, exec_error] if item)
    capabilities = CodexCliCapabilities(top_options, exec_options, error)
    with _CODEX_CAPABILITIES_LOCK:
        _CODEX_CAPABILITIES_CACHE[key] = capabilities
    return capabilities


def build_codex_exec_command(
    *,
    command: str,
    cd: Path,
    prompt: str,
    output_file: Path,
    sandbox: str,
    output_schema: Path | None = None,
    model: str = "",
    reasoning_effort: str = "",
    env: dict[str, str] | None = None,
    ask_for_approval: str = "never",
    skip_git_repo_check: bool = True,
    ignore_user_config: bool = False,
    ignore_rules: bool = False,
    ephemeral: bool = False,
    json_events: bool = True,
) -> tuple[list[str], str]:
    capabilities = codex_cli_capabilities(command, env)
    if capabilities.lookup_error:
        return _fallback_codex_exec_command(
            command=command,
            cd=cd,
            prompt=prompt,
            output_file=output_file,
            sandbox=sandbox,
            output_schema=output_schema,
            model=model,
            reasoning_effort=reasoning_effort,
            skip_git_repo_check=skip_git_repo_check,
            ignore_user_config=ignore_user_config,
            ignore_rules=ignore_rules,
            ephemeral=ephemeral,
            json_events=json_events,
        ), ""

    top_level_args: list[str] = []
    exec_args: list[str] = []
    missing_required: list[str] = []

    def add_supported(option: str, *values: str, required: bool = False) -> None:
        args = [option, *[value for value in values if value]]
        if option in capabilities.exec_options:
            exec_args.extend(args)
        elif option in capabilities.top_level_options:
            top_level_args.extend(args)
        elif required:
            missing_required.append(option)

    if ask_for_approval:
        add_supported("--ask-for-approval", ask_for_approval)
    add_supported("--cd", str(cd))
    if skip_git_repo_check:
        add_supported("--skip-git-repo-check")
    add_supported("--sandbox", sandbox, required=True)
    if ignore_user_config:
        add_supported("--ignore-user-config")
    if ignore_rules:
        add_supported("--ignore-rules")
    if ephemeral:
        add_supported("--ephemeral")
    if output_schema is not None:
        add_supported("--output-schema", str(output_schema))
    add_supported("--output-last-message", str(output_file), required=True)
    if json_events:
        add_supported("--json")
    if model:
        add_supported("--model", model)
    if reasoning_effort:
        add_supported("--config", f'model_reasoning_effort="{reasoning_effort}"')

    if missing_required:
        detail = ", ".join(sorted(set(missing_required)))
        return [command, "exec", prompt], f"codex exec does not support required option(s): {detail}"

    return [command, *top_level_args, "exec", *exec_args, "-"], ""


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
    cmd, command_error = build_codex_exec_command(
        command=config.command,
        cd=cd,
        prompt=prompt,
        output_schema=output_schema,
        output_file=output_file,
        sandbox=sandbox,
        model=config.model,
        reasoning_effort=config.reasoning_effort,
        env=env,
    )
    if command_error:
        return ProcessResult(
            command=cmd,
            cwd=str(cd),
            returncode=2,
            stdout="",
            stderr=command_error,
            duration_ms=0,
        )
    queued_at = time.monotonic()
    with _CODEX_CLI_LOCK:
        queue_wait_ms = int((time.monotonic() - queued_at) * 1000)
        return run_process(
            cmd,
            cwd=cd,
            env=env,
            timeout=timeout_seconds,
            queue_wait_ms=queue_wait_ms,
            stdin_text=prompt,
        )


def _codex_help_options(command: list[str], env: dict[str, str] | None) -> tuple[frozenset[str], str]:
    try:
        completed = subprocess.run(
            command,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
        )
    except FileNotFoundError:
        return frozenset(), f"{command[0]} not found"
    except subprocess.TimeoutExpired:
        return frozenset(), f"{' '.join(command)} timed out"
    except Exception as exc:
        return frozenset(), str(exc)
    output = "\n".join(part for part in [completed.stdout, completed.stderr] if part)
    options = frozenset(option for option in _CODEX_KNOWN_OPTIONS if option in output)
    if completed.returncode != 0:
        detail = output.strip().splitlines()[0] if output.strip() else f"exit {completed.returncode}"
        return options, f"{' '.join(command)} failed: {detail}"
    return options, ""


def _fallback_codex_exec_command(
    *,
    command: str,
    cd: Path,
    prompt: str,
    output_file: Path,
    sandbox: str,
    output_schema: Path | None,
    model: str,
    reasoning_effort: str,
    skip_git_repo_check: bool,
    ignore_user_config: bool,
    ignore_rules: bool,
    ephemeral: bool,
    json_events: bool,
) -> list[str]:
    cmd = [
        command,
        "exec",
        "--cd",
        str(cd),
    ]
    if skip_git_repo_check:
        cmd.append("--skip-git-repo-check")
    cmd.extend(["--sandbox", sandbox])
    if ignore_user_config:
        cmd.append("--ignore-user-config")
    if ignore_rules:
        cmd.append("--ignore-rules")
    if ephemeral:
        cmd.append("--ephemeral")
    if output_schema is not None:
        cmd.extend(["--output-schema", str(output_schema)])
    cmd.extend(["--output-last-message", str(output_file)])
    if json_events:
        cmd.append("--json")
    if model:
        cmd.extend(["--model", model])
    if reasoning_effort:
        cmd.extend(["--config", f'model_reasoning_effort="{reasoning_effort}"'])
    cmd.append("-")
    return cmd
