from __future__ import annotations

# Loaded by main.py; keep definitions in that module's globals for compatibility.

def verification_audit_payload(
    *,
    candidate_count: int,
    reported_findings: list[dict],
    rejected_reasons: dict[str, int],
    rejected_samples: list[dict] | None = None,
    audit_only_findings: list[dict] | None = None,
    audit_only_samples: list[dict] | None = None,
    verified_suppression_count: int = 0,
) -> dict:
    rejected_count = sum(rejected_reasons.values())
    status_counts = {status: 0 for status in _VERIFICATION_STATUSES}
    for finding in reported_findings:
        status = str(finding.get("verificationStatus") or "").strip().lower()
        if status not in status_counts:
            status = "potential_risk"
        status_counts[status] += 1
    audit_only_findings = audit_only_findings or []
    parts = [
        f"{candidate_count} candidates evaluated",
        f"{len(reported_findings)} reported",
    ]
    if audit_only_findings:
        parts.append(f"{len(audit_only_findings)} retained for audit only")
    if rejected_count:
        parts.append(f"{rejected_count} rejected before reporting")
    if verified_suppression_count:
        parts.append(f"{verified_suppression_count} verified/static-proof candidates not formally reported")
    return {
        "candidateCount": max(0, int(candidate_count)),
        "reportedCount": len(reported_findings),
        "auditOnlyCount": len(audit_only_findings),
        "rejectedCount": rejected_count,
        "downgradedCount": 0,
        "verifiedSuppressionCount": max(0, int(verified_suppression_count or 0)),
        "verifiedCount": status_counts["verified"],
        "staticProofCount": status_counts["static_proof"],
        "potentialRiskCount": status_counts["potential_risk"],
        "unverifiedCount": status_counts["unverified"],
        "rejectedReasons": [
            {"reason": reason, "count": count}
            for reason, count in sorted(rejected_reasons.items())
            if count > 0
        ],
        "rejectedSamples": [sample for sample in rejected_samples or [] if isinstance(sample, dict)][:5],
        "auditOnlySamples": [sample for sample in audit_only_samples or [] if isinstance(sample, dict)][:5],
        "summary": "; ".join(parts) + ".",
    }


