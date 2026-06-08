from __future__ import annotations

# Loaded by main.py; keep definitions in that module's globals for compatibility.

_REVIEW_VERIFIED_STATUSES = {"verified", "static_proof"}
_REVIEW_DECISIONS = {"reported", "audit_only", "rejected"}
_REVIEW_MODE_ORDER = {"off": 0, "shadow": 1, "audit_only": 2, "enforce": 3}
_REVIEW_PRIOR_ALPHA = 3.0
_REVIEW_PRIOR_BETA = 2.0
_REVIEW_SOURCE_FACTOR_MIN = 0.65
_REVIEW_SOURCE_FACTOR_MAX = 1.15
_REVIEW_POTENTIAL_RISK_REPORT_THRESHOLD = 0.82
_REVIEW_POTENTIAL_RISK_AUDIT_THRESHOLD = 0.65
_REVIEW_UNVERIFIED_AUDIT_THRESHOLD = 0.60
_REVIEW_PROMPT_VERSION = "pullwise-review-prompt/0.1"
_REVIEW_VERIFIER_VERSION = "pullwise-worker-verifier/0.1"
_REVIEW_STATIC_CHECKER_VERSION = "pullwise-static-checker/0.1"
_REVIEW_HARD_REJECT_REASONS = {
    "invalid_candidate",
    "missing_title",
    "missing_evidence",
    "missing_false_positive_check",
    "invalid_candidate_location",
    "invalid_location",
    "verifier_explicitly_rejected",
    "verifier_explicit_rejection",
}
_REVIEW_DELTA_EXCLUSION_REASONS = {"not_introduced_by_current_delta", "stale_previous_location"}


def review_text(value: object, limit: int = 160) -> str:
    text = re.sub(r"\s+", " ", str(value or "").strip())
    return text[:limit]


def review_git_sha(value: object) -> str:
    text = review_text(value, 80).lower()
    return text if re.fullmatch(r"[0-9a-f]{40}", text) else ""


def review_probability(value: object) -> float:
    if isinstance(value, bool):
        return 0.0
    try:
        number = float(value or 0.0)
    except (OverflowError, TypeError, ValueError):
        return 0.0
    if not math.isfinite(number):
        return 0.0
    return max(0.0, min(1.0, number))


def review_positive_float(value: object) -> float:
    if isinstance(value, bool):
        return 0.0
    try:
        number = float(value or 0.0)
    except (OverflowError, TypeError, ValueError):
        return 0.0
    if not math.isfinite(number):
        return 0.0
    return max(0.0, number)


def review_status(finding: dict) -> str:
    status = str(finding.get("verificationStatus") or "").strip().lower()
    return status if status in _VERIFICATION_STATUSES else "potential_risk"


def review_severity(finding: dict) -> str:
    severity = str(finding.get("severity") or "").strip().lower()
    aliases = {"p0": "critical", "p1": "high", "p2": "medium", "p3": "low", "p4": "info"}
    severity = aliases.get(severity, severity)
    return severity if severity in {"critical", "high", "medium", "low", "info"} else "medium"


def review_candidate_id(finding: dict, fingerprint: str) -> str:
    candidate_id = review_text(
        finding.get("id") or finding.get("issue_id") or finding.get("issueId") or finding.get("candidate_id"),
        120,
    )
    return candidate_id or fingerprint[:32]


def review_line_end(finding: dict) -> int:
    for item in finding.get("affectedLocations") if isinstance(finding.get("affectedLocations"), list) else []:
        if not isinstance(item, dict):
            continue
        line = positive_int(item.get("endLine") or item.get("line") or item.get("startLine"))
        if line:
            return line
    return finding_primary_line(finding)


def review_candidate_features(finding: dict, record: dict) -> dict:
    fingerprint = review_text(record.get("fingerprint"), 96) or finding_fingerprint(finding)
    status = review_status(finding)
    return {
        "candidate_id": review_candidate_id(finding, fingerprint),
        "fingerprint": fingerprint,
        "source": finding_source(finding),
        "category": review_text(finding.get("category") or "Quality", 80),
        "severity": review_severity(finding),
        "verification_status": status,
        "raw_confidence": review_probability(finding.get("confidence")),
        "file_path": finding_primary_file(finding),
        "line_start": finding_primary_line(finding),
        "line_end": review_line_end(finding),
        "has_repro_command": finding_reproduction_evidence(finding),
        "has_runtime_log": any(
            isinstance(item, dict) and evidence_log_path_is_structured(item.get("logPath") or item.get("log_path"))
            for item in (finding.get("evidence") if isinstance(finding.get("evidence"), list) else [])
        ),
        "has_static_proof": status == "static_proof",
        "has_exact_location": finding_precise_location(finding),
        "false_positive_checks_passed": finding_has_false_positive_check(finding),
        "normalized_title": normalized_fingerprint_text(finding.get("title"))[:180],
    }


