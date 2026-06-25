from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import sys
import threading
import time
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Iterable

from .app_server_runner import get_codex_app_server_client
from .codex_runner import run_codex_turn
from .config import CodexConfig, ReviewConfig, load_config
from .inventory.git_inventory import analyzable_files, build_git_inventory
from .repro.worker_dir import create_worker_dir
from .snapshot import capture_source_state, create_immutable_snapshot, source_state_changed, source_state_from_inventory
from .utils.jsonl import read_json, write_json, write_jsonl, write_text
from .utils.paths import ensure_dir, is_within, safe_relative_path
from .utils.process import ProcessCancelled, process_cancel_requested, raise_if_cancelled_callback_exception


ProgressCallback = Callable[[dict], None]
ENGINE_VERSION = "simple-full-repository/1"
MAX_EVENT_BYTES = 8 * 1024 * 1024
MAX_EVENT_LINE_CHARS = 2 * 1024 * 1024
MAX_COMMAND_OUTPUT_CHARS = 32_000
MAX_DEBUG_REASON_CHARS = 800
MAX_INTERNAL_DIAGNOSTIC_ITEMS = 50
MAX_DEBUG_REASON_BUCKETS = 12
MAX_REPORT_TEXT_CHARS = 4_000
_AUTH_EXPIRED_MARKERS = (
    "failed to refresh token",
    "access token could not be refreshed",
    "refresh token was already used",
    "please log out and sign in again",
)
_AUTH_REQUIRED_MARKERS = (
    "401 unauthorized",
    "not authenticated",
    "authentication required",
    "login required",
)
_AUTH_MARKERS = _AUTH_EXPIRED_MARKERS + _AUTH_REQUIRED_MARKERS
_QUOTA_MARKERS = (
    "429",
    "rate limit",
    "rate_limit",
    "quota exceeded",
    "quota exhausted",
    "usage limit",
    "credits exhausted",
)
_SEVERITY_RANK = {"critical": 5, "high": 4, "medium": 3, "low": 2, "info": 1}
_NON_REPRO_COMMANDS = {
    "cat",
    "echo",
    "false",
    "find",
    "grep",
    "head",
    "ls",
    "pwd",
    "printf",
    "rg",
    "sed",
    "tail",
    "true",
}
_SHELL_OPERATORS = {";", "&&", "||", "|", "&"}
_INFO_ONLY_ARGUMENTS = {"--help", "-h", "--version", "-v", "version", "help"}
_DIRECT_RUNTIME_EXECUTABLES = {
    "bun",
    "deno",
    "java",
    "node",
    "php",
    "phpunit",
    "pytest",
    "python",
    "python3",
    "ruby",
    "rspec",
}
_SCRIPT_FILE_SUFFIXES = {
    ".bash",
    ".cjs",
    ".js",
    ".mjs",
    ".php",
    ".py",
    ".rb",
    ".sh",
    ".ts",
}
_INLINE_CODE_FLAGS = {
    "node": {"-e", "--eval", "-p", "--print"},
    "php": {"-r"},
    "python": {"-c"},
    "python3": {"-c"},
    "ruby": {"-e"},
}
_INLINE_SHELLS = {"bash", "sh", "zsh"}
_MAX_HARNESS_BYTES = 256 * 1024
_AUTO_PARALLELISM_SENTINEL = 0
_SIMPLE_PARALLEL_MAX = 4


DISCOVERY_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["reviewed_unit_ids", "candidates"],
    "properties": {
        "reviewed_unit_ids": {
            "type": "array",
            "items": {"type": "string"},
        },
        "candidates": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "unit_id",
                    "severity",
                    "category",
                    "title",
                    "claim",
                    "trigger_condition",
                    "expected_behavior",
                    "expected_behavior_source",
                    "actual_behavior_hypothesis",
                    "impact",
                    "evidence",
                    "path_summary",
                    "reproduction_idea",
                ],
                "properties": {
                    "unit_id": {"type": "string"},
                    "severity": {
                        "type": "string",
                        "enum": ["critical", "high", "medium", "low", "info"],
                    },
                    "category": {"type": "string"},
                    "title": {"type": "string"},
                    "claim": {"type": "string"},
                    "trigger_condition": {"type": "string"},
                    "expected_behavior": {"type": "string"},
                    "expected_behavior_source": {"type": "string"},
                    "actual_behavior_hypothesis": {"type": "string"},
                    "impact": {"type": "string"},
                    "evidence": {
                        "type": "array",
                        "minItems": 1,
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["file", "line_start", "line_end", "why_it_matters"],
                            "properties": {
                                "file": {"type": "string"},
                                "line_start": {"type": "integer", "minimum": 1},
                                "line_end": {"type": "integer", "minimum": 1},
                                "why_it_matters": {"type": "string"},
                            },
                        },
                    },
                    "path_summary": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "reproduction_idea": {"type": "string"},
                },
            },
        },
    },
}


VERIFICATION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "candidate_id",
        "status",
        "safe_to_show_user",
        "reason",
        "expected_behavior",
        "observed_behavior",
        "reproduction_command",
        "output_marker",
        "exercised_files",
        "skeptic_agreed",
        "independent_check",
        "limitations",
    ],
    "properties": {
        "candidate_id": {"type": "string"},
        "status": {"type": "string", "enum": ["confirmed", "rejected", "blocked"]},
        "safe_to_show_user": {"type": "boolean"},
        "reason": {"type": "string"},
        "expected_behavior": {"type": "string"},
        "observed_behavior": {"type": "string"},
        "reproduction_command": {"type": "string"},
        "output_marker": {"type": "string"},
        "exercised_files": {"type": "array", "items": {"type": "string"}},
        "skeptic_agreed": {"type": "boolean"},
        "independent_check": {"type": "string"},
        "limitations": {"type": "array", "items": {"type": "string"}},
    },
}


@dataclass(frozen=True)
class ReviewUnit:
    unit_id: str
    area: str
    files: tuple[str, ...]
    size_bytes: int
    line_count: int

    def to_dict(self, inventory_by_path: dict[str, dict]) -> dict:
        return {
            "unit_id": self.unit_id,
            "area": self.area,
            "size_bytes": self.size_bytes,
            "line_count": self.line_count,
            "files": [
                {
                    "path": path,
                    "size_bytes": int(inventory_by_path[path].get("size_bytes") or 0),
                    "line_count": int(inventory_by_path[path].get("line_count") or 0),
                    "content_hash": str(inventory_by_path[path].get("content_hash") or ""),
                }
                for path in self.files
            ],
        }


@dataclass(frozen=True)
class DiscoveryBatch:
    batch_id: str
    units: tuple[ReviewUnit, ...]
    agent_groups: tuple[tuple[str, ...], ...]


@dataclass(frozen=True)
class SimpleSettings:
    discovery_turns: int
    max_discovery_turns: int
    discovery_parallel: int
    verification_parallel: int
    subagents_per_turn: int
    max_candidates: int
    max_candidates_per_unit: int
    max_unit_files: int
    max_unit_bytes: int
    max_batch_files: int
    max_batch_bytes: int
    discovery_timeout_seconds: int
    verification_timeout_seconds: int
    scan_deadline_seconds: int
    output_language: str


@dataclass(frozen=True)
class CommandEvidence:
    command: str
    cwd: str
    exit_code: int
    output: str
    status: str


class CandidateRejected(ValueError):
    pass


class VerificationRejected(ValueError):
    pass