def completion_audit_payload(
    *,
    result_status: str,
    audit_payload: dict | None = None,
    preflight: dict | None = None,
    verification_audit: dict | None = None,
    logs_summary: str = "",
    candidate_count: int = 0,
    rejected_reasons: dict[str, int] | None = None,
    error: str = "",
    error_code: str = "",
) -> dict:
    audit_payload = audit_payload if isinstance(audit_payload, dict) else {}
    preflight = preflight if isinstance(preflight, dict) else {}
    verification_audit = verification_audit if isinstance(verification_audit, dict) else {}
    rejected_reasons = rejected_reasons if isinstance(rejected_reasons, dict) else {}
    cards = [item for item in (first_list(audit_payload, "issue_cards", "issueCards") or []) if isinstance(item, dict)]
    results = [
        item
        for item in (first_list(audit_payload, "verification_results", "verificationResults") or [])
        if isinstance(item, dict)
    ]
    blockers: list[str] = []
    warnings: list[str] = []
    checks: list[dict] = []
    retry_recommended = False
    retry_reason = ""
    normalized_status = clean_protocol_text(result_status).lower()

    if normalized_status == "done":
        card_ids = {audit_swarm_resolved_issue_id(card, index) for index, card in enumerate(cards)}
        missing_locations = []
        missing_claims = []
        missing_evidence = []
        missing_reproduction = []
        missing_suggested_tests = []
        for index, card in enumerate(cards):
            issue_id = audit_swarm_resolved_issue_id(card, index) or f"issue-{index + 1}"
            if not audit_swarm_locations(card):
                missing_locations.append(issue_id)
            if not protocol_multiline_text(card.get("claim")):
                missing_claims.append(issue_id)
            if not protocol_text_items(card.get("evidence")):
                missing_evidence.append(issue_id)
            if not protocol_multiline_text(card.get("reproduction_idea") or card.get("reproductionIdea")):
                missing_reproduction.append(issue_id)
            if not protocol_multiline_text(card.get("suggested_test") or card.get("suggestedTest")):
                missing_suggested_tests.append(issue_id)
        _completion_audit_append_check(
            checks,
            check_id="issue_card_locations",
            failed_ids=missing_locations,
            noun="issue cards",
            ok_count=len(cards) - len(missing_locations),
            detail_label="missing repository-relative locations",
            blockers=blockers,
        )
        _completion_audit_append_check(
            checks,
            check_id="issue_card_claims",
            failed_ids=missing_claims,
            noun="issue cards",
            ok_count=len(cards) - len(missing_claims),
            detail_label="missing claim text",
            blockers=blockers,
        )
        _completion_audit_append_check(
            checks,
            check_id="issue_card_evidence",
            failed_ids=missing_evidence,
            noun="issue cards",
            ok_count=len(cards) - len(missing_evidence),
            detail_label="missing evidence",
            blockers=blockers,
        )
        _completion_audit_append_check(
            checks,
            check_id="issue_card_reproduction_ideas",
            failed_ids=missing_reproduction,
            noun="issue cards",
            ok_count=len(cards) - len(missing_reproduction),
            detail_label="missing reproduction ideas",
            blockers=blockers,
        )
        _completion_audit_append_check(
            checks,
            check_id="issue_card_suggested_tests",
            failed_ids=missing_suggested_tests,
            noun="issue cards",
            ok_count=len(cards) - len(missing_suggested_tests),
            detail_label="missing suggested tests",
            blockers=blockers,
        )
        unknown_result_issue_ids = []
        single_placeholder_id = audit_swarm_single_placeholder_issue_id(cards)
        for result in results:
            resolved_issue_id = audit_swarm_resolved_verification_issue_id(result, single_placeholder_id)
            if resolved_issue_id and resolved_issue_id not in card_ids:
                unknown_result_issue_ids.append(resolved_issue_id)
        unknown_result_issue_ids = dedupe_text(unknown_result_issue_ids)
        if unknown_result_issue_ids:
            message = (
                "Verification results reference unknown issue ids: "
                + ", ".join(unknown_result_issue_ids[:5])
                + "."
            )
            blockers.append(message)
            checks.append(
                {
                    "id": "verification_issue_references",
                    "status": "failed",
                    "summary": message,
                    "details": unknown_result_issue_ids[:5],
                }
            )
        else:
            checks.append(
                {
                    "id": "verification_issue_references",
                    "status": "passed",
                    "summary": f"All {len(results)} verification results reference known issue ids.",
                }
            )
        negative_evidence_sources = completion_audit_negative_evidence_sources(preflight, logs_summary)
        if cards:
            checks.append(
                {
                    "id": "empty_result_negative_evidence",
                    "status": "passed",
                    "summary": f"Reported findings present; empty-result evidence checks are not required for {len(cards)} issue cards.",
                }
            )
        else:
            evidence_status = "passed" if len(negative_evidence_sources) >= 2 else "warning"
            evidence_summary = (
                f"Empty result is backed by negative evidence from {', '.join(negative_evidence_sources)}."
                if evidence_status == "passed"
                else "Empty result has limited negative evidence from preflight/verifier/logs."
            )
            if evidence_status == "warning":
                warnings.append(evidence_summary)
            checks.append(
                {
                    "id": "empty_result_negative_evidence",
                    "status": evidence_status,
                    "summary": evidence_summary,
                    "details": negative_evidence_sources,
                }
            )
        short_output_status = "passed"
        short_output_summary = "Provider output did not look suspiciously short."
        short_logs = clean_protocol_text(logs_summary)
        if not cards and not results and candidate_count == 0 and len(short_logs) < 24:
            short_output_status = "warning"
            short_output_summary = "Provider output/log summary looks unusually short for an empty review result."
            warnings.append(short_output_summary)
            retry_recommended = True
            retry_reason = short_output_summary
        checks.append(
            {
                "id": "provider_output_shape",
                "status": short_output_status,
                "summary": short_output_summary,
            }
        )
    else:
        classification = completion_audit_failed_retry_classification(error_code, error)
        retry_recommended = classification["retryRecommended"]
        retry_reason = classification["retryReason"]
        if classification["status"] == "warning":
            warnings.append(classification["summary"])
        checks.append(
            {
                "id": "failed_result_retryability",
                "status": classification["status"],
                "summary": classification["summary"],
            }
        )

    final_status = "failed" if blockers else "warning" if warnings else "passed"
    summary_parts = [f"{len(checks)} deterministic checks", f"{len(blockers)} blockers", f"{len(warnings)} warnings"]
    summary = f"{final_status}: " + ", ".join(summary_parts) + "."
    if retry_reason:
        summary = f"{summary} Retry hint: {retry_reason}"
    return {
        "protocol": "pullwise-completion-audit/0.1",
        "status": final_status,
        "blockers": blockers[:10],
        "warnings": warnings[:10],
        "checks": checks[:10],
        "retryRecommended": retry_recommended,
        "retryReason": retry_reason,
        "summary": summary,
    }