def review_beta_lower_bound(alpha: float, beta: float, *, z: float) -> float:
    total = alpha + beta
    if total <= 0:
        return 0.0
    mean = alpha / total
    variance = alpha * beta / ((total * total) * (total + 1))
    return max(0.0, min(1.0, mean - z * (variance ** 0.5)))


def review_source_reliability_from_counts(source_stats: dict, *, z: float) -> dict:
    confirmed = source_stat_count(source_stats, "confirmed")
    rejected = source_stat_count(source_stats, "rejected")
    alpha = _REVIEW_PRIOR_ALPHA + confirmed
    beta = _REVIEW_PRIOR_BETA + rejected
    total = alpha + beta
    return {
        "posterior_alpha": alpha,
        "posterior_beta": beta,
        "posterior_mean": alpha / total,
        "posterior_lb": review_beta_lower_bound(alpha, beta, z=z),
        "effective_samples": float(confirmed + rejected),
        "source": "legacy_source_stats" if confirmed or rejected else "prior",
    }


def review_calibration_context_for_job(job: dict) -> dict:
    context = job.get("review_calibration_context") if isinstance(job.get("review_calibration_context"), dict) else {}
    if str(context.get("protocol") or "").strip() != REVIEW_CALIBRATION_PROTOCOL_VERSION:
        return {}
    return context


def review_provider_model_keys(config: WorkerConfig) -> tuple[str, str]:
    provider_chain = list(getattr(config, "provider_chain", []) or [])
    provider = provider_chain[0] if provider_chain else getattr(config, "provider", "")
    model = getattr(config, "codex_model", "") if provider == "codex" else getattr(config, "opencode_model", "")
    return normalized_source_key(provider), normalized_source_key(model)


def review_context_cohort_candidates(features: dict, config: WorkerConfig, *, include_status_global: bool = False) -> list[str]:
    source = normalized_source_key(features.get("source"))
    category = normalized_source_key(features.get("category"))
    status = str(features.get("verification_status") or "").lower()
    provider, model = review_provider_model_keys(config)
    candidates: list[str] = []

    def append(key: str) -> None:
        if key and key not in candidates:
            candidates.append(key)

    if provider and model and source:
        if category and status:
            append(f"provider:{provider}|model:{model}|source:{source}|category:{category}|status:{status}")
        if category:
            append(f"provider:{provider}|model:{model}|source:{source}|category:{category}")
        if status:
            append(f"provider:{provider}|model:{model}|source:{source}|status:{status}")
        append(f"provider:{provider}|model:{model}|source:{source}")
    if provider and model:
        append(f"provider:{provider}|model:{model}")
    if provider:
        append(f"provider:{provider}")
    if source:
        if category and status:
            append(f"source:{source}|category:{category}|status:{status}")
        if category:
            append(f"source:{source}|category:{category}")
        if status:
            append(f"source:{source}|status:{status}")
        append(f"source:{source}")
        append(source)
    if include_status_global:
        if status:
            append(f"status:{status}")
        append("global")
    return candidates


def review_context_reliability(features: dict, job: dict, config: WorkerConfig) -> dict:
    context = review_calibration_context_for_job(job)
    buckets = context.get("source_reliability") if isinstance(context.get("source_reliability"), dict) else {}
    if not buckets:
        return {}
    for key in review_context_cohort_candidates(features, config):
        item = buckets.get(key)
        if not isinstance(item, dict):
            continue
        lb = review_probability(item.get("posterior_lb") or item.get("posteriorLb"))
        mean = review_probability(item.get("posterior_mean") or item.get("posteriorMean"))
        effective = review_positive_float(item.get("effective_samples") or item.get("effectiveSamples"))
        if effective < getattr(config, "review_calibration_min_effective_samples", 20):
            continue
        if lb and mean:
            return {
                "posterior_mean": mean,
                "posterior_lb": lb,
                "effective_samples": effective,
                "source": "review_calibration_context",
                "cohort_key": key,
            }
    return {}


