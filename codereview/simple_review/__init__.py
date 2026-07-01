from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import threading
import time
import uuid
from pathlib import Path
from types import ModuleType

_LEGACY_NAME = "codereview._simple_review_legacy"
_LEGACY_LOCK = threading.Lock()
_LEGACY: ModuleType | None = None
ENGINE_VERSION = "simple-full-repository/2"
_RISK_WORDS = (
    "auth",
    "token",
    "quota",
    "worker",
    "job",
    "claim",
    "result",
    "upload",
    "progress",
    "cancel",
    "thread",
    "lock",
    "timeout",
    "retry",
    "sandbox",
    "repro",
    "verification",
    "evidence",
    "symlink",
    "cleanup",
)


def _legacy() -> ModuleType:
    global _LEGACY
    with _LEGACY_LOCK:
        if _LEGACY is not None:
            return _LEGACY
        existing = sys.modules.get(_LEGACY_NAME)
        if existing is not None:
            _LEGACY = existing
            return existing
        path = Path(__file__).resolve().parents[1] / "simple_review.py"
        spec = importlib.util.spec_from_file_location(_LEGACY_NAME, path)
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot load legacy simple review from {path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[_LEGACY_NAME] = module
        spec.loader.exec_module(module)
        _LEGACY = module
        return module


def run_review(checkout: Path, mode: str = "", scan_mode: str = "", progress=None) -> Path:
    s = _legacy()
    checkout = checkout.resolve(strict=False)
    config = s.load_config(checkout, mode=mode, scan_mode=scan_mode)
    raw_config = s.read_json(checkout / ".codereview" / "config.json", default={})
    raw_config = raw_config if isinstance(raw_config, dict) else {}
    settings = s.load_simple_settings(raw_config, config)
    metrics = s.TurnMetrics(input_limit_chars=max(0, int(config.codex.max_input_chars or 0)))
    deadline_at = time.monotonic() + settings.scan_deadline_seconds if settings.scan_deadline_seconds > 0 else 0.0
    run_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    run = checkout / ".codereview" / "runs" / run_id
    s.ensure_dir(run)
    s.write_json(run / "meta.json", {"run_id": run_id, "engine": ENGINE_VERSION, "mode": config.mode, "scan_mode": config.scan.mode, "scope": "full-repository"})
    s._emit(progress, "setup", "Preparing simple full-repository review", run_id=run_id)

    s._emit(progress, "inventory", "Building full repository inventory", run_id=run_id)
    inventory = s.build_git_inventory(checkout, include_untracked=config.scan.include_untracked, max_text_file_bytes=config.scope.max_text_file_bytes)
    inventory_files = s.analyzable_files(inventory)
    if not inventory_files:
        return s._write_reports(run, mode=config.mode, scan_mode=config.scan.mode, inventory=inventory, units=[], discovery_results=[], raw_candidates=[], valid_candidates=[], selected_candidates=[], confirmed=[], rejected=[], account={}, progress=progress, turn_metrics=metrics)
    source_before = s.source_state_from_inventory(inventory)
    s.write_json(run / "inventory.json", inventory)
    s.write_json(run / "source-state-before.json", source_before)

    s._emit(progress, "snapshot", "Creating immutable full-repository snapshot", run_id=run_id)
    snapshot_manifest = s.create_immutable_snapshot(checkout, inventory, run)
    snapshot_repo = Path(str(snapshot_manifest["snapshot_repo"]))
    s.write_json(run / "snapshot.json", snapshot_manifest)
    s.write_json(run / "snapshot-source-state-before.json", source_before)
    account = s.codex_account_preflight(config.codex, snapshot_repo)
    s.write_json(run / "codex-account.json", account)
    settings = s.tune_simple_parallelism(settings, inventory, account)

    inventory_by_path = {str(item.get("path") or ""): item for item in inventory_files if isinstance(item, dict) and str(item.get("path") or "")}
    units = s.plan_review_units(inventory_files, max_files=settings.max_unit_files, max_bytes=settings.max_unit_bytes)
    s.validate_unit_coverage(units, set(inventory_by_path))
    s.write_jsonl(run / "units.jsonl", [unit.to_dict(inventory_by_path) for unit in units])
    batches = s.plan_discovery_batches(units, target_turns=settings.discovery_turns, max_turns=settings.max_discovery_turns, max_batch_files=settings.max_batch_files, max_batch_bytes=settings.max_batch_bytes, subagents_per_turn=settings.subagents_per_turn)
    batches = sorted(batches, key=_batch_sort_key)
    s.write_json(run / "coverage-planned.json", {"scope": "full-repository", "files": len(inventory_by_path), "units": len(units), "discovery_turns": len(batches), "complete": True})

    s._emit(progress, "finder", f"Discovery 0/{len(batches)}", current=0, total=len(batches), run_id=run_id)
    discovery_results = s._run_discovery(snapshot_repo, run, batches, units, inventory_by_path, config.codex, settings, progress, run_id, metrics)
    raw_candidates = [item for result in discovery_results for item in (result.get("candidates") if isinstance(result.get("candidates"), list) else []) if isinstance(item, dict)]
    s.write_jsonl(run / "candidates" / "raw.jsonl", raw_candidates)

    unit_by_id = {unit.unit_id: unit for unit in units}
    valid_candidates = []
    rejected = []
    for raw in raw_candidates:
        try:
            valid_candidates.append(s.normalize_candidate(raw, unit_by_id, inventory_by_path))
        except s.CandidateRejected as exc:
            rejected.append(s._rejected_record("discovery", raw, str(exc)))
    unit_limited, unit_rejections = s.limit_candidates_per_unit(valid_candidates, settings.max_candidates_per_unit)
    rejected.extend(unit_rejections)
    deduped, duplicate_rejections = s.dedupe_candidates(unit_limited)
    rejected.extend(duplicate_rejections)
    selected_candidates, budget_rejections = _select_candidates(deduped, settings.max_candidates)
    rejected.extend(budget_rejections)
    s.write_jsonl(run / "candidates" / "valid.jsonl", valid_candidates)
    s.write_jsonl(run / "candidates" / "selected.jsonl", selected_candidates)
    s._emit(progress, "candidates", f"Candidates: {len(raw_candidates)} raw, {len(selected_candidates)} selected", run_id=run_id, extra={"candidateCount": len(selected_candidates)})

    stop_event = threading.Event()
    s._emit(progress, "verification", f"Verification 0/{len(selected_candidates)}", current=0, total=len(selected_candidates), run_id=run_id)
    verification_results = s._run_verifications(snapshot_repo, run, selected_candidates, config.codex, settings, stop_event, progress, run_id, deadline_at, metrics)
    confirmed = []
    for result in verification_results:
        if result.get("confirmed") and isinstance(result.get("item"), dict):
            confirmed.append(result["item"])
        else:
            item = {"stage": "verification", "candidate_id": str(result.get("candidate_id") or ""), "reason": s._clean_text(result.get("reason"), s.MAX_DEBUG_REASON_CHARS)}
            if result.get("blocked") is True:
                item["blocked"] = True
            rejected.append(item)

    snapshot_after = s.capture_source_state(snapshot_repo, include_untracked=True, max_text_file_bytes=config.scope.max_text_file_bytes)
    s.write_json(run / "snapshot-source-state-after.json", snapshot_after)
    if s.source_state_changed(source_before, snapshot_after):
        raise RuntimeError("immutable snapshot changed during simple full-repository review")
    source_after = s.capture_source_state(checkout, include_untracked=config.scan.include_untracked, max_text_file_bytes=config.scope.max_text_file_bytes)
    s.write_json(run / "source-state-after.json", source_after)
    if config.scan.fail_on_source_change and s.source_state_changed(source_before, source_after):
        raise RuntimeError("source checkout changed during simple full-repository review")

    final = s._write_reports(run, mode=config.mode, scan_mode=config.scan.mode, inventory=inventory, units=units, discovery_results=discovery_results, raw_candidates=raw_candidates, valid_candidates=valid_candidates, selected_candidates=selected_candidates, confirmed=confirmed, rejected=rejected, account=account, progress=progress, turn_metrics=metrics)
    _patch_engine_summary(final)
    return final


def _path_risk(path: object) -> int:
    text = str(path or "").lower().replace("-", "_").replace(".", "_")
    score = sum(1 for word in _RISK_WORDS if word in text)
    if text.startswith("tests/") or "/tests/" in text:
        score -= 1
    if text.endswith((".md", ".txt")):
        score -= 1
    return max(0, score)


def _batch_sort_key(batch: object) -> tuple[int, str]:
    score = 0
    for unit in getattr(batch, "units", ()):
        for path in getattr(unit, "files", ()):
            score = max(score, _path_risk(path))
    return (-score, str(getattr(batch, "batch_id", "")))


def _candidate_score(candidate: dict) -> tuple[int, tuple[str, ...]]:
    severity_rank = {"critical": 5, "high": 4, "medium": 3, "low": 2, "info": 1}
    score = severity_rank.get(str(candidate.get("severity") or "info").lower(), 1) * 100
    reasons = ["severity"]
    evidence = candidate.get("evidence") if isinstance(candidate.get("evidence"), list) else []
    files = [str(item.get("file") or "") for item in evidence if isinstance(item, dict) and item.get("file")]
    if files:
        score += min(30, len(set(files)) * 5)
        reasons.append("file-evidence")
    risk = max([_path_risk(path) for path in files] or [0])
    if risk:
        score += risk * 8
        reasons.append("risk-path")
    if str(candidate.get("expected_behavior_source") or "").strip():
        score += 20
        reasons.append("expected-source")
    if str(candidate.get("reproduction_idea") or candidate.get("minimal_repro_idea") or "").strip():
        score += 10
        reasons.append("repro-idea")
    return score, tuple(reasons)


def _select_candidates(candidates: list[dict], limit: int) -> tuple[list[dict], list[dict]]:
    scored = []
    for candidate in candidates:
        score, reasons = _candidate_score(candidate)
        enriched = dict(candidate)
        enriched.setdefault("selection_score", score)
        enriched.setdefault("selection_reasons", list(reasons))
        scored.append((score, str(candidate.get("candidate_id") or ""), enriched))
    ordered = [item[2] for item in sorted(scored, key=lambda item: (-item[0], item[1]))]
    selected = ordered[: max(0, int(limit or 0))]
    rejected = [{"stage": "budget", "candidate_id": str(candidate.get("candidate_id") or ""), "reason": "verification budget exhausted after simple scoring", "score": candidate.get("selection_score", 0)} for candidate in ordered[len(selected) :]]
    return selected, rejected


def _patch_engine_summary(final_path: Path) -> None:
    s = _legacy()
    summary_path = final_path.parent / "summary.json"
    summary = s.read_json(summary_path, default={})
    if not isinstance(summary, dict):
        return
    engine = summary.get("engine") if isinstance(summary.get("engine"), dict) else {}
    engine["version"] = ENGINE_VERSION
    engine["shape"] = "single-simple-engine"
    engine["batchRiskOrdering"] = True
    engine["candidateScoring"] = True
    summary["engine"] = engine
    s.write_json(summary_path, summary)


def init_project(checkout: Path) -> Path:
    return _legacy().init_project(checkout)


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


_legacy_module = _legacy()
for _name, _value in vars(_legacy_module).items():
    if _name != "__version__" and _name.startswith("__") and _name.endswith("__"):
        continue
    globals().setdefault(_name, _value)

globals()["ENGINE_VERSION"] = ENGINE_VERSION
globals()["run_review"] = run_review
globals()["init_project"] = init_project
globals()["main"] = main

__all__ = [name for name in globals() if name == "__version__" or not (name.startswith("__") and name.endswith("__"))]