def _completion_audit_append_check(
    checks: list[dict],
    *,
    check_id: str,
    failed_ids: list[str],
    noun: str,
    ok_count: int,
    detail_label: str,
    blockers: list[str],
) -> None:
    failed_ids = dedupe_text(failed_ids)
    if failed_ids:
        message = f"{len(failed_ids)} {noun} are {detail_label}: {', '.join(failed_ids[:5])}."
        blockers.append(message)
        checks.append({"id": check_id, "status": "failed", "summary": message, "details": failed_ids[:5]})
        return
    checks.append(
        {
            "id": check_id,
            "status": "passed",
            "summary": f"All {max(0, int(ok_count))} {noun} passed the {detail_label} check.",
        }
    )


def completion_audit_negative_evidence_sources(preflight: dict, logs_summary: str) -> list[str]:
    sources = []
    if preflight:
        sources.append("preflight")
    verifier = preflight.get("verifier") if isinstance(preflight.get("verifier"), dict) else {}
    if verifier or protocol_multiline_text(logs_summary):
        sources.append("verifier")
    if protocol_multiline_text(logs_summary):
        sources.append("logs")
    return dedupe_text(sources)


def completion_audit_failed_retry_classification(error_code: str, error: str) -> dict:
    normalized_code = clean_protocol_text(error_code)
    detail = protocol_multiline_text(error)
    lowered = detail.lower()
    if normalized_code == REPOSITORY_TOO_LARGE_ERROR_CODE:
        reason = "Repository exceeds configured limits; retry only after repository size or worker limits change."
        return {
            "status": "passed",
            "retryRecommended": False,
            "retryReason": reason,
            "summary": reason,
        }
    if "auth failure" in lowered or "authentication" in lowered or "login" in lowered:
        reason = "Provider authentication must be repaired before retrying this job."
        return {
            "status": "passed",
            "retryRecommended": False,
            "retryReason": reason,
            "summary": reason,
        }
    if any(marker in lowered for marker in ("timed out", "timeout", "temporarily disabled", "connection", "network")):
        reason = "This failure looks transient; retry is recommended after the provider or network recovers."
        return {
            "status": "passed",
            "retryRecommended": True,
            "retryReason": reason,
            "summary": reason,
        }
    if "all review providers failed" in lowered:
        reason = "All configured review providers failed before producing a valid payload; retry is recommended."
        return {
            "status": "passed",
            "retryRecommended": True,
            "retryReason": reason,
            "summary": reason,
        }
    reason = "Retryability could not be classified deterministically from the worker failure."
    return {
        "status": "warning",
        "retryRecommended": False,
        "retryReason": "",
        "summary": reason,
    }


def job_trace_checkpoint(
    stage: str,
    *,
    status: str = "ok",
    summary: str = "",
    counts: dict | None = None,
    details: dict | None = None,
    logs_summary: str = "",
) -> dict:
    normalized_status = clean_protocol_text(status).lower()
    if normalized_status not in {"ok", "warning", "failed"}:
        normalized_status = "ok"
    payload = {
        "stage": clean_protocol_text(stage),
        "status": normalized_status,
        "summary": protocol_multiline_text(summary)[:400],
        "counts": _job_trace_counts_payload(counts),
        "details": _job_trace_value(details),
        "logsSummary": protocol_multiline_text(logs_summary)[:240],
    }
    return {key: value for key, value in payload.items() if value not in ("", [], {})}