def review_drift_state_for_features(features: dict, job: dict, config: WorkerConfig) -> str:
    if not getattr(config, "review_calibration_enable_drift", False):
        return "normal"
    context = review_calibration_context_for_job(job)
    drift = context.get("drift_state") if isinstance(context.get("drift_state"), dict) else {}
    for key in review_context_cohort_candidates(features, config, include_status_global=True):
        state = str(drift.get(key) or "").strip().lower()
        if state in {"normal", "watch", "audit_only", "suspended"}:
            return state
    return "normal"


def review_source_factor(reliability: dict) -> float:
    prior_lb = review_beta_lower_bound(_REVIEW_PRIOR_ALPHA, _REVIEW_PRIOR_BETA, z=1.0)
    source_lb = review_probability(reliability.get("posterior_lb")) or prior_lb
    if prior_lb <= 0:
        return 1.0
    return max(_REVIEW_SOURCE_FACTOR_MIN, min(_REVIEW_SOURCE_FACTOR_MAX, source_lb / prior_lb))


def review_sample_rate(config: WorkerConfig) -> float:
    rate = review_positive_float(getattr(config, "review_calibration_sample_audit_rate", 0.0))
    return max(0.0, min(1.0, rate))


def review_sample_selected(config: WorkerConfig, job: dict, features: dict) -> tuple[bool, float]:
    rate = review_sample_rate(config)
    if rate <= 0:
        return False, 0.0
    if rate >= 1:
        return True, 1.0
    sample_key = review_hash(
        "manual_review_sample",
        job.get("user_id"),
        review_scope_repo_key(job),
        job.get("branch") or "main",
        features.get("candidate_id"),
        features.get("fingerprint"),
        features.get("verification_status"),
    )
    bucket = int(sample_key[:12], 16) / float(0xFFFFFFFFFFFF)
    return bucket < rate, rate


def review_score_band(score: dict) -> str:
    decision_score = review_probability(score.get("decision_score"))
    if decision_score >= _REVIEW_POTENTIAL_RISK_REPORT_THRESHOLD:
        return "report_band"
    if decision_score >= _REVIEW_POTENTIAL_RISK_AUDIT_THRESHOLD:
        return "audit_band"
    return "reject_band"


def review_manual_sample_metadata(
    *,
    config: WorkerConfig,
    job: dict,
    features: dict,
    score: dict,
    decision: str,
) -> dict:
    selected, rate = review_sample_selected(config, job, features)
    if not selected:
        return {}
    score_kind = "truth_probability" if score.get("truth_probability") is not None else "ranking_score"
    return {
        "sampledForManualReview": True,
        "sampleReason": f"calibration_sample_{decision}",
        "sampleRate": rate,
        "scoreBand": review_score_band(score),
        "scoreKind": score_kind,
        "decisionScore": review_probability(score.get("decision_score")),
        "cohortKey": score.get("cohort_key") or "",
    }


def review_logit(value: float) -> float:
    probability = max(0.01, min(0.99, review_probability(value)))
    return math.log(probability / (1.0 - probability))


def review_sigmoid(value: float) -> float:
    if value >= 0:
        z = math.exp(-value)
        return 1.0 / (1.0 + z)
    z = math.exp(value)
    return z / (1.0 + z)


def review_source_logit_adjustment(reliability: dict) -> float:
    prior_lb = review_beta_lower_bound(_REVIEW_PRIOR_ALPHA, _REVIEW_PRIOR_BETA, z=1.0)
    source_lb = review_probability(reliability.get("posterior_lb")) or prior_lb
    raw = review_logit(source_lb) - review_logit(prior_lb)
    return max(-0.70, min(0.40, raw))


def review_factor_log_adjustment(value: float, *, lower: float = 0.20, upper: float = 1.20) -> float:
    factor = max(lower, min(upper, float(value or 0.0)))
    return math.log(factor)


def review_verification_logit_adjustment(status: str) -> float:
    return {
        "verified": 0.35,
        "static_proof": 0.30,
        "potential_risk": 0.0,
        "unverified": -0.35,
    }.get(status, 0.0)