def run_review(checkout: Path, mode: str = "", scan_mode: str = "", progress: ProgressCallback | None = None) -> Path:
    checkout = checkout.resolve(strict=False)
    config = load_config(checkout, mode=mode, scan_mode=scan_mode)
    raw_config = read_json(checkout / ".codereview" / "config.json", default={})
    raw_config = raw_config if isinstance(raw_config, dict) else {}
    settings = load_simple_settings(raw_config, config)
    deadline_at = time.monotonic() + settings.scan_deadline_seconds if settings.scan_deadline_seconds > 0 else 0.0
    run_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    run = checkout / ".codereview" / "runs" / run_id
    ensure_dir(run)
    write_json(
        run / "meta.json",
        {
            "run_id": run_id,
            "engine": ENGINE_VERSION,
            "mode": config.mode,
            "scan_mode": config.scan.mode,
            "scope": "full-repository",
        },
    )
    _emit(progress, "setup", "Preparing simple full-repository review", run_id=run_id)

    _emit(progress, "inventory", "Building full repository inventory", run_id=run_id)
    inventory = build_git_inventory(checkout, include_untracked=config.scan.include_untracked, max_text_file_bytes=config.scope.max_text_file_bytes)
    inventory_files = analyzable_files(inventory)
    if not inventory_files:
        return _write_reports(
            run,
            mode=config.mode,
            scan_mode=config.scan.mode,
            inventory=inventory,
            units=[],
            discovery_results=[],
            raw_candidates=[],
            valid_candidates=[],
            selected_candidates=[],
            confirmed=[],
            rejected=[],
            account={},
            progress=progress,
        )
    source_state_before = source_state_from_inventory(inventory)
    write_json(run / "inventory.json", inventory)
    write_json(run / "source-state-before.json", source_state_before)

    _emit(progress, "snapshot", "Creating immutable full-repository snapshot", run_id=run_id)
    snapshot_manifest = create_immutable_snapshot(checkout, inventory, run)
    snapshot_repo = Path(str(snapshot_manifest["snapshot_repo"]))
    snapshot_state_before = source_state_before
    write_json(run / "snapshot.json", snapshot_manifest)
    write_json(run / "snapshot-source-state-before.json", snapshot_state_before)
    account = codex_account_preflight(config.codex, snapshot_repo)
    write_json(run / "codex-account.json", account)
    settings = tune_simple_parallelism(settings, inventory, account)
    inventory_by_path = {
        str(item.get("path") or ""): item
        for item in inventory_files
        if isinstance(item, dict) and str(item.get("path") or "")
    }
    units = plan_review_units(
        inventory_files,
        max_files=settings.max_unit_files,
        max_bytes=settings.max_unit_bytes,
    )
    validate_unit_coverage(units, set(inventory_by_path))
    write_jsonl(run / "units.jsonl", [unit.to_dict(inventory_by_path) for unit in units])
    batches = plan_discovery_batches(
        units,
        target_turns=settings.discovery_turns,
        max_turns=settings.max_discovery_turns,
        max_batch_files=settings.max_batch_files,
        max_batch_bytes=settings.max_batch_bytes,
        subagents_per_turn=settings.subagents_per_turn,
    )
    write_json(
        run / "coverage-planned.json",
        {
            "scope": "full-repository",
            "files": len(inventory_by_path),
            "units": len(units),
            "discovery_turns": len(batches),
            "complete": True,
        },
    )

    _emit(progress, "finder", f"Discovery 0/{len(batches)}", current=0, total=len(batches), run_id=run_id)
    discovery_results = _run_discovery(
        snapshot_repo,
        run,
        batches,
        units,
        inventory_by_path,
        config.codex,
        settings,
        progress,
        run_id,
    )
    raw_candidates = [
        item
        for result in discovery_results
        for item in (result.get("candidates") if isinstance(result.get("candidates"), list) else [])
        if isinstance(item, dict)
    ]
    write_jsonl(run / "candidates" / "raw.jsonl", raw_candidates)

    unit_by_id = {unit.unit_id: unit for unit in units}
    valid_candidates: list[dict] = []
    rejected: list[dict] = []
    for raw_candidate in raw_candidates:
        try:
            valid_candidates.append(normalize_candidate(raw_candidate, unit_by_id, inventory_by_path))
        except CandidateRejected as exc:
            rejected.append(_rejected_record("discovery", raw_candidate, str(exc)))
    unit_limited, unit_budget_rejections = limit_candidates_per_unit(
        valid_candidates,
        settings.max_candidates_per_unit,
    )
    rejected.extend(unit_budget_rejections)
    deduped, duplicate_rejections = dedupe_candidates(unit_limited)
    rejected.extend(duplicate_rejections)
    selected_candidates, budget_rejections = select_candidates(deduped, settings.max_candidates)
    rejected.extend(budget_rejections)
    write_jsonl(run / "candidates" / "valid.jsonl", valid_candidates)
    write_jsonl(run / "candidates" / "selected.jsonl", selected_candidates)
    _emit(
        progress,
        "candidates",
        f"Candidates: {len(raw_candidates)} raw, {len(selected_candidates)} selected for runtime verification",
        run_id=run_id,
        extra={"candidateCount": len(selected_candidates)},
    )

    stop_event = threading.Event()
    _emit(
        progress,
        "verification",
        f"Runtime verification 0/{len(selected_candidates)}",
        current=0,
        total=len(selected_candidates),
        run_id=run_id,
    )
    verification_results = _run_verifications(
        snapshot_repo,
        run,
        selected_candidates,
        config.codex,
        settings,
        stop_event,
        progress,
        run_id,
        deadline_at,
    )
    confirmed: list[dict] = []
    for result in verification_results:
        if result.get("confirmed") and isinstance(result.get("item"), dict):
            confirmed.append(result["item"])
        else:
            rejected.append(
                {
                    "stage": "verification",
                    "candidate_id": str(result.get("candidate_id") or ""),
                    "reason": _clean_text(result.get("reason"), MAX_DEBUG_REASON_CHARS),
                }
            )

    snapshot_state_after = capture_source_state(snapshot_repo, include_untracked=True, max_text_file_bytes=config.scope.max_text_file_bytes)
    write_json(run / "snapshot-source-state-after.json", snapshot_state_after)
    if source_state_changed(snapshot_state_before, snapshot_state_after):
        raise RuntimeError("immutable snapshot changed during simple full-repository review")
    source_state_after = capture_source_state(checkout, include_untracked=config.scan.include_untracked, max_text_file_bytes=config.scope.max_text_file_bytes)
    write_json(run / "source-state-after.json", source_state_after)
    if config.scan.fail_on_source_change and source_state_changed(source_state_before, source_state_after):
        raise RuntimeError("source checkout changed during simple full-repository review")

    return _write_reports(
        run,
        mode=config.mode,
        scan_mode=config.scan.mode,
        inventory=inventory,
        units=units,
        discovery_results=discovery_results,
        raw_candidates=raw_candidates,
        valid_candidates=valid_candidates,
        selected_candidates=selected_candidates,
        confirmed=confirmed,
        rejected=rejected,
        account=account,
        progress=progress,
    )


def load_simple_settings(raw_config: dict, config: ReviewConfig) -> SimpleSettings:
    simple = raw_config.get("simple") if isinstance(raw_config.get("simple"), dict) else {}
    mode_defaults = {
        "fast": {"turns": 2, "candidates": 8},
        "standard": {"turns": 3, "candidates": 10},
        "deep": {"turns": 4, "candidates": 20},
    }.get(config.mode, {"turns": 3, "candidates": 10})
    return SimpleSettings(
        discovery_turns=_bounded_int(simple.get("discovery_turns"), mode_defaults["turns"], 1, 16),
        max_discovery_turns=_bounded_int(simple.get("max_discovery_turns"), 48, 1, 64),
        discovery_parallel=_bounded_int(simple.get("discovery_parallel"), _AUTO_PARALLELISM_SENTINEL, 0, _SIMPLE_PARALLEL_MAX),
        verification_parallel=_bounded_int(simple.get("verification_parallel"), _AUTO_PARALLELISM_SENTINEL, 0, _SIMPLE_PARALLEL_MAX),
        subagents_per_turn=_bounded_int(simple.get("subagents_per_turn"), 3, 1, 4),
        max_candidates=_bounded_int(simple.get("max_candidates"), mode_defaults["candidates"], 1, 100),
        max_candidates_per_unit=_bounded_int(simple.get("max_candidates_per_unit"), 2, 1, 4),
        max_unit_files=_bounded_int(simple.get("max_unit_files"), 40, 5, 100),
        max_unit_bytes=_bounded_int(simple.get("max_unit_bytes"), 500_000, 50_000, 2_000_000),
        max_batch_files=_bounded_int(simple.get("max_batch_files"), 120, 10, 400),
        max_batch_bytes=_bounded_int(simple.get("max_batch_bytes"), 1_500_000, 100_000, 5_000_000),
        discovery_timeout_seconds=_bounded_int(simple.get("discovery_timeout_seconds"), 900, 60, 3600),
        verification_timeout_seconds=_bounded_int(simple.get("verification_timeout_seconds"), 1200, 60, 7200),
        scan_deadline_seconds=_bounded_int(simple.get("scan_deadline_seconds"), {"fast": 1800, "standard": 3600, "deep": 7200}.get(config.mode, 3600), 0, 21600),
        output_language=_clean_text(simple.get("output_language"), 80) or "English",
    )


def tune_simple_parallelism(settings: SimpleSettings, inventory: dict, account: dict) -> SimpleSettings:
    recommended = recommended_simple_parallelism(inventory, account)
    discovery = recommended if settings.discovery_parallel <= 0 else settings.discovery_parallel
    verification = recommended if settings.verification_parallel <= 0 else settings.verification_parallel
    return replace(
        settings,
        discovery_parallel=max(1, min(_SIMPLE_PARALLEL_MAX, discovery)),
        verification_parallel=max(1, min(_SIMPLE_PARALLEL_MAX, verification)),
    )


def recommended_simple_parallelism(inventory: dict, account: dict) -> int:
    if not isinstance(account, dict) or not bool(account.get("shared_app_server")):
        return 1
    cpu_count = os.cpu_count() or 1
    if cpu_count < 4:
        cpu_budget = 1
    elif cpu_count < 8:
        cpu_budget = 2
    elif cpu_count < 12:
        cpu_budget = 3
    else:
        cpu_budget = _SIMPLE_PARALLEL_MAX

    memory_gib = available_memory_gib()
    if memory_gib <= 0:
        memory_budget = _SIMPLE_PARALLEL_MAX
    elif memory_gib < 4:
        memory_budget = 1
    elif memory_gib < 8:
        memory_budget = 2
    elif memory_gib < 16:
        memory_budget = 3
    else:
        memory_budget = _SIMPLE_PARALLEL_MAX

    file_count, byte_count = inventory_pressure(inventory)
    if file_count < 25 and byte_count < 1_000_000:
        repo_budget = 1
    elif file_count < 250 and byte_count < 25_000_000:
        repo_budget = 2
    elif file_count < 1_000 and byte_count < 100_000_000:
        repo_budget = 3
    else:
        repo_budget = _SIMPLE_PARALLEL_MAX
    return max(1, min(_SIMPLE_PARALLEL_MAX, cpu_budget, memory_budget, repo_budget))