def job_trace_payload(
    *,
    result_status: str,
    checkpoints: list[dict],
    candidate_count_before_filter: int = 0,
    rejected_reasons: dict[str, int] | None = None,
    next_retry_hint: str = "",
) -> dict:
    rejected_reasons = rejected_reasons if isinstance(rejected_reasons, dict) else {}
    payload = {
        "protocol": "pullwise-job-trace/0.1",
        "status": clean_protocol_text(result_status).lower() or "done",
        "checkpoints": [item for item in checkpoints if isinstance(item, dict)][:12],
        "candidateCountBeforeFilter": max(0, int(candidate_count_before_filter or 0)),
        "rejectionReasons": [
            {"reason": clean_protocol_text(reason), "count": protocol_count(count)}
            for reason, count in sorted(rejected_reasons.items())
            if clean_protocol_text(reason) and protocol_count(count)
        ],
        "nextRetryHint": protocol_multiline_text(next_retry_hint)[:240],
    }
    if payload["checkpoints"]:
        payload["summary"] = protocol_multiline_text(payload["checkpoints"][-1].get("summary"))[:240]
    return {key: value for key, value in payload.items() if value not in ("", [], {})}


def _job_trace_counts_payload(value: object) -> dict:
    source = value if isinstance(value, dict) else {}
    counts = {}
    for key, raw in source.items():
        normalized_key = clean_protocol_text(key)
        count = protocol_count(raw)
        if normalized_key and count:
            counts[normalized_key] = count
    return counts


def _job_trace_value(value: object) -> object:
    if isinstance(value, dict):
        payload = {}
        for key, item in value.items():
            normalized_key = clean_protocol_text(key)
            normalized_value = _job_trace_value(item)
            if normalized_key and normalized_value not in ("", [], {}):
                payload[normalized_key] = normalized_value
        return payload
    if isinstance(value, list):
        items = []
        for item in value[:6]:
            normalized_item = _job_trace_value(item)
            if normalized_item not in ("", [], {}):
                items.append(normalized_item)
        return items
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return round(value, 3)
    return protocol_multiline_text(value)[:200]


def audit_swarm_scan_artifacts(
    stage: str,
    *,
    config: WorkerConfig | None = None,
    audit_payload: dict | None = None,
    preflight: dict | None = None,
    verification_audit: dict | None = None,
    summary: str = "",
    logs_summary: str = "",
) -> dict:
    audit_payload = audit_payload if isinstance(audit_payload, dict) else {}
    preflight = preflight if isinstance(preflight, dict) else {}
    verification_audit = verification_audit if isinstance(verification_audit, dict) else {}
    cards = [item for item in (first_list(audit_payload, "issue_cards", "issueCards") or []) if isinstance(item, dict)]
    results = [
        item
        for item in (first_list(audit_payload, "verification_results", "verificationResults") or [])
        if isinstance(item, dict)
    ]
    provider = clean_protocol_text(getattr(config, "provider", ""))
    roles = dedupe_text(
        [
            *[
                clean_protocol_text(card.get("agent_role") or card.get("agentRole"))
                for card in cards
            ],
            *[
                clean_protocol_text(result.get("verifier_role") or result.get("verifierRole"))
                for result in results
            ],
        ]
    )
    shards = dedupe_text(
        [
            clean_protocol_text(card.get("shard_id") or card.get("shardId"))
            for card in cards
        ]
    )
    verifier_runs = []
    verifier = preflight.get("verifier") if isinstance(preflight.get("verifier"), dict) else {}
    if isinstance(verifier.get("runs"), list):
        verifier_runs = [item for item in verifier["runs"] if isinstance(item, dict)]
    counts = {
        "issueCards": len(cards),
        "verificationResults": len(results),
        "candidateCount": protocol_count(verification_audit.get("candidateCount") or verification_audit.get("candidate_count")),
        "reportedCount": protocol_count(verification_audit.get("reportedCount") or verification_audit.get("reported_count")),
        "auditOnlyCount": protocol_count(verification_audit.get("auditOnlyCount") or verification_audit.get("audit_only_count")),
        "rejectedCount": protocol_count(verification_audit.get("rejectedCount") or verification_audit.get("rejected_count")),
        "verifiedCount": protocol_count(verification_audit.get("verifiedCount") or verification_audit.get("verified_count")),
        "staticProofCount": protocol_count(verification_audit.get("staticProofCount") or verification_audit.get("static_proof_count")),
        "potentialRiskCount": protocol_count(verification_audit.get("potentialRiskCount") or verification_audit.get("potential_risk_count")),
        "unverifiedCount": protocol_count(verification_audit.get("unverifiedCount") or verification_audit.get("unverified_count")),
        "manifestCount": len(preflight.get("manifests") or []) if isinstance(preflight.get("manifests"), list) else 0,
        "toolCount": len(preflight.get("toolVersions") or []) if isinstance(preflight.get("toolVersions"), list) else 0,
        "verifierRunCount": len(verifier_runs),
    }
    payload = {
        "protocol": clean_protocol_text(audit_payload.get("audit_protocol") or audit_payload.get("auditProtocol"))
        or AUDIT_SWARM_PROTOCOL_VERSION,
        "stage": clean_protocol_text(stage),
        "adapter": provider,
        "provider": provider,
        "summary": protocol_multiline_text(summary) or protocol_multiline_text(verification_audit.get("summary")),
        "logsSummary": protocol_multiline_text(logs_summary)[:1000],
        "counts": {key: value for key, value in counts.items() if value},
        "roles": roles[:12],
        "shards": shards[:20],
        "issueCards": [audit_swarm_issue_card_summary(card, index) for index, card in enumerate(cards[:10])],
        "verificationResults": [
            audit_swarm_verification_result_summary(result)
            for result in results[:20]
        ],
        "evidenceBlocks": audit_swarm_evidence_blocks(
            stage,
            cards=cards,
            results=results,
            preflight=preflight,
            verification_audit=verification_audit,
            summary=summary,
        ),
    }
    return {key: value for key, value in payload.items() if value not in ("", [], {})}