def review_calibrated_confidence(features: dict, job: dict, config: WorkerConfig) -> float:
    raw = review_probability(features.get("raw_confidence"))
    status = str(features.get("verification_status") or "potential_risk")
    caps = {
        "verified": 0.98,
        "static_proof": 0.97,
        "potential_risk": 0.90,
        "unverified": 0.75,
    }
    capped = min(raw, caps.get(status, 0.90))
    if not getattr(config, "review_calibration_enable_buckets", False):
        return capped
    context = review_calibration_context_for_job(job)
    buckets_by_cohort = context.get("confidence_calibration") if isinstance(context.get("confidence_calibration"), dict) else {}
    for cohort_key in review_context_cohort_candidates(features, config, include_status_global=True):
        buckets = buckets_by_cohort.get(cohort_key)
        if not isinstance(buckets, dict):
            continue
        for raw_range, bucket in buckets.items():
            if not isinstance(bucket, dict):
                continue
            parts = str(raw_range).split("-")
            if len(parts) != 2:
                continue
            try:
                lower = float(parts[0])
                upper = float(parts[1])
            except ValueError:
                continue
            effective = review_positive_float(bucket.get("labeled_weight") or bucket.get("labeledWeight"))
            precision = review_probability(bucket.get("precision") or bucket.get("bucket_precision") or bucket.get("bucketPrecision"))
            if lower <= raw <= upper and effective >= getattr(config, "review_calibration_min_effective_samples", 20) and precision:
                return max(0.0, min(caps.get(status, 0.90), (0.5 * raw) + (0.5 * precision)))
    return capped


def review_evidence_strength(features: dict) -> float:
    status = str(features.get("verification_status") or "")
    if status in _REVIEW_VERIFIED_STATUSES:
        return 1.0
    if features.get("has_runtime_log") and features.get("false_positive_checks_passed"):
        return 0.96
    if features.get("has_repro_command") and features.get("false_positive_checks_passed"):
        return 0.92
    if features.get("has_exact_location") and features.get("false_positive_checks_passed"):
        return 0.86
    if features.get("has_repro_command") or features.get("has_runtime_log"):
        return 0.74
    if features.get("has_exact_location"):
        return 0.66
    return 0.50


def review_delta_relevance(record: dict) -> float:
    reason = str(record.get("reason") or "").strip()
    if reason == "not_introduced_by_current_delta":
        return 0.30
    if reason == "stale_previous_location":
        return 0.20
    return 1.0


def review_category_factor(features: dict) -> float:
    category = normalized_source_key(features.get("category"))
    if category in {"security", "correctness", "ci", "build"}:
        return 1.03
    if category in {"style", "docs"}:
        return 0.96
    return 1.0


def review_score_candidate(record: dict, job: dict, config: WorkerConfig) -> tuple[dict, dict]:
    finding = record.get("finding") if isinstance(record.get("finding"), dict) else {}
    features = review_candidate_features(finding, record)
    mode = effective_review_calibration_mode(config, job)
    z = 1.28 if mode == "enforce" and features["verification_status"] not in _REVIEW_VERIFIED_STATUSES else 1.0
    reliability = review_context_reliability(features, job, config)
    if not reliability:
        reliability = review_source_reliability_from_counts(record.get("source_stats") or {}, z=z)
    calibrated = review_calibrated_confidence(features, job, config)
    source_factor = review_source_factor(reliability)
    evidence_strength = review_evidence_strength(features)
    delta_relevance = review_delta_relevance(record)
    category_factor = review_category_factor(features)
    model = getattr(config, "review_calibration_model", "relative_factor")
    if model == "logit_beta":
        source_adjustment = review_source_logit_adjustment(reliability)
        evidence_adjustment = review_factor_log_adjustment(evidence_strength)
        delta_adjustment = review_factor_log_adjustment(delta_relevance)
        category_adjustment = review_factor_log_adjustment(category_factor, lower=0.80, upper=1.20)
        verification_adjustment = review_verification_logit_adjustment(features["verification_status"])
        truth_probability = max(
            0.0,
            min(
                1.0,
                review_sigmoid(
                    review_logit(calibrated)
                    + source_adjustment
                    + evidence_adjustment
                    + delta_adjustment
                    + category_adjustment
                    + verification_adjustment
                ),
            ),
        )
        decision_score = truth_probability
        source_adjustment_value = source_adjustment
    else:
        truth_probability = None
        decision_score = max(0.0, min(1.0, calibrated * source_factor * evidence_strength * delta_relevance * category_factor))
        source_adjustment_value = source_factor
    score = {
        "calibrated_confidence": calibrated,
        "source_reliability_mean": review_probability(reliability.get("posterior_mean")),
        "source_reliability_lb": review_probability(reliability.get("posterior_lb")),
        "source_adjustment": source_adjustment_value,
        "source_factor": source_factor,
        "evidence_strength": evidence_strength,
        "delta_relevance": delta_relevance,
        "category_adjustment": category_factor,
        "truth_probability": truth_probability,
        "decision_score": decision_score,
        "scoring_protocol": REVIEW_SCORING_PROTOCOL_VERSION,
        "reliability_source": reliability.get("source") or "prior",
        "cohort_key": reliability.get("cohort_key") or "",
        "score_model": model,
        "drift_state": review_drift_state_for_features(features, job, config),
    }
    return features, score


