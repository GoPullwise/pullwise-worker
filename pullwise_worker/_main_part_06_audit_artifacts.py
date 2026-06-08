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
    provider_chain = list(getattr(config, "provider_chain", []) or [])
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
        "adapter": provider_chain[0] if provider_chain else clean_protocol_text(getattr(config, "provider", "")),
        "providerChain": provider_chain,
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