def audit_swarm_evidence_blocks(
    stage: str,
    *,
    cards: list[dict],
    results: list[dict],
    preflight: dict,
    verification_audit: dict,
    summary: str = "",
) -> list[dict]:
    blocks: list[dict] = []
    stage_text = clean_protocol_text(stage)
    summary_text = protocol_multiline_text(summary) or protocol_multiline_text(verification_audit.get("summary"))
    if summary_text:
        blocks.append(
            audit_swarm_evidence_block(
                "summary",
                block_id=f"{stage_text or 'audit'}:summary",
                title="Audit summary",
                summary=summary_text,
                stage=stage_text,
            )
        )
    if verification_audit:
        rejected_count = protocol_count(verification_audit.get("rejectedCount") or verification_audit.get("rejected_count"))
        if rejected_count:
            blocks.append(
                audit_swarm_evidence_block(
                    "risk",
                    block_id=f"{stage_text or 'audit'}:rejected",
                    title="Rejected before reporting",
                    summary=f"{rejected_count} candidates were rejected before reporting because they lacked enough evidence.",
                    stage=stage_text,
                    status="rejected",
                )
            )
    if preflight:
        preflight_summary = protocol_multiline_text(preflight.get("summary"))
        if preflight_summary and not cards and not results:
            blocks.append(
                audit_swarm_evidence_block(
                    "summary",
                    block_id=f"{stage_text or 'audit'}:preflight",
                    title="Preflight evidence",
                    summary=preflight_summary,
                    stage=stage_text,
                )
            )
    results_by_issue = audit_swarm_verifications_by_issue(results)
    for index, card in enumerate(cards[:8]):
        blocks.extend(audit_swarm_issue_card_evidence_blocks(card, results_by_issue.get(audit_swarm_issue_key(card), []), index))
    for index, result in enumerate(results[:12]):
        blocks.extend(audit_swarm_verification_evidence_blocks(result, index))
    return audit_swarm_dedupe_blocks(blocks)[:40]