def inventory_pressure(inventory: dict) -> tuple[int, int]:
    if not isinstance(inventory, dict):
        return 0, 0
    files = analyzable_files(inventory)
    summary = inventory.get("summary") if isinstance(inventory.get("summary"), dict) else {}
    try:
        file_count = int(summary.get("analyzable_files") or len(files))
    except (TypeError, ValueError):
        file_count = len(files)
    byte_count = 0
    for item in files:
        if not isinstance(item, dict):
            continue
        try:
            byte_count += max(0, int(item.get("size_bytes") or 0))
        except (TypeError, ValueError):
            continue
    return max(0, file_count), byte_count


def available_memory_gib() -> float:
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("MemAvailable:"):
                    parts = line.split()
                    if len(parts) >= 2:
                        return max(0.0, int(parts[1]) / 1024 / 1024)
    except (OSError, ValueError):
        return 0.0
    return 0.0


def plan_review_units(files: list[dict], *, max_files: int, max_bytes: int) -> list[ReviewUnit]:
    grouped: dict[str, list[dict]] = {}
    for item in files:
        if not isinstance(item, dict):
            continue
        path = safe_relative_path(item.get("path"))
        if not path:
            continue
        grouped.setdefault(_area_for_path(path), []).append(item)
    units: list[ReviewUnit] = []
    sequence = 1
    for area in sorted(grouped):
        current: list[dict] = []
        current_bytes = 0
        for item in sorted(grouped[area], key=lambda value: str(value.get("path") or "")):
            size = max(0, int(item.get("size_bytes") or 0))
            if current and (len(current) >= max_files or current_bytes + size > max_bytes):
                units.append(_make_unit(sequence, area, current))
                sequence += 1
                current = []
                current_bytes = 0
            current.append(item)
            current_bytes += size
        if current:
            units.append(_make_unit(sequence, area, current))
            sequence += 1
    return units


def validate_unit_coverage(units: list[ReviewUnit], expected_files: set[str]) -> None:
    assigned: list[str] = [path for unit in units for path in unit.files]
    counts: dict[str, int] = {}
    for path in assigned:
        counts[path] = counts.get(path, 0) + 1
    assigned_set = set(counts)
    duplicates = sorted(path for path, count in counts.items() if count > 1)
    missing = sorted(expected_files - assigned_set)
    unexpected = sorted(assigned_set - expected_files)
    if duplicates or missing or unexpected:
        raise RuntimeError(
            "review unit coverage invalid: "
            f"duplicates={duplicates[:10]} missing={missing[:10]} unexpected={unexpected[:10]}"
        )


def plan_discovery_batches(
    units: list[ReviewUnit],
    *,
    target_turns: int,
    max_turns: int,
    max_batch_files: int,
    max_batch_bytes: int,
    subagents_per_turn: int,
) -> list[DiscoveryBatch]:
    if not units:
        return []
    turn_limit = min(max(1, max_turns), len(units))
    buckets: list[list[ReviewUnit]] = []
    bucket_bytes: list[int] = []
    bucket_files: list[int] = []
    for unit in sorted(units, key=lambda value: (-value.size_bytes, -len(value.files), value.unit_id)):
        if unit.size_bytes > max_batch_bytes or len(unit.files) > max_batch_files:
            raise RuntimeError(
                "full-repository review unit exceeds the configured discovery batch limits: "
                f"unit={unit.unit_id} files={len(unit.files)} bytes={unit.size_bytes}"
            )
        candidates = [
            index
            for index in range(len(buckets))
            if bucket_bytes[index] + unit.size_bytes <= max_batch_bytes
            and bucket_files[index] + len(unit.files) <= max_batch_files
        ]
        if candidates:
            index = max(
                candidates,
                key=lambda item: (bucket_bytes[item], bucket_files[item], -item),
            )
        else:
            index = len(buckets)
            buckets.append([])
            bucket_bytes.append(0)
            bucket_files.append(0)
        buckets[index].append(unit)
        bucket_bytes[index] += unit.size_bytes
        bucket_files[index] += len(unit.files)
    if len(buckets) > turn_limit:
        total_files = sum(len(unit.files) for unit in units)
        total_bytes = sum(unit.size_bytes for unit in units)
        raise RuntimeError(
            "full-repository review exceeds the bounded discovery plan: "
            f"required_turns={len(buckets)} max_turns={turn_limit} "
            f"files={total_files} bytes={total_bytes}"
        )
    desired_turns = min(max(1, target_turns), turn_limit)
    while len(buckets) < desired_turns:
        split_candidates = [index for index, bucket in enumerate(buckets) if len(bucket) > 1]
        if not split_candidates:
            break
        source_index = max(
            split_candidates,
            key=lambda item: (bucket_bytes[item], bucket_files[item], len(buckets[item]), -item),
        )
        moved = min(
            buckets[source_index],
            key=lambda unit: (unit.size_bytes, len(unit.files), unit.unit_id),
        )
        buckets[source_index].remove(moved)
        bucket_bytes[source_index] -= moved.size_bytes
        bucket_files[source_index] -= len(moved.files)
        buckets.append([moved])
        bucket_bytes.append(moved.size_bytes)
        bucket_files.append(len(moved.files))
    ordered_bucket_indices = sorted(
        range(len(buckets)),
        key=lambda item: min(unit.unit_id for unit in buckets[item]),
    )
    batches: list[DiscoveryBatch] = []
    for sequence, bucket_index in enumerate(ordered_bucket_indices, start=1):
        ordered_units = tuple(sorted(buckets[bucket_index], key=lambda value: value.unit_id))
        groups: list[list[str]] = [[] for _ in range(min(subagents_per_turn, len(ordered_units)))]
        group_bytes = [0 for _ in groups]
        for unit in sorted(ordered_units, key=lambda value: (-value.size_bytes, value.unit_id)):
            group_index = min(range(len(groups)), key=lambda item: (group_bytes[item], len(groups[item]), item))
            groups[group_index].append(unit.unit_id)
            group_bytes[group_index] += unit.size_bytes
        batches.append(
            DiscoveryBatch(
                batch_id=f"discovery-{sequence:03d}",
                units=ordered_units,
                agent_groups=tuple(tuple(group) for group in groups),
            )
        )
    return batches


def codex_account_preflight(config: CodexConfig, cwd: Path) -> dict:
    client = None
    try:
        client = get_codex_app_server_client(config.command or "codex", config.env, cwd)
        client.ensure_started()
        result = client.request("account/read", {"refreshToken": False}, timeout_seconds=30)
    except ProcessCancelled:
        if client is not None:
            client.close()
        raise
    except Exception as exc:
        detail = _codex_error("Codex account preflight failed", str(exc))
        if client is not None and _looks_like_auth_error(detail):
            client.close()
        raise RuntimeError(detail) from exc
    requires_auth = bool(result.get("requiresOpenaiAuth"))
    account = result.get("account") if isinstance(result.get("account"), dict) else None
    if requires_auth and account is None:
        client.close()
        raise RuntimeError("codex_auth_required: Codex app-server reports that OpenAI authentication is required")
    return {
        "requires_openai_auth": requires_auth,
        "account_type": _clean_text((account or {}).get("type"), 80),
        "plan_type": _clean_text((account or {}).get("planType"), 80),
        "refresh_forced": False,
        "shared_app_server": True,
    }