def proposed_review_decision(record: dict, features: dict, score: dict) -> tuple[str, str, bool]:
    status = str(features.get("verification_status") or "potential_risk")
    decision_score = review_probability(score.get("decision_score"))
    original_reason = str(record.get("reason") or "").strip()
    if original_reason in _REVIEW_HARD_REJECT_REASONS:
        return "rejected", original_reason, False
    if status in _REVIEW_VERIFIED_STATUSES:
        if original_reason in _REVIEW_DELTA_EXCLUSION_REASONS:
            return "audit_only", "not_delta_relevant_but_verified", True
        return "reported", "verified_or_static_proof_guardrail", True
    drift_state = str(score.get("drift_state") or "normal")
    if drift_state == "suspended":
        return "rejected", "drift_suspended_source", False
    if drift_state == "audit_only":
        return "audit_only", "drift_audit_only_source", False
    if status == "unverified":
        if decision_score >= _REVIEW_UNVERIFIED_AUDIT_THRESHOLD:
            return "audit_only", "unverified_high_score_audit_only", False
        return "rejected", "unverified_below_audit_threshold", False
    if decision_score >= _REVIEW_POTENTIAL_RISK_REPORT_THRESHOLD:
        return "reported", "potential_risk_above_report_threshold", False
    if decision_score >= _REVIEW_POTENTIAL_RISK_AUDIT_THRESHOLD:
        return "audit_only", "potential_risk_below_report_threshold", False
    return "rejected", "potential_risk_below_audit_threshold", False


def review_hash(*parts: object) -> str:
    payload = "|".join(str(part or "") for part in parts)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def review_scope_repo_key(job: dict) -> str:
    return review_text(job.get("repo_id") or job.get("github_repo_id") or job.get("repo"), 160).lower()


def review_mode(value: object, default: str = "shadow") -> str:
    mode = str(value or default).strip().lower()
    return mode if mode in _REVIEW_MODE_ORDER else default


def review_server_policy_mode(job: dict) -> str:
    context = job.get("review_calibration_context") if isinstance(job.get("review_calibration_context"), dict) else {}
    if not context:
        return ""
    policy = context.get("rollout_policy") if isinstance(context.get("rollout_policy"), dict) else {}
    return review_mode(policy.get("effective_mode") or policy.get("effectiveMode") or context.get("mode"), "")


def effective_review_calibration_mode(config: WorkerConfig, job: dict) -> str:
    local_mode = review_mode(getattr(config, "review_calibration_mode", "shadow"))
    server_mode = review_server_policy_mode(job)
    if not server_mode:
        return local_mode
    return min((local_mode, server_mode), key=lambda item: _REVIEW_MODE_ORDER[item])