def audit_swarm_issue_card_evidence_blocks(card: dict, results: list[dict], index: int) -> list[dict]:
    issue_id = audit_swarm_issue_key(card) or audit_swarm_generated_id(card, index)
    title = clean_protocol_text(card.get("title")) or f"Audit candidate {index + 1}"
    severity = audit_swarm_severity(card.get("severity"))
    category = audit_swarm_category(card)
    role = clean_protocol_text(card.get("agent_role") or card.get("agentRole"))
    shard_id = clean_protocol_text(card.get("shard_id") or card.get("shardId"))
    confidence = audit_swarm_confidence(card.get("confidence"), audit_swarm_verdict(results))
    common = {
        "issueId": issue_id,
        "severity": severity,
        "category": category,
        "role": role,
        "shardId": shard_id,
        "confidence": confidence,
    }
    blocks = []
    claim = protocol_multiline_text(card.get("claim") or card.get("summary") or card.get("description"))
    if claim:
        blocks.append(
            audit_swarm_evidence_block(
                "claim",
                block_id=f"{issue_id}:claim",
                title=title,
                summary=claim,
                **common,
            )
        )
    for location_index, location in enumerate(audit_swarm_locations(card)[:2]):
        blocks.append(
            audit_swarm_evidence_block(
                "code_location",
                block_id=f"{issue_id}:location:{location_index}",
                title="Code location",
                summary=claim or title,
                file=clean_protocol_text(location.get("file")),
                startLine=protocol_count(location.get("startLine")),
                endLine=protocol_count(location.get("endLine")),
                **common,
            )
        )
    for evidence_index, evidence in enumerate(protocol_text_items(card.get("evidence"))[:3]):
        blocks.append(
            audit_swarm_evidence_block(
                "evidence",
                block_id=f"{issue_id}:evidence:{evidence_index}",
                title="Discovery evidence",
                summary=evidence,
                **common,
            )
        )
    for check_index, check in enumerate(protocol_text_list(card.get("false_positive_checks") or card.get("falsePositiveChecks"))[:3]):
        blocks.append(
            audit_swarm_evidence_block(
                "false_positive_check",
                block_id=f"{issue_id}:false-positive:{check_index}",
                title="False-positive check",
                summary=check,
                **common,
            )
        )
    for invariant_index, invariant in enumerate(protocol_text_list(card.get("violated_invariants") or card.get("violatedInvariants"))[:3]):
        blocks.append(
            audit_swarm_evidence_block(
                "invariant",
                block_id=f"{issue_id}:invariant:{invariant_index}",
                title="Violated invariant",
                summary=invariant,
                **common,
            )
        )
    suggested_test = protocol_multiline_text(card.get("suggested_test") or card.get("suggestedTest"))
    if suggested_test:
        blocks.append(
            audit_swarm_evidence_block(
                "command",
                block_id=f"{issue_id}:suggested-test",
                title="Suggested test",
                summary=suggested_test,
                status="suggested",
                **common,
            )
        )
    return blocks


def audit_swarm_verification_evidence_blocks(result: dict, index: int) -> list[dict]:
    issue_id = clean_protocol_text(result.get("issue_id") or result.get("issueId"))
    role = clean_protocol_text(result.get("verifier_role") or result.get("verifierRole"))
    verdict = clean_protocol_text(result.get("verdict")).lower()
    proof_type = clean_protocol_text(result.get("proof_type") or result.get("proofType"))
    confidence = audit_swarm_confidence(result.get("confidence"), verdict)
    summary = protocol_multiline_text(result.get("result_summary") or result.get("resultSummary") or result.get("summary"))
    common = {
        "issueId": issue_id,
        "role": role,
        "verdict": verdict if verdict in {"confirmed", "rejected", "inconclusive"} else "",
        "proofType": proof_type,
        "proofStrength": protocol_count(result.get("proof_strength") or result.get("proofStrength")),
        "confidence": confidence,
    }
    key = issue_id or f"verification-{index}"
    blocks = [
        audit_swarm_evidence_block(
            "verifier_verdict",
            block_id=f"{key}:verdict:{role or index}",
            title="Verifier verdict",
            summary=summary or f"{role or 'verifier'} returned {common['verdict'] or 'a verdict'}.",
            **common,
        )
    ]
    for command_index, command in enumerate(protocol_text_list(result.get("commands_run") or result.get("commandsRun"))[:3]):
        blocks.append(
            audit_swarm_evidence_block(
                "command",
                block_id=f"{key}:command:{command_index}",
                title="Verifier command",
                summary=summary,
                command=command,
                status="executed",
                **common,
            )
        )
    for evidence_index, evidence in enumerate(protocol_text_items(result.get("evidence"))[:3]):
        blocks.append(
            audit_swarm_evidence_block(
                "evidence",
                block_id=f"{key}:verification-evidence:{evidence_index}",
                title="Verifier evidence",
                summary=evidence,
                **common,
            )
        )
    return blocks


