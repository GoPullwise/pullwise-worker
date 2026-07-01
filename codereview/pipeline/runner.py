from __future__ import annotations

import os
import time
import uuid
from dataclasses import replace
from pathlib import Path

from codereview.config import ReviewConfig, load_config
from codereview.inventory.git_inventory import analyzable_files, build_git_inventory
from codereview.snapshot import capture_source_state, create_immutable_snapshot, source_state_changed, source_state_from_inventory
from codereview.utils.jsonl import read_json, write_json, write_jsonl
from codereview.utils.paths import ensure_dir

from .cache import PipelineCache
from .candidates import rank_candidates, select_candidates_for_verification
from .legacy import get_legacy_simple_review
from .repo_profile import RepoProfile, build_repo_profile, repo_profile_from_json

ENGINE_VERSION = "adaptive-full-repository/2"


def run_review(checkout: Path, mode: str = "", scan_mode: str = "", progress=None) -> Path:
    return ReviewPipeline().run(checkout, mode=mode, scan_mode=scan_mode, progress=progress)


class ReviewPipeline:
    """Adaptive full-repository review pipeline.

    The public graphVerifiedReport/1 artifact contract stays the same. Internally
    the review now has deterministic stages for inventory, immutable snapshot,
    repository risk profile, bounded discovery, candidate scoring, strict
    verification, and report assembly.
    """

    def run(self, checkout: Path, mode: str = "", scan_mode: str = "", progress=None) -> Path:
        legacy = get_legacy_simple_review()
        checkout = checkout.resolve(strict=False)
        config = load_config(checkout, mode=mode, scan_mode=scan_mode)
        raw_config = read_json(checkout / ".codereview" / "config.json", default={})
        raw_config = raw_config if isinstance(raw_config, dict) else {}
        settings = legacy.load_simple_settings(raw_config, config)
        turn_metrics = legacy.TurnMetrics(input_limit_chars=max(0, int(config.codex.max_input_chars or 0)))
        deadline_at = time.monotonic() + settings.scan_deadline_seconds if settings.scan_deadline_seconds > 0 else 0.0
        run_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        run = checkout / ".codereview" / "runs" / run_id
        ensure_dir(run)
        write_json(
            run / "meta.json",
            {
                "run_id": run_id,
                "engine": ENGINE_VERSION,
                "legacy_engine": getattr(legacy, "ENGINE_VERSION", "simple-full-repository/1"),
                "mode": config.mode,
                "scan_mode": config.scan.mode,
                "scope": "full-repository",
            },
        )
        legacy._emit(progress, "setup", "Preparing adaptive full-repository review", run_id=run_id)

        legacy._emit(progress, "inventory", "Building full repository inventory", run_id=run_id)
        inventory = build_git_inventory(
            checkout,
            include_untracked=config.scan.include_untracked,
            max_text_file_bytes=config.scope.max_text_file_bytes,
        )
        inventory_files = analyzable_files(inventory)
        if not inventory_files:
            return legacy._write_reports(
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
                turn_metrics=turn_metrics,
            )

        source_state_before = source_state_from_inventory(inventory)
        write_json(run / "inventory.json", inventory)
        write_json(run / "source-state-before.json", source_state_before)

        cache = PipelineCache.from_checkout(checkout)
        cache_key = cache.key(
            engine=ENGINE_VERSION,
            source_state=source_state_before,
            mode=config.mode,
            scan_mode=config.scan.mode,
            config=_cache_relevant_config(raw_config),
        )
        repo_profile = _cached_or_build_repo_profile(cache, cache_key, checkout, inventory_files)
        write_json(run / "repo-profile.json", repo_profile.to_dict())

        legacy._emit(progress, "snapshot", "Creating immutable full-repository snapshot", run_id=run_id)
        snapshot_manifest = create_immutable_snapshot(checkout, inventory, run)
        snapshot_repo = Path(str(snapshot_manifest["snapshot_repo"]))
        snapshot_state_before = source_state_before
        write_json(run / "snapshot.json", snapshot_manifest)
        write_json(run / "snapshot-source-state-before.json", snapshot_state_before)

        account = legacy.codex_account_preflight(config.codex, snapshot_repo)
        write_json(run / "codex-account.json", account)
        settings = legacy.tune_simple_parallelism(settings, inventory, account)
        settings = _tune_settings_for_profile(settings, repo_profile)

        inventory_by_path = {
            str(item.get("path") or ""): item
            for item in inventory_files
            if isinstance(item, dict) and str(item.get("path") or "")
        }
        units = legacy.plan_review_units(inventory_files, max_files=settings.max_unit_files, max_bytes=settings.max_unit_bytes)
        legacy.validate_unit_coverage(units, set(inventory_by_path))
        write_jsonl(run / "units.jsonl", [unit.to_dict(inventory_by_path) for unit in units])

        batches = legacy.plan_discovery_batches(
            units,
            target_turns=settings.discovery_turns,
            max_turns=settings.max_discovery_turns,
            max_batch_files=settings.max_batch_files,
            max_batch_bytes=settings.max_batch_bytes,
            subagents_per_turn=settings.subagents_per_turn,
        )
        batches = _prioritize_batches(batches, repo_profile)
        write_json(
            run / "coverage-planned.json",
            {
                "scope": "full-repository",
                "files": len(inventory_by_path),
                "units": len(units),
                "discovery_turns": len(batches),
                "complete": True,
                "profileRiskAreas": list(repo_profile.risk_areas),
            },
        )

        legacy._emit(progress, "finder", f"Discovery 0/{len(batches)}", current=0, total=len(batches), run_id=run_id)
        discovery_results = legacy._run_discovery(
            snapshot_repo,
            run,
            batches,
            units,
            inventory_by_path,
            config.codex,
            settings,
            progress,
            run_id,
            turn_metrics,
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
                valid_candidates.append(legacy.normalize_candidate(raw_candidate, unit_by_id, inventory_by_path))
            except legacy.CandidateRejected as exc:
                rejected.append(legacy._rejected_record("discovery", raw_candidate, str(exc)))

        unit_limited, unit_budget_rejections = legacy.limit_candidates_per_unit(valid_candidates, settings.max_candidates_per_unit)
        rejected.extend(unit_budget_rejections)
        deduped, duplicate_rejections = legacy.dedupe_candidates(unit_limited)
        rejected.extend(duplicate_rejections)
        scored = rank_candidates(deduped, repo_profile)
        selected_candidates, score_rejections, score_summaries = select_candidates_for_verification(
            scored,
            limit=settings.max_candidates,
            min_score=max(0, int(config.scoring.min_score_for_repro or 0)),
            always_repro_severities=config.scoring.always_repro_severities,
        )
        rejected.extend(score_rejections)
        write_jsonl(run / "candidates" / "valid.jsonl", valid_candidates)
        write_jsonl(run / "candidates" / "selected.jsonl", selected_candidates)
        write_json(run / "candidates" / "scores.json", score_summaries)
        cache.write_json(cache_key, "repo-profile.json", repo_profile.to_dict())
        cache.write_json(cache_key, "candidate-scores.json", score_summaries)

        legacy._emit(
            progress,
            "candidates",
            f"Candidates: {len(raw_candidates)} raw, {len(selected_candidates)} selected by adaptive score",
            run_id=run_id,
            extra={"candidateCount": len(selected_candidates), "scoredCandidateCount": len(scored)},
        )

        stop_event = legacy.threading.Event()
        legacy._emit(
            progress,
            "verification",
            f"Verification 0/{len(selected_candidates)}",
            current=0,
            total=len(selected_candidates),
            run_id=run_id,
        )
        verification_results = legacy._run_verifications(
            snapshot_repo,
            run,
            selected_candidates,
            config.codex,
            settings,
            stop_event,
            progress,
            run_id,
            deadline_at,
            turn_metrics,
        )
        confirmed: list[dict] = []
        for result in verification_results:
            if result.get("confirmed") and isinstance(result.get("item"), dict):
                confirmed.append(result["item"])
            else:
                rejected_record = {
                    "stage": "verification",
                    "candidate_id": str(result.get("candidate_id") or ""),
                    "reason": legacy._clean_text(result.get("reason"), legacy.MAX_DEBUG_REASON_CHARS),
                }
                if result.get("blocked") is True:
                    rejected_record["blocked"] = True
                rejected.append(rejected_record)

        snapshot_state_after = capture_source_state(snapshot_repo, include_untracked=True, max_text_file_bytes=config.scope.max_text_file_bytes)
        write_json(run / "snapshot-source-state-after.json", snapshot_state_after)
        if source_state_changed(snapshot_state_before, snapshot_state_after):
            raise RuntimeError("immutable snapshot changed during adaptive full-repository review")
        source_state_after = capture_source_state(checkout, include_untracked=config.scan.include_untracked, max_text_file_bytes=config.scope.max_text_file_bytes)
        write_json(run / "source-state-after.json", source_state_after)
        if config.scan.fail_on_source_change and source_state_changed(source_state_before, source_state_after):
            raise RuntimeError("source checkout changed during adaptive full-repository review")

        final_path = legacy._write_reports(
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
            turn_metrics=turn_metrics,
        )
        _patch_summary_with_adaptive_metadata(final_path, repo_profile, score_summaries, cache)
        return final_path


def _cached_or_build_repo_profile(cache: PipelineCache, key: str, checkout: Path, inventory_files: list[dict]) -> RepoProfile:
    cached = repo_profile_from_json(cache.read_json(key, "repo-profile.json", default=None))
    if cached is not None:
        return cached
    return build_repo_profile(checkout, inventory_files)


def _tune_settings_for_profile(settings, profile: RepoProfile):
    high_risk_count = len(profile.high_risk_paths)
    if profile.file_count < 80 and high_risk_count < 8:
        return replace(
            settings,
            discovery_parallel=max(1, min(settings.discovery_parallel, 2)),
            verification_parallel=max(1, min(settings.verification_parallel, 2)),
        )
    return settings


def _prioritize_batches(batches: list, profile: RepoProfile) -> list:
    def batch_score(batch) -> tuple[int, str]:
        score = 0
        for unit in getattr(batch, "units", ()):
            for path in getattr(unit, "files", ()):  # ReviewUnit.files
                score = max(score, profile.risk_for_path(path))
        return (-score, str(getattr(batch, "batch_id", "")))

    return sorted(batches, key=batch_score)


def _cache_relevant_config(raw_config: dict) -> dict:
    simple = raw_config.get("simple") if isinstance(raw_config.get("simple"), dict) else {}
    scoring = raw_config.get("scoring") if isinstance(raw_config.get("scoring"), dict) else {}
    return {
        "simple": {
            key: simple.get(key)
            for key in (
                "max_unit_files",
                "max_unit_bytes",
                "max_batch_files",
                "max_batch_bytes",
                "max_candidates",
                "max_candidates_per_unit",
            )
            if key in simple
        },
        "scoring": scoring,
    }


def _patch_summary_with_adaptive_metadata(final_path: Path, profile: RepoProfile, score_summaries: list[dict], cache: PipelineCache) -> None:
    summary_path = final_path.parent / "summary.json"
    summary = read_json(summary_path, default={})
    if not isinstance(summary, dict):
        return
    engine = summary.get("engine") if isinstance(summary.get("engine"), dict) else {}
    engine.update(
        {
            "version": ENGINE_VERSION,
            "legacyCompatibility": "graphVerifiedReport/1",
            "riskProfile": True,
            "candidateScoring": True,
            "cacheEnabled": cache.enabled,
            "cacheRoot": str(cache.root),
        }
    )
    summary["engine"] = engine
    summary["repositoryProfile"] = {
        "languages": list(profile.languages),
        "riskAreas": list(profile.risk_areas),
        "highRiskPathCount": len(profile.high_risk_paths),
        "entrypoints": list(profile.entrypoints[:20]),
    }
    summary["candidateScoring"] = {
        "scored": len(score_summaries),
        "top": score_summaries[:20],
    }
    write_json(summary_path, summary)