def review_decision_event(
    *,
    job: dict,
    attempt_id: str,
    record: dict,
    features: dict,
    score: dict,
    actual_decision: str,
    actual_reason: str,
    proposed_decision: str,
    proposed_reason: str,
    guardrail_applied: bool,
    config: WorkerConfig,
    mode: str | None = None,
) -> dict:
    candidate_id = str(features.get("candidate_id") or "")
    fingerprint = str(features.get("fingerprint") or "")
    commit = str(job.get("resolved_commit") or job.get("commit") or "").strip()
    convergence_context = job.get("convergence_context") if isinstance(job.get("convergence_context"), dict) else {}
    base_sha = review_git_sha(
        job.get("base_sha")
        or job.get("baseSha")
        or job.get("base_commit")
        or job.get("baseCommit")
        or convergence_context.get("previous_head_sha")
        or convergence_context.get("previousHeadSha")
    )
    head_sha = review_git_sha(job.get("head_sha") or job.get("headSha") or commit)
    source = str(features.get("source") or "reviewer")
    status = str(features.get("verification_status") or "potential_risk")
    branch = str(job.get("branch") or "main").strip() or "main"
    observation_key = review_hash(
        job.get("user_id"),
        review_scope_repo_key(job),
        branch,
        commit,
        source,
        fingerprint,
        candidate_id,
        status,
    )
    event_id = review_hash(
        job.get("job_id"),
        attempt_id,
        candidate_id,
        fingerprint,
        actual_decision,
        score.get("scoring_protocol"),
    )
    provider_chain = list(getattr(config, "provider_chain", []) or [])
    provider = provider_chain[0] if provider_chain else getattr(config, "provider", "")
    model = getattr(config, "codex_model", "") if provider == "codex" else getattr(config, "opencode_model", "")
    provider_chain_text = ",".join(review_text(item, 40) for item in provider_chain if review_text(item, 40))
    return {
        "protocol": REVIEW_DECISION_EVENT_PROTOCOL_VERSION,
        "event_id": event_id,
        "candidate_observation_key": observation_key,
        "scan_id": review_text(job.get("scan_id"), 120),
        "job_id": review_text(job.get("job_id"), 120),
        "attempt_id": attempt_id,
        "user_id": review_text(job.get("user_id"), 120),
        "repo_id": review_text(job.get("repo_id"), 120),
        "github_repo_id": review_text(job.get("github_repo_id"), 120),
        "repo_full_name": review_text(job.get("repo"), 160),
        "branch": branch,
        "commit_sha": commit,
        "base_sha": base_sha,
        "head_sha": head_sha or commit,
        "candidate_id": candidate_id,
        "fingerprint": fingerprint,
        "source": source,
        "provider": provider,
        "model": model,
        "category": review_text(features.get("category"), 80),
        "severity": features.get("severity"),
        "verification_status": status,
        "file_path": features.get("file_path") or "",
        "line_start": features.get("line_start") or 0,
        "line_end": features.get("line_end") or features.get("line_start") or 0,
        "normalized_title": features.get("normalized_title") or "",
        "raw_confidence": features.get("raw_confidence"),
        "calibrated_confidence": score.get("calibrated_confidence"),
        "source_reliability_mean": score.get("source_reliability_mean"),
        "source_reliability_lb": score.get("source_reliability_lb"),
        "source_adjustment": score.get("source_adjustment"),
        "evidence_strength": score.get("evidence_strength"),
        "delta_relevance": score.get("delta_relevance"),
        "category_adjustment": score.get("category_adjustment"),
        "truth_probability": score.get("truth_probability"),
        "decision_score": score.get("decision_score"),
        "decision": actual_decision,
        "decision_reason": actual_reason,
        "scoring_protocol": score.get("scoring_protocol"),
        "score_factors": {
            "scoreKind": "truth_probability" if score.get("truth_probability") is not None else "ranking_score",
            "mode": mode or getattr(config, "review_calibration_mode", "shadow"),
            "model": getattr(config, "review_calibration_model", "relative_factor"),
            "proposedDecision": proposed_decision,
            "proposedReason": proposed_reason,
            "originalDecision": record.get("decision"),
            "originalReason": record.get("reason"),
            "guardrailApplied": bool(guardrail_applied),
            "reliabilitySource": score.get("reliability_source") or "prior",
            "cohortKey": score.get("cohort_key") or "",
            "driftState": score.get("drift_state") or "normal",
            "rawConfidence": features.get("raw_confidence"),
            "calibratedConfidence": score.get("calibrated_confidence"),
            "sourceFactor": score.get("source_factor"),
            "sourceAdjustment": score.get("source_adjustment"),
            "evidenceStrength": score.get("evidence_strength"),
            "deltaRelevance": score.get("delta_relevance"),
            "categoryAdjustment": score.get("category_adjustment"),
            "truthProbability": score.get("truth_probability"),
            "decisionScore": score.get("decision_score"),
            "providerChain": provider_chain_text,
            "workerVersion": __version__,
            "auditProtocol": AUDIT_SWARM_PROTOCOL_VERSION,
            "promptVersion": _REVIEW_PROMPT_VERSION,
            "verifierVersion": _REVIEW_VERIFIER_VERSION,
            "staticCheckerVersion": _REVIEW_STATIC_CHECKER_VERSION,
            "baseSha": base_sha,
            "headSha": head_sha or commit,
        },
        "created_at": int(time.time()),
    }