def audit_swarm_evidence_block(kind: str, *, block_id: str = "", title: str = "", summary: str = "", **fields: object) -> dict:
    normalized_kind = clean_protocol_text(kind).lower()
    if normalized_kind not in AUDIT_SWARM_EVIDENCE_BLOCK_KINDS:
        normalized_kind = "evidence"
    payload = {
        "id": clean_protocol_text(block_id),
        "kind": normalized_kind,
        "title": clean_protocol_text(title),
        "summary": protocol_multiline_text(summary),
    }
    for key in (
        "issueId",
        "severity",
        "category",
        "role",
        "shardId",
        "stage",
        "status",
        "verdict",
        "proofType",
        "command",
        "file",
    ):
        text = clean_protocol_text(fields.get(key))
        if text:
            payload[key] = text
    for key in ("startLine", "endLine", "proofStrength"):
        count = protocol_count(fields.get(key))
        if count:
            payload[key] = count
    if "confidence" in fields:
        try:
            confidence = float(fields["confidence"])
        except (OverflowError, TypeError, ValueError):
            confidence = 0.0
        if confidence:
            payload["confidence"] = max(0.0, min(1.0, confidence))
    return {key: value for key, value in payload.items() if value not in ("", [], {})}


def audit_swarm_dedupe_blocks(blocks: list[dict]) -> list[dict]:
    deduped = []
    seen = set()
    for block in blocks:
        if not isinstance(block, dict):
            continue
        key = (
            clean_protocol_text(block.get("kind")),
            clean_protocol_text(block.get("issueId")),
            clean_protocol_text(block.get("title")),
            protocol_multiline_text(block.get("summary")),
            clean_protocol_text(block.get("command")),
            clean_protocol_text(block.get("file")),
            protocol_count(block.get("startLine")),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(block)
    return deduped


def audit_swarm_issue_card_summary(card: dict, index: int) -> dict:
    locations = audit_swarm_locations(card)
    primary = locations[0] if locations else {}
    payload = {
        "issueId": audit_swarm_issue_key(card) or audit_swarm_generated_id(card, index),
        "title": clean_protocol_text(card.get("title")) or f"Audit candidate {index + 1}",
        "severity": audit_swarm_severity(card.get("severity")),
        "category": audit_swarm_category(card),
        "shardId": clean_protocol_text(card.get("shard_id") or card.get("shardId")),
        "agentRole": clean_protocol_text(card.get("agent_role") or card.get("agentRole")),
        "confidence": audit_swarm_confidence(card.get("confidence"), "candidate"),
        "file": clean_protocol_text(primary.get("file")),
        "line": protocol_count(primary.get("startLine")),
        "evidenceCount": len(card.get("evidence") or []) if isinstance(card.get("evidence"), list) else 0,
    }
    return {key: value for key, value in payload.items() if value not in ("", [], {})}


def audit_swarm_verification_result_summary(result: dict) -> dict:
    commands = protocol_text_list(result.get("commands_run") or result.get("commandsRun"))
    evidence = protocol_text_list(result.get("evidence"))
    payload = {
        "issueId": clean_protocol_text(result.get("issue_id") or result.get("issueId")),
        "verifierRole": clean_protocol_text(result.get("verifier_role") or result.get("verifierRole")),
        "verdict": clean_protocol_text(result.get("verdict")),
        "proofType": clean_protocol_text(result.get("proof_type") or result.get("proofType")),
        "proofStrength": protocol_count(result.get("proof_strength") or result.get("proofStrength")),
        "confidence": audit_swarm_confidence(result.get("confidence"), clean_protocol_text(result.get("verdict"))),
        "commandCount": len(commands),
        "evidenceCount": len(evidence),
        "summary": protocol_multiline_text(result.get("result_summary") or result.get("resultSummary")),
    }
    if commands:
        payload["command"] = commands[0]
    return {key: value for key, value in payload.items() if value not in ("", [], {})}


def protocol_count(value: object) -> int:
    if isinstance(value, bool):
        return 0
    try:
        count = int(value or 0)
    except (OverflowError, TypeError, ValueError):
        return 0
    return max(0, count)