def normalize_candidate(raw: dict, unit_by_id: dict[str, ReviewUnit], inventory_by_path: dict[str, dict]) -> dict:
    unit_id = _clean_text(raw.get("unit_id"), 120)
    unit = unit_by_id.get(unit_id)
    if unit is None:
        raise CandidateRejected("candidate unit_id is not part of the assigned full-repository plan")
    required_text = {
        key: _clean_text(raw.get(key), 2_000)
        for key in (
            "title",
            "claim",
            "trigger_condition",
            "expected_behavior",
            "expected_behavior_source",
            "actual_behavior_hypothesis",
            "impact",
            "reproduction_idea",
        )
    }
    missing = [key for key, value in required_text.items() if not value]
    if missing:
        raise CandidateRejected(f"candidate missing required evidence fields: {', '.join(missing)}")
    severity = _clean_text(raw.get("severity"), 20).lower()
    if severity not in _SEVERITY_RANK:
        raise CandidateRejected("candidate severity is invalid")
    evidence: list[dict] = []
    raw_evidence = raw.get("evidence") if isinstance(raw.get("evidence"), list) else []
    for item in raw_evidence[:12]:
        if not isinstance(item, dict):
            continue
        path = safe_relative_path(item.get("file"))
        inventory_item = inventory_by_path.get(path)
        if not path or inventory_item is None:
            raise CandidateRejected(f"candidate evidence path is outside inventory: {item.get('file')}")
        start = _positive_int(item.get("line_start"))
        end = _positive_int(item.get("line_end"))
        line_count = max(1, int(inventory_item.get("line_count") or 1))
        if not start or start > line_count:
            raise CandidateRejected(f"candidate evidence line is outside file: {path}:{start}")
        if not end or end < start or end > line_count:
            raise CandidateRejected(f"candidate evidence range is invalid: {path}:{start}-{end}")
        why = _clean_text(item.get("why_it_matters"), 1_000)
        if not why:
            raise CandidateRejected(f"candidate evidence lacks why_it_matters: {path}:{start}")
        evidence.append({"file": path, "lines": f"{start}-{end}", "why_it_matters": why})
    if not evidence:
        raise CandidateRejected("candidate has no concrete file-and-line evidence")
    primary = evidence[0]["file"]
    if primary not in unit.files:
        raise CandidateRejected("candidate primary evidence is outside its reviewed unit")
    path_summary = [
        _clean_text(value, 240)
        for value in (raw.get("path_summary") if isinstance(raw.get("path_summary"), list) else [])[:8]
        if _clean_text(value, 240)
    ]
    if not path_summary:
        path_summary = [f"{unit.area} -> {primary}"]
    evidence_files = list(dict.fromkeys(item["file"] for item in evidence))
    fingerprint_source = "\n".join(
        [
            primary,
            evidence[0]["lines"],
            _normalize_text(required_text["claim"]),
            _normalize_text(required_text["trigger_condition"]),
        ]
    )
    digest = hashlib.sha256(fingerprint_source.encode("utf-8")).hexdigest()[:20]
    candidate_id = f"cand-{digest}"
    return {
        "candidate_id": candidate_id,
        "issue_id": candidate_id,
        "dedupe_key": f"sha256:{hashlib.sha256(fingerprint_source.encode('utf-8')).hexdigest()}",
        "unit_id": unit_id,
        "severity": severity,
        "category": _clean_text(raw.get("category"), 80) or "Correctness",
        "title": required_text["title"][:240],
        "claim": required_text["claim"],
        "trigger_condition": required_text["trigger_condition"],
        "expected_behavior": required_text["expected_behavior"],
        "expected_behavior_source": required_text["expected_behavior_source"],
        "actual_behavior_hypothesis": required_text["actual_behavior_hypothesis"],
        "impact": required_text["impact"],
        "reproduction_idea": required_text["reproduction_idea"],
        "minimal_repro_idea": required_text["reproduction_idea"],
        "evidence": evidence,
        "graph_evidence": {
            "slice_id": unit_id,
            "unit_id": unit_id,
            "path_summary": path_summary,
            "codegraph_files": evidence_files,
            "context_files": evidence_files,
        },
    }


def limit_candidates_per_unit(candidates: list[dict], limit: int) -> tuple[list[dict], list[dict]]:
    maximum = max(0, int(limit or 0))
    counts: dict[str, int] = {}
    kept: list[dict] = []
    rejected: list[dict] = []
    for candidate in sorted(candidates, key=_candidate_sort_key):
        unit_id = str(candidate.get("unit_id") or "")
        count = counts.get(unit_id, 0)
        if count >= maximum:
            rejected.append(
                {
                    "stage": "unit-budget",
                    "candidate_id": str(candidate.get("candidate_id") or ""),
                    "reason": f"per-unit runtime verification budget exhausted for {unit_id}",
                }
            )
            continue
        counts[unit_id] = count + 1
        kept.append(candidate)
    return kept, rejected


def dedupe_candidates(candidates: list[dict]) -> tuple[list[dict], list[dict]]:
    kept: dict[str, dict] = {}
    rejected: list[dict] = []
    for candidate in sorted(candidates, key=_candidate_sort_key):
        key = str(candidate.get("dedupe_key") or candidate.get("candidate_id") or "")
        if key in kept:
            rejected.append(
                {
                    "stage": "dedupe",
                    "candidate_id": str(candidate.get("candidate_id") or ""),
                    "reason": f"duplicate of {kept[key].get('candidate_id')}",
                }
            )
            continue
        kept[key] = candidate
    return list(kept.values()), rejected


def select_candidates(candidates: list[dict], limit: int) -> tuple[list[dict], list[dict]]:
    ordered = sorted(candidates, key=_candidate_sort_key)
    selected = ordered[: max(0, limit)]
    rejected = [
        {
            "stage": "budget",
            "candidate_id": str(candidate.get("candidate_id") or ""),
            "reason": "runtime verification budget exhausted",
        }
        for candidate in ordered[len(selected) :]
    ]
    return selected, rejected


def parse_command_events(path: Path) -> list[CommandEvidence]:
    if path.is_symlink() or not path.is_file():
        return []
    try:
        metadata = path.stat()
    except OSError:
        return []
    if not stat.S_ISREG(metadata.st_mode):
        return []
    commands: list[CommandEvidence] = []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if len(line) > MAX_EVENT_LINE_CHARS:
                    continue
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if message.get("method") != "item/completed":
                    continue
                params = message.get("params") if isinstance(message.get("params"), dict) else {}
                item = params.get("item") if isinstance(params.get("item"), dict) else {}
                if item.get("type") != "commandExecution":
                    continue
                exit_code = item.get("exitCode")
                if isinstance(exit_code, bool) or not isinstance(exit_code, int):
                    continue
                commands.append(
                    CommandEvidence(
                        command=str(item.get("command") or "").strip(),
                        cwd=str(item.get("cwd") or "").strip(),
                        exit_code=exit_code,
                        output=str(item.get("aggregatedOutput") or "")[-MAX_COMMAND_OUTPUT_CHARS:],
                        status=str(item.get("status") or "").strip(),
                    )
                )
                if len(commands) > 400:
                    del commands[:-200]
    except OSError:
        return []
    return commands[-200:]

def validate_verification_result(
    candidate: dict,
    payload: dict,
    commands: list[CommandEvidence],
    worker_repo: Path,
    *,
    source_changed: bool,
) -> tuple[CommandEvidence, str]:
    candidate_id = str(candidate.get("candidate_id") or "")
    if _clean_text(payload.get("candidate_id"), 120) != candidate_id:
        raise VerificationRejected("verification candidate_id does not match")
    if payload.get("status") != "confirmed" or payload.get("safe_to_show_user") is not True:
        raise VerificationRejected(_clean_text(payload.get("reason"), MAX_DEBUG_REASON_CHARS) or "verifier rejected candidate")
    if payload.get("skeptic_agreed") is not True:
        raise VerificationRejected("independent skeptic did not agree")
    if source_changed:
        raise VerificationRejected("verification modified repository source files")
    declared_command = _clean_text(payload.get("reproduction_command"), 2_000)
    marker = _clean_text(payload.get("output_marker"), 500)
    if (
        not declared_command
        or not marker
        or len(marker) < 8
        or marker.strip().lower() in {"error", "failed", "false", "pass", "passed", "true"}
    ):
        raise VerificationRejected("verification lacks an executed command and distinctive output marker")
    matching = [
        item
        for item in commands
        if command_matches(declared_command, item.command)
        and marker in item.output
        and _command_cwd_is_safe(item.cwd, worker_repo)
        and item.status.lower() in {"completed", "failed"}
    ]
    if not matching:
        raise VerificationRejected(
            "the reproduction command and output marker must appear in a completed app-server command event"
        )
    actual = matching[-1]
    if not command_is_reproduction(actual.command):
        raise VerificationRejected("recorded command is inspection-only and does not reproduce behavior")
    if command_uses_inline_code(actual.command):
        raise VerificationRejected("final reproduction command must execute a repro harness file, not inline code")
    if not command_references_repro_harness(actual.command, worker_repo):
        raise VerificationRejected("final reproduction command must execute a harness under .codereview/repro")
    exercised_files = [safe_relative_path(value) for value in payload.get("exercised_files", [])]
    exercised_files = [value for value in exercised_files if value]
    primary_files = [safe_relative_path(item.get("file")) for item in candidate.get("evidence", []) if isinstance(item, dict)]
    primary_files = [value for value in primary_files if value]
    if not exercised_files or not set(primary_files).intersection(exercised_files):
        raise VerificationRejected("verification did not exercise the candidate's source evidence")
    for rel in exercised_files:
        target = worker_repo / rel
        if not target.is_file() or target.is_symlink() or not is_within(target, worker_repo):
            raise VerificationRejected(f"verification exercised file is invalid: {rel}")
    if not verification_source_is_grounded(candidate, matching, worker_repo, marker=marker):
        raise VerificationRejected(
            "reproduction command, output, and referenced harnesses do not ground execution in the cited source files"
        )
    reason = _clean_text(payload.get("reason"), 2_000)
    expected = _clean_text(payload.get("expected_behavior"), 2_000)
    observed = _clean_text(payload.get("observed_behavior"), 2_000)
    independent = _clean_text(payload.get("independent_check"), 2_000)
    if not all((reason, expected, observed, independent)):
        raise VerificationRejected("verification lacks expected, observed, reason, or independent check")
    candidate_expected = _clean_text(candidate.get("expected_behavior"), 2_000)
    if candidate_expected and _normalize_text(expected) != _normalize_text(candidate_expected):
        raise VerificationRejected("verification changed the candidate's expected behavior")
    if _normalize_text(expected) == _normalize_text(observed):
        raise VerificationRejected("verification did not observe behavior that differs from the expectation")
    return actual, marker