def sample_from_finding_for_audit_only(finding: dict, reason: str, metadata: dict | None = None) -> dict:
    sample = rejected_candidate_sample(finding, reason)
    sample["decision"] = "audit_only"
    if metadata:
        sample.update(metadata)
    return sample


def apply_review_calibration_decisions(
    config: WorkerConfig,
    job: dict,
    findings: list[dict],
    decision_records: list[dict],
    *,
    attempt_id: str,
) -> dict:
    mode = effective_review_calibration_mode(config, job)
    if mode == "off":
        return {
            "reported_findings": findings,
            "audit_only_findings": [],
            "audit_only_samples": [],
            "rejected_reasons": {},
            "rejected_samples": [],
            "decision_events": [],
            "verified_suppression_count": 0,
        }
    if not decision_records:
        decision_records = [
            {
                "stage": "convergence",
                "decision": "reported",
                "reason": "reported_without_decision_record",
                "finding": finding,
                "fingerprint": finding_fingerprint(finding),
                "source_stats": {},
            }
            for finding in findings
        ]
    reported_by_identity = {id(finding) for finding in findings}
    formal_reported: list[dict] = []
    audit_only: list[dict] = []
    audit_only_samples: list[dict] = []
    rejected_reasons: dict[str, int] = {}
    rejected_samples: list[dict] = []
    decision_events: list[dict] = []
    verified_suppression_count = 0

    for record in decision_records:
        finding = record.get("finding") if isinstance(record.get("finding"), dict) else {}
        if not finding:
            continue
        features, score = review_score_candidate(record, job, config)
        original_decision = str(record.get("decision") or "rejected")
        original_reason = str(record.get("reason") or original_decision)
        proposed_decision, proposed_reason, guardrail_applied = proposed_review_decision(record, features, score)
        actual_decision = original_decision if original_decision in _REVIEW_DECISIONS else "rejected"
        actual_reason = original_reason
        if original_decision == "reported":
            if mode in {"audit_only", "enforce"}:
                actual_decision = proposed_decision
                actual_reason = proposed_reason
            else:
                actual_decision = "reported"
                actual_reason = original_reason
            if actual_decision == "reported":
                formal_reported.append(finding)
            elif actual_decision == "audit_only":
                audit_only.append(finding)
                if len(audit_only_samples) < 5:
                    sample_metadata = review_manual_sample_metadata(
                        config=config,
                        job=job,
                        features=features,
                        score=score,
                        decision=actual_decision,
                    )
                    audit_only_samples.append(sample_from_finding_for_audit_only(finding, actual_reason, sample_metadata))
            else:
                rejected_reasons[actual_reason] = rejected_reasons.get(actual_reason, 0) + 1
                if len(rejected_samples) < 5:
                    sample = rejected_candidate_sample(finding, actual_reason)
                    sample_metadata = review_manual_sample_metadata(
                        config=config,
                        job=job,
                        features=features,
                        score=score,
                        decision=actual_decision,
                    )
                    if sample_metadata:
                        sample.update(sample_metadata)
                    rejected_samples.append(sample)
        elif id(finding) in reported_by_identity and mode == "shadow":
            formal_reported.append(finding)

        if actual_decision != "reported" and features["verification_status"] in _REVIEW_VERIFIED_STATUSES:
            verified_suppression_count += 1
        decision_events.append(
            review_decision_event(
                job=job,
                attempt_id=attempt_id,
                record=record,
                features=features,
                score=score,
                actual_decision=actual_decision,
                actual_reason=actual_reason,
                proposed_decision=proposed_decision,
                proposed_reason=proposed_reason,
                guardrail_applied=guardrail_applied,
                config=config,
                mode=mode,
            )
        )

    if mode == "shadow":
        # Preserve convergence output exactly in shadow mode, including ordering.
        formal_reported = findings
        verified_suppression_count = 0

    return {
        "reported_findings": formal_reported,
        "audit_only_findings": audit_only,
        "audit_only_samples": audit_only_samples,
        "rejected_reasons": rejected_reasons,
        "rejected_samples": rejected_samples,
        "decision_events": decision_events,
        "verified_suppression_count": verified_suppression_count,
    }