def command_matches(declared: str, actual: str) -> bool:
    left = _normalize_command(declared)
    right = _normalize_command(actual)
    return bool(left and right and left == right)


def command_is_reproduction(command: str) -> bool:
    return any(_segment_executes_code(segment) for segment in _shell_command_segments(command))


def command_uses_inline_code(command: str) -> bool:
    for segment in _shell_command_segments(command):
        tokens = _unwrap_command_tokens(segment)
        if not tokens:
            continue
        name = Path(tokens[0]).name.lower()
        arguments = tokens[1:]
        if name in _INLINE_SHELLS and "-c" in arguments:
            return True
        forbidden = _INLINE_CODE_FLAGS.get(name, set())
        for argument in arguments:
            lowered = argument.lower()
            if lowered in forbidden or any(lowered.startswith(f"{flag}=") for flag in forbidden):
                return True
    return False


def command_references_repro_harness(command: str, worker_repo: Path) -> bool:
    for path in _command_referenced_files(command, worker_repo):
        try:
            relative = path.relative_to(worker_repo).as_posix()
        except ValueError:
            continue
        if relative.startswith(".codereview/repro/"):
            return True
    return False


def verification_source_is_grounded(
    candidate: dict,
    commands: list[CommandEvidence],
    worker_repo: Path,
    *,
    marker: str,
) -> bool:
    evidence_files = [
        safe_relative_path(item.get("file"))
        for item in candidate.get("evidence", [])
        if isinstance(item, dict)
    ]
    evidence_files = [path for path in evidence_files if path]
    if not evidence_files:
        return False
    corpus = [item.command for item in commands]
    corpus.extend(item.output for item in commands)
    for command in commands:
        for path in _command_referenced_files(command.command, worker_repo):
            try:
                if path.stat().st_size > _MAX_HARNESS_BYTES:
                    continue
                content = path.read_text(encoding="utf-8", errors="replace")
                relative = path.relative_to(worker_repo).as_posix()
                if relative.startswith(".codereview/repro/") and marker in content:
                    return False
                corpus.append(content)
            except (OSError, ValueError):
                continue
    normalized_corpus = "\n".join(corpus).replace("\\", "/").lower()
    return any(
        variant in normalized_corpus
        for source in evidence_files
        for variant in _source_reference_variants(source)
    )


def _shell_command_segments(command: str) -> list[list[str]]:
    try:
        lexer = shlex.shlex(str(command or ""), posix=True, punctuation_chars=";&|")
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens = list(lexer)
    except ValueError:
        return []
    segments: list[list[str]] = []
    current: list[str] = []
    for token in tokens:
        if token in _SHELL_OPERATORS:
            if current:
                segments.append(current)
                current = []
            continue
        current.append(token)
    if current:
        segments.append(current)
    return segments


def _segment_executes_code(tokens: list[str]) -> bool:
    tokens = _unwrap_command_tokens(tokens)
    if not tokens:
        return False
    executable = tokens[0]
    name = Path(executable).name.lower()
    arguments = tokens[1:]
    if name in _NON_REPRO_COMMANDS:
        return False
    if executable.startswith("./"):
        return True
    if name in {"bash", "sh", "zsh"}:
        if "-c" in arguments:
            index = arguments.index("-c")
            return index + 1 < len(arguments) and command_is_reproduction(arguments[index + 1])
        return any(not argument.startswith("-") for argument in arguments)
    if name in _DIRECT_RUNTIME_EXECUTABLES:
        return bool(arguments) and not all(argument.lower() in _INFO_ONLY_ARGUMENTS for argument in arguments)
    if name in {"go", "cargo", "swift", "dotnet"}:
        return bool(arguments) and arguments[0].lower() in {"run", "test"}
    if name in {"npm", "pnpm", "yarn"}:
        return bool(arguments) and arguments[0].lower() in {"exec", "run", "test"}
    if name == "npx":
        return bool(arguments) and arguments[0].lower() not in _INFO_ONLY_ARGUMENTS
    if name in {"gradle", "gradlew", "mvn", "mvnw", "make"}:
        return bool(arguments) and not all(argument.lower() in _INFO_ONLY_ARGUMENTS for argument in arguments)
    return False


def _unwrap_command_tokens(tokens: list[str]) -> list[str]:
    values = list(tokens)
    while values and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", values[0]):
        values.pop(0)
    while values and Path(values[0]).name.lower() in {"command", "exec"}:
        values.pop(0)
    if values and Path(values[0]).name.lower() == "env":
        values.pop(0)
        while values and (values[0].startswith("-") or re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", values[0])):
            values.pop(0)
    if values and Path(values[0]).name.lower() == "timeout":
        values.pop(0)
        while values and values[0].startswith("-"):
            values.pop(0)
        if values:
            values.pop(0)
    return values


def _command_referenced_files(command: str, worker_repo: Path) -> list[Path]:
    paths: list[Path] = []
    seen: set[Path] = set()
    for segment in _shell_command_segments(command):
        for token in segment:
            raw = token.split("::", 1)[0].strip()
            if "=" in raw and raw.startswith("--"):
                raw = raw.split("=", 1)[1]
            suffix = Path(raw).suffix.lower()
            if suffix not in _SCRIPT_FILE_SUFFIXES:
                continue
            candidate = Path(raw)
            if not candidate.is_absolute():
                candidate = worker_repo / candidate
            try:
                resolved = candidate.resolve(strict=True)
            except OSError:
                continue
            if resolved.is_symlink() or not resolved.is_file() or not is_within(resolved, worker_repo):
                continue
            if resolved not in seen:
                seen.add(resolved)
                paths.append(resolved)
    return paths[:20]


def _source_reference_variants(path: str) -> set[str]:
    normalized = path.replace("\\", "/").strip("/").lower()
    if not normalized:
        return set()
    without_suffix = normalized.rsplit(".", 1)[0] if "." in Path(normalized).name else normalized
    variants = {normalized, without_suffix, without_suffix.replace("/", ".")}
    return {variant for variant in variants if len(variant) >= 3}


def validate_discovery_payload(batch: DiscoveryBatch, result: dict) -> tuple[list[str], list[dict]]:
    reviewed = {
        _clean_text(value, 120)
        for value in result.get("reviewed_unit_ids", [])
        if _clean_text(value, 120)
    }
    expected = {unit.unit_id for unit in batch.units}
    if reviewed != expected:
        missing = sorted(expected - reviewed)
        extra = sorted(reviewed - expected)
        raise RuntimeError(
            f"{batch.batch_id} did not prove complete unit coverage: "
            f"missing={missing[:20]} extra={extra[:20]}"
        )
    candidates = result.get("candidates") if isinstance(result.get("candidates"), list) else []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        unit_id = str(candidate.get("unit_id") or "")
        if unit_id not in expected:
            raise RuntimeError(
                f"{batch.batch_id} returned candidate for unit outside its assignment: {unit_id or '<missing>'}"
            )
    return sorted(reviewed), candidates


def _run_discovery(
    snapshot_repo: Path,
    run: Path,
    batches: list[DiscoveryBatch],
    units: list[ReviewUnit],
    inventory_by_path: dict[str, dict],
    codex: CodexConfig,
    settings: SimpleSettings,
    progress: ProgressCallback | None,
    run_id: str,
) -> list[dict]:
    schema_path = run / "schemas" / "discovery.schema.json"
    write_json(schema_path, DISCOVERY_SCHEMA)

    def execute(batch: DiscoveryBatch) -> dict:
        if process_cancel_requested():
            raise ProcessCancelled("discovery cancelled")
        work = run / "workers" / batch.batch_id
        ensure_dir(work)
        assignment_dir = snapshot_repo / ".codereview" / "simple" / "assignments"
        ensure_dir(assignment_dir)
        assignment_path = assignment_dir / f"{batch.batch_id}.json"
        assignment = {
            "scope": "full-repository",
            "batch_id": batch.batch_id,
            "max_candidates_per_unit": settings.max_candidates_per_unit,
            "agent_groups": [list(group) for group in batch.agent_groups],
            "units": [unit.to_dict(inventory_by_path) for unit in batch.units],
        }
        write_json(assignment_path, assignment)
        output = work / "output.json"
        events = work / "events.jsonl"
        prompt = discovery_prompt(
            assignment_path.relative_to(snapshot_repo).as_posix(),
            batch,
            settings,
        )
        result = _run_codex_json(
            cd=snapshot_repo,
            prompt=prompt,
            schema_path=schema_path,
            output_file=output,
            events_file=events,
            sandbox="read-only",
            timeout_seconds=settings.discovery_timeout_seconds,
            codex=codex,
        )
        reviewed, candidates = validate_discovery_payload(batch, result)
        write_json(work / "validated.json", result)
        return {"batch_id": batch.batch_id, "reviewed_unit_ids": reviewed, "candidates": candidates}

    return _parallel_collect(
        batches,
        execute,
        max_workers=settings.discovery_parallel,
        progress=lambda completed, total: _emit(
            progress,
            "finder",
            f"Discovery {completed}/{total}",
            current=completed,
            total=total,
            run_id=run_id,
        ),
    )


def _run_verifications(
    snapshot_repo: Path,
    run: Path,
    candidates: list[dict],
    codex: CodexConfig,
    settings: SimpleSettings,
    stop_event: threading.Event,
    progress: ProgressCallback | None,
    run_id: str,
    deadline_at: float = 0.0,
) -> list[dict]:
    schema_path = run / "schemas" / "verification.schema.json"
    write_json(schema_path, VERIFICATION_SCHEMA)
    reuse_lane = settings.verification_parallel <= 1
    lane_root = run / "workers" / "verification-lanes" / "lane-0"
    lane_repo = lane_root / "repo"
    lane_source_state: dict | None = None

    def prepare_workspace(candidate_id: str, candidate: dict) -> tuple[Path, Path, dict, bool]:
        nonlocal lane_source_state
        worker_root = run / "workers" / "verification" / candidate_id
        _reset_verification_artifact_dir(worker_root)
        write_json(worker_root / "input_candidate.json", candidate)
        write_json(worker_root / "candidate.json", candidate)
        if reuse_lane:
            if lane_source_state is None or not lane_repo.is_dir():
                create_worker_dir(snapshot_repo, lane_root, candidate)
                lane_source_state = capture_source_state(lane_repo, include_untracked=True)
            else:
                _reset_lane_repro_dir(lane_repo)
            return worker_root, lane_repo, lane_source_state, True
        create_worker_dir(snapshot_repo, worker_root, candidate)
        worker_repo = worker_root / "repo"
        return worker_root, worker_repo, capture_source_state(worker_repo, include_untracked=True), False

    def execute(candidate: dict) -> dict:
        nonlocal lane_source_state
        candidate_id = str(candidate.get("candidate_id") or "candidate")
        if stop_event.is_set() or process_cancel_requested():
            raise ProcessCancelled("verification cancelled")
        remaining_seconds = _deadline_remaining_seconds(deadline_at)
        if deadline_at and remaining_seconds < 60:
            return {"candidate_id": candidate_id, "confirmed": False, "reason": "global scan deadline exhausted before verification"}
        verification_timeout = settings.verification_timeout_seconds
        if deadline_at:
            verification_timeout = min(verification_timeout, max(60, remaining_seconds))
        worker_root, worker_repo, source_before, using_lane = prepare_workspace(candidate_id, candidate)
        candidate_dir = worker_repo / ".codereview" / "simple"
        ensure_dir(candidate_dir)
        candidate_path = candidate_dir / "candidate.json"
        write_json(candidate_path, candidate)
        output = worker_root / "output.json"
        events = worker_root / "events.jsonl"
        try:
            payload = _run_codex_json(
                cd=worker_repo,
                prompt=verification_prompt(candidate_path.relative_to(worker_repo).as_posix(), candidate, settings),
                schema_path=schema_path,
                output_file=output,
                events_file=events,
                sandbox="workspace-write",
                timeout_seconds=verification_timeout,
                codex=codex,
            )
        except ProcessCancelled:
            raise
        except Exception as exc:
            if using_lane:
                lane_source_state = None
            if _looks_like_readiness_error(str(exc)):
                stop_event.set()
                raise
            return {"candidate_id": candidate_id, "confirmed": False, "reason": f"verification turn failed: {_clean_text(str(exc), MAX_DEBUG_REASON_CHARS)}"}
        source_after = capture_source_state(worker_repo, include_untracked=True)
        changed = source_state_changed(source_before, source_after)
        if using_lane and changed:
            lane_source_state = None
        commands = parse_command_events(events)
        try:
            actual, marker = validate_verification_result(
                candidate,
                payload,
                commands,
                worker_repo,
                source_changed=changed,
            )
        except VerificationRejected as exc:
            return {"candidate_id": candidate_id, "confirmed": False, "reason": str(exc)}
        if using_lane:
            lane_source_state = source_before
        event_log_path = f"workers/verification/{candidate_id}/events.jsonl"
        observed = _clean_text(payload.get("observed_behavior"), 2_000)
        expected = _clean_text(payload.get("expected_behavior"), 2_000)
        reason = _clean_text(payload.get("reason"), 2_000)
        independent = _clean_text(payload.get("independent_check"), 2_000)
        excerpt = _output_excerpt(actual.output, marker)
        limitations = [
            _clean_text(value, 500)
            for value in payload.get("limitations", [])[:8]
            if _clean_text(value, 500)
        ]
        item = {
            "candidate": candidate,
            "verification": {
                "status": "confirmed",
                "verdict": "confirmed",
                "level": "L2",
                "safe_to_show_user": True,
                "reason": reason,
            },
            "repro": {
                "status": "reproduced",
                "level": "L2",
                "summary": observed,
                "commands_run": [
                    {
                        "cmd": actual.command,
                        "exit_code": actual.exit_code,
                        "log_path": event_log_path,
                    }
                ],
                "proof": {
                    "type": "runtime-command",
                    "expected": expected,
                    "actual": observed,
                    "log_excerpt": excerpt,
                },
                "graph_path_exercised": True,
                "limitations": limitations,
            },
            "judge": {
                "status": "confirmed",
                "level": "L2",
                "safe_to_show_user": True,
                "reason": independent,
                "evidence_summary": {
                    "command": actual.command,
                    "log_path": event_log_path,
                    "observable": marker,
                },
                "limitations": limitations,
            },
        }
        write_json(worker_root / "confirmed.json", item)
        return {"candidate_id": candidate_id, "confirmed": True, "item": item}

    return _parallel_collect(
        candidates,
        execute,
        max_workers=settings.verification_parallel,
        progress=lambda completed, total: _emit(
            progress,
            "verification",
            f"Runtime verification {completed}/{total}",
            current=completed,
            total=total,
            run_id=run_id,
        ),
    )


def _reset_verification_artifact_dir(worker_root: Path) -> None:
    if worker_root.is_symlink():
        worker_root.unlink()
    elif worker_root.exists():
        if not is_within(worker_root, worker_root.parent):
            raise RuntimeError(f"refusing to reset verification artifact dir outside worker root: {worker_root}")
        if worker_root.is_dir():
            shutil.rmtree(worker_root)
        else:
            worker_root.unlink()
    ensure_dir(worker_root)


def _reset_lane_repro_dir(worker_repo: Path) -> None:
    repro = worker_repo / ".codereview" / "repro"
    if repro.is_symlink():
        repro.unlink()
    elif repro.exists():
        if not is_within(repro, worker_repo):
            raise RuntimeError(f"refusing to reset repro dir outside lane repo: {repro}")
        if repro.is_dir():
            shutil.rmtree(repro)
        else:
            repro.unlink()
    ensure_dir(repro)


def _deadline_remaining_seconds(deadline_at: float) -> int:
    if not deadline_at:
        return 0
    return max(0, int(deadline_at - time.monotonic()))


def discovery_prompt(assignment_path: str, batch: DiscoveryBatch, settings: SimpleSettings) -> str:
    return f"""You are the coordinator for a full-repository code review. This is not a diff review.

Read the assignment at {assignment_path}. It is the authoritative list of units and files for this turn.
Explicitly delegate the listed agent_groups to at most {settings.subagents_per_turn} Codex subagents when the multi-agent tools are available. If they are unavailable, review the groups yourself. Every file in every assigned unit must be inspected before that unit is listed in reviewed_unit_ids.

Review goals:
- Find concrete correctness, security, data-integrity, concurrency, lifecycle, or reliability defects.
- Ignore style-only concerns, speculative risks, and issues that cannot be tested locally.
- Do not use a diff as scope. Review the current full snapshot.
- Return at most {settings.max_candidates_per_unit} strong candidates per unit.
- Each candidate needs a precise trigger, an expected-behavior source, file-and-line evidence, and a realistic local reproduction idea.
- Do not claim a candidate is confirmed. Runtime confirmation happens in a later isolated turn.
- Keep user-facing candidate text in {settings.output_language}.

Return JSON matching the supplied schema. reviewed_unit_ids must exactly equal every unit_id in the assignment, with no omissions or extras.
"""


def verification_prompt(candidate_path: str, candidate: dict, settings: SimpleSettings) -> str:
    candidate_id = str(candidate.get("candidate_id") or "")
    return f"""Independently verify candidate {candidate_id} from {candidate_path} in this isolated repository copy.

Use Codex subagents when available: delegate one agent to trace the claimed behavior and one skeptical agent to challenge the claim and reproduction. The coordinator owns the final decision and must execute the final reproduction command itself.

Hard rules:
- Network access is unavailable. Do not install dependencies or fetch anything.
- Do not modify repository source files. Temporary harnesses may only be created below .codereview/repro/.
- A finding is confirmed only when an actually executed command exercises the cited repository code and produces deterministic observable evidence.
- Prefer a small harness below .codereview/repro/ that imports or invokes the cited code. Do not use echo, printf, cat, or a print-only inline snippet as proof.
- The final reproduction command must execute a normal harness file below .codereview/repro/. Do not use python -c, node -e, ruby -e, php -r, sh -c, bash -c, or other inline-code execution as final proof.
- Execute the final reproduction command at least once. Prefer a repeat when it is cheap, but do not reject a real reproduction only because it was run once or exit codes differ.
- Set reproduction_command to the exact command that produced the observed evidence.
- Set output_marker to an exact non-trivial substring present in the real stdout/stderr. It must come from the observed result or assertion, not unconditional hard-coded output.
- exercised_files must include the cited source file paths that the command actually exercised.
- Reject the candidate when reproduction depends on unavailable services, credentials, timing guesses, or assumptions.
- skeptic_agreed may be true only after the independent challenge finds the proof valid.
- Keep user-facing text in {settings.output_language}.

Return JSON matching the supplied schema. Do not include a finding merely because static reading makes it plausible.
"""


def _run_codex_json(
    *,
    cd: Path,
    prompt: str,
    schema_path: Path,
    output_file: Path,
    events_file: Path,
    sandbox: str,
    timeout_seconds: int,
    codex: CodexConfig,
) -> dict:
    if codex.max_input_chars and len(prompt) > codex.max_input_chars:
        raise RuntimeError(
            f"Codex prompt exceeds configured input limit: {len(prompt)} > {codex.max_input_chars}"
        )
    result = run_codex_turn(
        cd=cd,
        prompt=prompt,
        output_schema=schema_path,
        output_file=output_file,
        sandbox=sandbox,
        timeout_seconds=timeout_seconds,
        config=codex,
        env=codex.env,
        events_file=events_file,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "Codex turn failed")[-2_000:]
        raise RuntimeError(_codex_error("Codex turn failed", detail))
    try:
        payload = json.loads(output_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Codex turn returned invalid structured JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("Codex structured output must be a JSON object")
    return payload


def _parallel_collect(
    items: Iterable,
    worker: Callable,
    *,
    max_workers: int,
    progress: Callable[[int, int], None],
) -> list:
    values = list(items)
    if not values:
        return []
    completed = 0
    results_by_index: dict[int, object] = {}
    worker_count = max(1, min(_SIMPLE_PARALLEL_MAX, int(max_workers or 1), len(values)))
    if worker_count <= 1:
        for index, item in enumerate(values):
            results_by_index[index] = worker(item)
            completed += 1
            progress(completed, len(values))
        return [results_by_index[index] for index in range(len(values))]
    executor = concurrent.futures.ThreadPoolExecutor(
        max_workers=worker_count,
        thread_name_prefix="pullwise-simple-review",
    )
    futures: dict[concurrent.futures.Future, int] = {}
    try:
        for index, item in enumerate(values):
            futures[executor.submit(worker, item)] = index
        for future in concurrent.futures.as_completed(futures):
            index = futures[future]
            try:
                results_by_index[index] = future.result()
            except Exception:
                for pending in futures:
                    pending.cancel()
                raise
            completed += 1
            progress(completed, len(values))
    finally:
        executor.shutdown(wait=True, cancel_futures=True)
    return [results_by_index[index] for index in range(len(values))]


def _write_reports(
    run: Path,
    *,
    mode: str,
    scan_mode: str,
    inventory: dict,
    units: list[ReviewUnit],
    discovery_results: list[dict],
    raw_candidates: list[dict],
    valid_candidates: list[dict],
    selected_candidates: list[dict],
    confirmed: list[dict],
    rejected: list[dict],
    account: dict,
    progress: ProgressCallback | None,
) -> Path:
    reviewed_unit_ids = {
        str(unit_id)
        for result in discovery_results
        for unit_id in (result.get("reviewed_unit_ids") if isinstance(result.get("reviewed_unit_ids"), list) else [])
        if str(unit_id)
    }
    planned_files = sum(len(unit.files) for unit in units)
    coverage_complete = not units or len(reviewed_unit_ids) == len(units)
    verification_budget_dropped = len(
        [item for item in rejected if item.get("stage") in {"budget", "unit-budget"}]
    )
    verification_complete = verification_budget_dropped == 0
    verification_rejected = len([item for item in rejected if item.get("stage") == "verification"])
    summary = {
        "engine": {
            "version": ENGINE_VERSION,
            "stages": ["snapshot", "discovery", "runtime-verification", "report"],
            "sharedAppServer": True,
            "forcedTokenRefresh": False,
        },
        "inventory": {
            "files": int((inventory.get("summary") or {}).get("files") or 0),
            "analyzableFiles": int((inventory.get("summary") or {}).get("analyzable_files") or 0),
            "mode": "full-repository-snapshot",
        },
        "coverage": {
            "plannedUnits": len(units),
            "reviewedUnits": len(reviewed_unit_ids),
            "plannedFiles": planned_files,
            "complete": coverage_complete and verification_complete,
            "discoveryComplete": coverage_complete,
            "discoveredCandidates": len(raw_candidates),
            "validCandidates": len(valid_candidates),
            "verifiedCandidates": len(selected_candidates),
            "verificationBudgetDropped": verification_budget_dropped,
            "verificationComplete": verification_complete,
        },
        "finder": {
            "tasks": len(discovery_results),
            "results": len(discovery_results),
            "blocked": 0,
            "candidates": len(raw_candidates),
            "blockedItems": [],
        },
        "candidates": {
            "raw": len(raw_candidates),
            "valid": len(valid_candidates),
            "selectedForRepro": len(selected_candidates),
        },
        "repro": {
            "tasks": len(selected_candidates),
            "confirmed": len(confirmed),
            "rejected": 0,
            "internalRejected": verification_rejected,
            "blocked": 0,
            "blockedItems": [],
        },
        "judge": {
            "implementation": "deterministic-event-gate",
            "confirmed": len(confirmed),
            "rejected": 0,
            "internalRejected": len(rejected),
            "blocked": 0,
            "blockedItems": [],
        },
        "reports": {
            "confirmed": len(confirmed),
            "rejected": 0,
            "blocked": 0 if coverage_complete and verification_complete else 1,
        },
        "codexAccount": account,
    }
    reports = run / "reports"
    ensure_dir(reports)
    diagnostics = run / "diagnostics"
    ensure_dir(diagnostics)
    internal_diagnostics = build_internal_diagnostics(
        selected_candidates=selected_candidates,
        rejected=rejected,
    )
    write_json(diagnostics / "internal-rejections.json", rejected)
    write_json(reports / "diagnostics.json", internal_diagnostics)
    write_json(reports / "confirmed.json", confirmed)
    # Unconfirmed hypotheses remain internal. They are never part of the public report contract.
    write_json(reports / "rejected.json", [])
    write_json(reports / "final.json", {"confirmed": confirmed})
    write_json(reports / "summary.json", summary)
    write_text(reports / "final.md", render_final_markdown(confirmed, mode=mode, scan_mode=scan_mode))
    write_text(reports / "debug.md", render_debug_markdown(summary, rejected))
    _emit(progress, "report", f"Report: {len(confirmed)} confirmed finding(s)", run_id=run.name)
    return reports / "final.md"


def render_final_markdown(confirmed: list[dict], *, mode: str, scan_mode: str) -> str:
    lines = [
        "# Pullwise full-repository review",
        "",
        f"Engine: `{ENGINE_VERSION}`  ",
        f"Mode: `{mode}`  ",
        f"Scan mode: `{scan_mode}`  ",
        "Scope: full repository snapshot, not a diff.",
        "",
    ]
    if not confirmed:
        lines.append("No runtime-reproducible findings were confirmed.")
        return "\n".join(lines) + "\n"
    lines.append(f"Confirmed findings: **{len(confirmed)}**")
    lines.append("")
    for index, item in enumerate(confirmed, start=1):
        candidate = item.get("candidate") if isinstance(item.get("candidate"), dict) else {}
        repro = item.get("repro") if isinstance(item.get("repro"), dict) else {}
        proof = repro.get("proof") if isinstance(repro.get("proof"), dict) else {}
        commands = repro.get("commands_run") if isinstance(repro.get("commands_run"), list) else []
        command = commands[0] if commands and isinstance(commands[0], dict) else {}
        lines.extend(
            [
                f"## {index}. {_clean_text(candidate.get('title'), 240)}",
                "",
                f"- Severity: `{_clean_text(candidate.get('severity'), 20)}`",
                f"- Trigger: {_clean_text(candidate.get('trigger_condition'), MAX_REPORT_TEXT_CHARS)}",
                f"- Expected: {_clean_text(proof.get('expected'), MAX_REPORT_TEXT_CHARS)}",
                f"- Observed: {_clean_text(proof.get('actual'), MAX_REPORT_TEXT_CHARS)}",
                f"- Command: `{_clean_text(command.get('cmd'), 1_000)}`",
                f"- Exit code: `{command.get('exit_code')}`",
                "",
            ]
        )
    return "\n".join(lines) + "\n"


def render_debug_markdown(summary: dict, rejected: list[dict]) -> str:
    reason_counts = internal_rejection_reason_counts(rejected)
    lines = [
        "# Pullwise review diagnostics",
        "",
        f"- Coverage complete: `{bool((summary.get('coverage') or {}).get('complete'))}`",
        f"- Discovery complete: `{bool((summary.get('coverage') or {}).get('discoveryComplete'))}`",
        f"- Verification complete: `{bool((summary.get('coverage') or {}).get('verificationComplete'))}`",
        f"- Raw candidates: `{(summary.get('candidates') or {}).get('raw', 0)}`",
        f"- Verified candidates: `{(summary.get('coverage') or {}).get('verifiedCandidates', 0)}`",
        f"- Verification budget dropped: `{(summary.get('coverage') or {}).get('verificationBudgetDropped', 0)}`",
        f"- Internal verification rejections: `{(summary.get('repro') or {}).get('internalRejected', 0)}`",
        f"- Confirmed: `{(summary.get('reports') or {}).get('confirmed', 0)}`",
        "",
        "Only runtime-confirmed findings are included. Unconfirmed hypotheses are not exposed.",
    ]
    if reason_counts:
        lines.extend(["", "Internal rejection reason counts:"])
        for item in reason_counts[:MAX_DEBUG_REASON_BUCKETS]:
            reason = _clean_text(item.get("reason"), MAX_DEBUG_REASON_CHARS)
            count = _positive_int(item.get("count"))
            if reason and count:
                lines.append(f"- `{count}` x {reason}")
    return "\n".join(lines) + "\n"


def build_internal_diagnostics(*, selected_candidates: list[dict], rejected: list[dict]) -> dict:
    return {
        "schemaVersion": 1,
        "selectedCandidateCount": len(selected_candidates),
        "internalRejectionCount": len(rejected),
        "reasonCounts": internal_rejection_reason_counts(rejected),
        "internalRejections": [internal_rejection_payload(item) for item in rejected[:MAX_INTERNAL_DIAGNOSTIC_ITEMS]],
        "selectedCandidates": [
            internal_candidate_summary(candidate)
            for candidate in selected_candidates[:MAX_INTERNAL_DIAGNOSTIC_ITEMS]
        ],
    }


def internal_rejection_reason_counts(rejected: list[dict]) -> list[dict]:
    counts: dict[tuple[str, str], dict] = {}
    for item in rejected:
        if not isinstance(item, dict):
            continue
        stage = _clean_text(item.get("stage"), 80) or "unknown"
        reason = _clean_text(item.get("reason"), MAX_DEBUG_REASON_CHARS) or "unspecified"
        key = (stage, reason)
        bucket = counts.setdefault(key, {"stage": stage, "reason": reason, "count": 0})
        bucket["count"] += 1
    return sorted(
        counts.values(),
        key=lambda value: (-_positive_int(value.get("count")), str(value.get("stage")), str(value.get("reason"))),
    )


def internal_rejection_payload(item: dict) -> dict:
    source = item if isinstance(item, dict) else {}
    return {
        "stage": _clean_text(source.get("stage"), 80),
        "candidate_id": _clean_text(source.get("candidate_id"), 160),
        "reason": _clean_text(source.get("reason"), MAX_DEBUG_REASON_CHARS),
    }


def internal_candidate_summary(candidate: dict) -> dict:
    source = candidate if isinstance(candidate, dict) else {}
    evidence = source.get("evidence") if isinstance(source.get("evidence"), list) else []
    first_evidence = next((item for item in evidence if isinstance(item, dict)), {})
    summary = {
        "candidate_id": _clean_text(source.get("candidate_id"), 160),
        "unit_id": _clean_text(source.get("unit_id"), 160),
        "severity": _clean_text(source.get("severity"), 20),
        "category": _clean_text(source.get("category"), 80),
        "title": _clean_text(source.get("title"), 240),
    }
    if first_evidence:
        summary["primaryEvidence"] = {
            "file": safe_relative_path(first_evidence.get("file")) or "",
            "line": _clean_text(first_evidence.get("line"), 40),
            "symbol": _clean_text(first_evidence.get("symbol"), 160),
        }
    return summary

def init_project(checkout: Path) -> Path:
    root = checkout.resolve(strict=False) / ".codereview"
    ensure_dir(root)
    ensure_dir(root / "runs")
    path = root / "config.json"
    if not path.exists():
        write_json(
            path,
            {
                "mode": "standard",
                "scan": {"mode": "full-cached", "include_untracked": True, "fail_on_source_change": True},
                "codex": {"command": "codex", "model": "", "reasoning_effort": "high", "env": {}},
                "simple": {
                    "discovery_turns": 3,
                    "max_discovery_turns": 48,
                    "discovery_parallel": 0,
                    "verification_parallel": 0,
                    "subagents_per_turn": 3,
                    "max_candidates": 10,
                    "scan_deadline_seconds": 3600,
                    "max_batch_files": 120,
                    "max_batch_bytes": 1500000,
                },
            },
        )
    return root


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m codereview")
    sub = parser.add_subparsers(dest="command", required=True)
    init_parser = sub.add_parser("init")
    init_parser.add_argument("--checkout", default=".")
    run_parser = sub.add_parser("run")
    run_parser.add_argument("--checkout", default=".")
    run_parser.add_argument("--mode", choices=["fast", "standard", "deep"], default="")
    run_parser.add_argument("--scan-mode", choices=["full-cached", "full-strict"], default="")
    args = parser.parse_args(argv)
    checkout = Path(args.checkout)
    if args.command == "init":
        print(init_project(checkout))
        return 0
    try:
        final = run_review(checkout, mode=args.mode, scan_mode=args.scan_mode)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 5
    print(final)
    try:
        confirmed = json.loads(final.with_name("confirmed.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 5
    return 1 if isinstance(confirmed, list) and confirmed else 0


def _make_unit(sequence: int, area: str, items: list[dict]) -> ReviewUnit:
    paths = tuple(str(item.get("path") or "") for item in items if str(item.get("path") or ""))
    return ReviewUnit(
        unit_id=f"unit-{sequence:04d}",
        area=area,
        files=paths,
        size_bytes=sum(max(0, int(item.get("size_bytes") or 0)) for item in items),
        line_count=sum(max(0, int(item.get("line_count") or 0)) for item in items),
    )


def _area_for_path(path: str) -> str:
    parts = path.split("/")
    return parts[0] if len(parts) > 1 else "__root__"


def _candidate_sort_key(candidate: dict) -> tuple:
    return (
        -_SEVERITY_RANK.get(str(candidate.get("severity") or "info"), 0),
        -len(candidate.get("evidence") if isinstance(candidate.get("evidence"), list) else []),
        str(candidate.get("candidate_id") or ""),
    )


def _rejected_record(stage: str, candidate: dict, reason: str) -> dict:
    return {
        "stage": stage,
        "candidate_id": _clean_text(candidate.get("candidate_id") or candidate.get("title"), 160),
        "reason": _clean_text(reason, MAX_DEBUG_REASON_CHARS),
    }


def _command_cwd_is_safe(cwd: str, worker_repo: Path) -> bool:
    if not cwd:
        return False
    try:
        return is_within(Path(cwd), worker_repo)
    except (OSError, RuntimeError, ValueError):
        return False


def _output_excerpt(output: str, marker: str, *, limit: int = 1_200) -> str:
    text = str(output or "")
    index = text.find(marker)
    if index < 0:
        return text[-limit:]
    start = max(0, index - limit // 3)
    return text[start : start + limit]


def _normalize_command(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def _normalize_text(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def _clean_text(value: object, limit: int) -> str:
    text = str(value or "").replace("\x00", "").replace("\r", "").strip()
    return text[: max(0, limit)]


def _positive_int(value: object) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def _bounded_int(value: object, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(minimum, min(maximum, number))


def _looks_like_auth_error(value: object) -> bool:
    lowered = str(value or "").lower()
    return any(marker in lowered for marker in _AUTH_MARKERS)


def _looks_like_readiness_error(value: object) -> bool:
    lowered = str(value or "").lower()
    return (
        _looks_like_auth_error(lowered)
        or any(marker in lowered for marker in _QUOTA_MARKERS)
        or any(
            marker in lowered
            for marker in (
                "codex_auth_required",
                "codex_auth_expired",
                "codex_authorization_failed",
                "codex_subscription_inactive",
                "codex_quota_exhausted",
                "codex_version_unsupported",
            )
        )
    )


def _codex_error(prefix: str, detail: str) -> str:
    text = _clean_text(detail, 2_000)
    lowered = text.lower()
    if any(marker in lowered for marker in _AUTH_EXPIRED_MARKERS):
        return f"codex_auth_expired: {prefix}: {text}"
    if any(marker in lowered for marker in _AUTH_REQUIRED_MARKERS):
        return f"codex_auth_required: {prefix}: {text}"
    if any(marker in lowered for marker in _QUOTA_MARKERS):
        return f"codex_quota_exhausted: {prefix}: {text}"
    return f"{prefix}: {text}"


def _emit(
    progress: ProgressCallback | None,
    stage: str,
    message: str,
    *,
    current: int | None = None,
    total: int | None = None,
    run_id: str = "",
    extra: dict[str, object] | None = None,
) -> None:
    if progress is None:
        return
    payload: dict[str, object] = {"stage": stage, "message": message}
    if current is not None:
        payload["current"] = current
    if total is not None:
        payload["total"] = total
    if run_id:
        payload["runId"] = run_id
    if extra:
        payload.update(extra)
    try:
        progress(payload)
    except Exception as exc:
        raise_if_cancelled_callback_exception(exc)


if __name__ == "__main__":
    raise SystemExit(main())
