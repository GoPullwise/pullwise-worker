from __future__ import annotations

# Loaded by main.py; keep definitions in that module's globals for compatibility.

def run_codex_review(config: WorkerConfig, job: dict, checkout_dir: Path) -> tuple[dict, dict, str]:
    errors: list[str] = []
    try:
        deterministic_payload = audit_swarm_payload_from_findings(
            run_deterministic_repository_checks(job, checkout_dir),
            verifier_role="deterministic-check",
        )
    except Exception as exc:
        deterministic_payload = empty_audit_swarm_payload()
        errors.append(f"deterministic: {redact_secrets(str(exc), config)}"[:500])
    for provider in config.provider_chain:
        try:
            if provider == "codex":
                provider_result = run_codex_provider_review(config, job, checkout_dir)
            elif provider == "opencode":
                provider_result = run_opencode_provider_review(config, job, checkout_dir)
            else:
                raise RuntimeError(f"unsupported review provider: {provider}")
            audit_payload, _summary, logs_summary = provider_result[:3]
            ai_usage = normalize_ai_usage(provider_result[3] if len(provider_result) > 3 else {})
            audit_payload = normalize_audit_swarm_files_for_checkout(audit_payload, checkout_dir)
            audit_payload = merge_audit_swarm_payloads(deterministic_payload, audit_payload)
            effective_agent_config = effective_agent_config_payload(config, provider)
            audit_payload["effectiveAgentConfig"] = effective_agent_config
            if ai_usage:
                audit_payload["aiUsage"] = ai_usage
            summary = summarize(audit_swarm_findings_from_payload(audit_payload) or [])
            if errors:
                logs_summary = "\n".join([*errors, logs_summary])[-1000:]
            return audit_payload, summary, logs_summary
        except Exception as exc:
            errors.append(f"{provider}: {redact_secrets(str(exc), config)}"[:500])
    raise RuntimeError(f"all review providers failed: {'; '.join(errors)}")


def codex_auth_failure_error(config: WorkerConfig) -> str | None:
    with _CODEX_AUTH_FAILURE_LOCK:
        remaining = _codex_auth_failure_until - time.monotonic()
        detail = _codex_auth_failure_detail
    if remaining <= 0:
        return None
    clean_detail = redact_secrets(detail, config)
    return f"codex exec temporarily disabled after auth failure; retrying in {remaining:.0f}s: {clean_detail}"


def mark_codex_auth_failure(config: WorkerConfig, detail: str) -> None:
    global _codex_auth_failure_until, _codex_auth_failure_detail

    cooldown = max(0, int(config.codex_auth_failure_cooldown_seconds))
    if cooldown <= 0:
        return
    clipped = redact_secrets(str(detail or "").strip(), config)
    if len(clipped) > 500:
        clipped = clipped[-500:]
    with _CODEX_AUTH_FAILURE_LOCK:
        _codex_auth_failure_until = time.monotonic() + cooldown
        _codex_auth_failure_detail = clipped


def clear_codex_auth_failure() -> None:
    global _codex_auth_failure_until, _codex_auth_failure_detail

    with _CODEX_AUTH_FAILURE_LOCK:
        _codex_auth_failure_until = 0.0
        _codex_auth_failure_detail = ""


def looks_like_codex_auth_failure(detail: str) -> bool:
    lowered = str(detail or "").lower()
    return any(marker.lower() in lowered for marker in _CODEX_AUTH_FAILURE_MARKERS)


def run_codex_provider_review(config: WorkerConfig, job: dict, checkout_dir: Path) -> tuple[dict, dict, str, dict]:
    prompt = review_prompt(job)
    with tempfile.TemporaryDirectory(prefix="pullwise-codex-") as tmpdir:
        schema_path = Path(tmpdir) / "audit-swarm.schema.json"
        output_path = Path(tmpdir) / "audit-swarm.json"
        schema_path.write_text(json.dumps(audit_swarm_output_schema()), encoding="utf-8")
        command = codex_review_command(config, str(schema_path), str(output_path), prompt)
        auth_failure = codex_auth_failure_error(config)
        if auth_failure:
            raise RuntimeError(auth_failure)
        with _CODEX_EXEC_LOCK:
            auth_failure = codex_auth_failure_error(config)
            if auth_failure:
                raise RuntimeError(auth_failure)
            completed = subprocess.run(
                command,
                cwd=str(checkout_dir),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=config.codex_timeout_seconds,
            )
        raw_logs = "\n".join([completed.stdout or "", completed.stderr or ""])
        logs_summary = redact_secrets(raw_logs[-1000:], config)
        if completed.returncode != 0:
            detail = codex_failure_detail(completed.stderr or completed.stdout, config)
            if looks_like_codex_auth_failure(detail):
                mark_codex_auth_failure(config, detail)
            raise RuntimeError(f"codex exec failed with exit code {completed.returncode}: {detail[:700]}")
        output = output_path.read_text(encoding="utf-8") if output_path.exists() else completed.stdout
    audit_payload = parse_audit_swarm_payload(output)
    return audit_payload, summarize(audit_swarm_findings_from_payload(audit_payload) or []), logs_summary, codex_ai_usage(raw_logs, config)


def codex_ai_usage(_raw_output: str, config: WorkerConfig) -> dict:
    return ai_usage_payload(config.codex_model)


def opencode_ai_usage(_raw_output: str, config: WorkerConfig) -> dict:
    return ai_usage_payload(config.opencode_model)


def ai_usage_payload(model: object) -> dict:
    clean_model = clean_protocol_text(model)
    return {"model": clean_model} if clean_model else {}


def normalize_ai_usage(value: object) -> dict:
    if not isinstance(value, dict):
        return {}
    return ai_usage_payload(value.get("model"))


def codex_failure_detail(raw_output: str, config: WorkerConfig) -> str:
    structured = extract_codex_error_detail(raw_output)
    raw_detail = structured or (raw_output or "").strip()[-1000:] or "no stderr/stdout"
    return redact_secrets(raw_detail, config)


def extract_codex_error_detail(raw_output: str) -> str | None:
    text = raw_output or ""
    marker = "ERROR:"
    index = text.find(marker)
    decoder = json.JSONDecoder()
    while index >= 0:
        candidate = text[index + len(marker):].lstrip()
        try:
            payload, _end = decoder.raw_decode(candidate)
        except json.JSONDecodeError:
            index = text.find(marker, index + len(marker))
            continue
        if isinstance(payload, dict):
            code = payload.get("code")
            message = payload.get("message")
            error = payload.get("error")
            if not isinstance(message, str) and isinstance(error, dict):
                message = error.get("message")
                code = code or error.get("code")
            parts = [part for part in (code, message) if isinstance(part, str) and part.strip()]
            if parts:
                return ": ".join(parts)
            error_type = payload.get("type")
            if isinstance(error_type, str) and error_type.strip():
                return f"type={error_type}"
        index = text.find(marker, index + len(marker))
    return None


def codex_review_command(config: WorkerConfig, schema_path: str, output_path: str, prompt: str) -> list[str]:
    scope_ok, scope_detail = provider_command_scope_check(config.codex_command, config, "Codex")
    if not scope_ok:
        raise RuntimeError(scope_detail)
    command = [
        config.codex_command,
        "exec",
        _CODEX_SKIP_GIT_REPO_CHECK_ARG,
        "--ignore-user-config",
        "--config",
        f'model_reasoning_effort="{config.codex_reasoning_effort}"',
        "--sandbox",
        "read-only",
        "--output-schema",
        schema_path,
        "--output-last-message",
        output_path,
    ]
    if config.codex_model:
        command.extend(["--model", config.codex_model])
    command.append(prompt)
    return command


def run_opencode_provider_review(config: WorkerConfig, job: dict, checkout_dir: Path) -> tuple[dict, dict, str, dict]:
    prompt = review_prompt(job)
    command = opencode_review_command(config, prompt)
    completed = subprocess.run(
        command,
        cwd=str(checkout_dir),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=config.codex_timeout_seconds,
    )
    raw_logs = completed.stderr or completed.stdout
    logs_summary = redact_secrets(raw_logs[-1000:], config)
    if completed.returncode != 0:
        raise RuntimeError(f"opencode run failed with exit code {completed.returncode}: {logs_summary[:300]}")
    audit_payload = parse_audit_swarm_payload(completed.stdout)
    return (
        audit_payload,
        summarize(audit_swarm_findings_from_payload(audit_payload) or []),
        logs_summary,
        opencode_ai_usage("\n".join([completed.stdout or "", completed.stderr or ""]), config),
    )


def opencode_review_command(config: WorkerConfig, prompt: str) -> list[str]:
    scope_ok, scope_detail = provider_command_scope_check(config.opencode_command, config, "OpenCode")
    if not scope_ok:
        raise RuntimeError(scope_detail)
    command = [config.opencode_command, "run"]
    if config.opencode_model:
        command.extend(["--model", config.opencode_model])
    if config.opencode_variant:
        command.extend(["--variant", config.opencode_variant])
    command.append(prompt)
    return command


REVIEW_OUTPUT_LANGUAGE_NAMES = {
    "en": "English",
    "zh-CN": "Chinese",
    "zh": "Chinese",
    "ja": "Japanese",
    "ko": "Korean",
    "es": "Spanish",
    "fr": "French",
    "de": "German",
    "pt-BR": "Portuguese",
    "pt": "Portuguese",
    "it": "Italian",
}


def review_output_language_name(job: dict) -> str:
    raw_code = str(job.get("review_output_language") or job.get("reviewOutputLanguage") or "").strip()
    raw_label = str(job.get("review_output_language_label") or job.get("reviewOutputLanguageLabel") or "").strip()
    if raw_label and len(raw_label) <= 80 and not any(char in raw_label for char in "\r\n"):
        return raw_label
    return REVIEW_OUTPUT_LANGUAGE_NAMES.get(raw_code, "English")


def review_prompt(job: dict) -> str:
    convergence_context = job.get("convergence_context") if isinstance(job.get("convergence_context"), dict) else {}
    previous_head_sha = normalized_head_sha(convergence_context.get("previous_head_sha"))
    open_findings = convergence_context.get("open_findings") if isinstance(convergence_context.get("open_findings"), list) else []
    language_name = review_output_language_name(job)
    language_instruction = (
        f"Write every human-facing review output field in {language_name}. "
        "Keep JSON keys, enum values, file paths, commands, identifiers, and code excerpts unchanged."
    )
    architecture_summary = job.get("architecture_summary") if isinstance(job.get("architecture_summary"), dict) else {}
    if not architecture_summary and isinstance(job.get("architectureSummary"), dict):
        architecture_summary = job.get("architectureSummary") or {}
    architecture_prompt = str(architecture_summary.get("promptText") or "").strip()
    if architecture_prompt and len(architecture_prompt) > REPOSITORY_GRAPH_MAX_PROMPT_CHARS:
        architecture_prompt = architecture_prompt[: REPOSITORY_GRAPH_MAX_PROMPT_CHARS - 3].rstrip() + "..."
    architecture_instruction = (
        f"Repository architecture context:\n{architecture_prompt}\n"
        if architecture_prompt
        else ""
    )
    convergence_instruction = ""
    if previous_head_sha or open_findings:
        prior_refs = []
        for item in open_findings:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or "").strip()[:120]
            issue_id = str(item.get("issue_id") or item.get("issueId") or "").strip()[:80]
            fingerprint = str(item.get("fingerprint") or "").strip()[:80]
            anchor = issue_id or fingerprint
            if anchor and title:
                prior_refs.append(f"{anchor} ({title})")
            elif anchor or title:
                prior_refs.append(anchor or title)
            if len(prior_refs) >= 8:
                break
        convergence_instruction = (
            " This is an incremental convergent review. First verify whether prior open findings still exist; "
            f"previous_head_sha: {previous_head_sha or 'unknown'}. "
            "Only report new issues that are directly introduced after that previous head. "
            "Do not report latent pre-existing issues, style preferences, or speculative risks. "
            "When a prior finding still exists, reuse its issue_id exactly. "
            "For new findings, choose deterministic issue_id values from the bug shape and primary path."
        )
        if prior_refs:
            convergence_instruction += f" Prior open findings to verify: {', '.join(prior_refs)}."
    return (
        "Run the Audit Swarm protocol for this repository. "
        "If the agent CLI supports subagents, split suitable independent analysis across multiple "
        "subagents according to repository shape and task scope to reduce context pressure; aggregate "
        "their results yourself and preserve the required final JSON output structure exactly. "
        "Return only JSON with top-level "
        "`audit_protocol`, `issue_cards`, and `verification_results`. Do not return deprecated Pullwise "
        "`findings`. Each issue card is a hypothesis and must include a concrete title, severity "
        "(P0/P1/P2/P3/P4 or critical/high/medium/low/info), one or more repository-relative locations, "
        "a claim, evidence, reproduction_idea, suggested_test, and false_positive_checks. "
        "Each verification result must reference an issue_id and use verdict `confirmed`, `rejected`, "
        "or `inconclusive`; include commands_run only for commands a user can copy to verify the issue. "
        "Do not emit vague concerns. Do not include absolute worker checkout paths or server filesystem paths. "
        "If a candidate has no file/line, no evidence, and no verifiable hypothesis, omit it. "
        f"{language_instruction} "
        f"{convergence_instruction} "
        f"{architecture_instruction}"
        f"Repository: {job.get('repo')} branch: {job.get('branch')} commit: {job.get('commit')}."
    )


def parse_audit_swarm_payload(output: str) -> dict:
    decoder = json.JSONDecoder()
    text = output.strip()
    candidates = [text]
    first = text.find("{")
    last = text.rfind("}")
    if first >= 0 and last > first:
        candidates.append(text[first : last + 1])
    candidates.extend(line.strip() for line in text.splitlines() if line.strip().startswith(("{", "[")))
    matched: dict | None = None
    for candidate in candidates:
        try:
            parsed = decoder.decode(candidate)
        except json.JSONDecodeError:
            continue
        payload = audit_swarm_payload_from_document(parsed)
        if payload is not None:
            matched = payload
    if matched is not None:
        return matched
    raise RuntimeError("review provider did not return an Audit Swarm payload")


def audit_swarm_payload_from_document(parsed: object) -> dict | None:
    if not isinstance(parsed, dict) or "event" in parsed:
        return None
    cards = first_list(parsed, "issue_cards", "issueCards")
    if cards is None:
        return None
    results = first_list(parsed, "verification_results", "verificationResults") or []
    return {
        "audit_protocol": clean_protocol_text(
            parsed.get("audit_protocol") or parsed.get("auditProtocol") or AUDIT_SWARM_PROTOCOL_VERSION
        )
        or AUDIT_SWARM_PROTOCOL_VERSION,
        "issue_cards": [item for item in cards if isinstance(item, dict)],
        "verification_results": [item for item in results if isinstance(item, dict)],
    }


def empty_audit_swarm_payload() -> dict:
    return {
        "audit_protocol": AUDIT_SWARM_PROTOCOL_VERSION,
        "issue_cards": [],
        "verification_results": [],
    }


def merge_audit_swarm_payloads(*payloads: dict) -> dict:
    merged = empty_audit_swarm_payload()
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        protocol = clean_protocol_text(payload.get("audit_protocol") or payload.get("auditProtocol"))
        if protocol:
            merged["audit_protocol"] = protocol
        cards = first_list(payload, "issue_cards", "issueCards") or []
        results = first_list(payload, "verification_results", "verificationResults") or []
        merged["issue_cards"].extend(item for item in cards if isinstance(item, dict))
        merged["verification_results"].extend(item for item in results if isinstance(item, dict))
    return merged


def filter_audit_swarm_payload_by_findings(payload: dict, findings: list[dict]) -> dict:
    reported_ids = {clean_protocol_text(finding.get("id")) for finding in findings if isinstance(finding, dict)}
    raw_cards = first_list(payload, "issue_cards", "issueCards") or []
    filtered_cards = []
    filtered_placeholder_ids = []
    for index, card in enumerate(raw_cards):
        if not isinstance(card, dict):
            continue
        card_id = audit_swarm_resolved_issue_id(card, index)
        if card_id in reported_ids:
            next_card = dict(card)
            next_card["issue_id"] = card_id
            filtered_cards.append(next_card)
            if audit_swarm_placeholder_issue_id(audit_swarm_issue_key(card)):
                filtered_placeholder_ids.append(card_id)
    filtered_card_ids = {audit_swarm_issue_key(card) for card in filtered_cards}
    single_placeholder_id = filtered_placeholder_ids[0] if len(filtered_placeholder_ids) == 1 else ""
    filtered_results = []
    for result in first_list(payload, "verification_results", "verificationResults") or []:
        if not isinstance(result, dict):
            continue
        result_issue_id = audit_swarm_resolved_verification_issue_id(result, single_placeholder_id)
        if result_issue_id in filtered_card_ids:
            next_result = dict(result)
            next_result["issue_id"] = result_issue_id
            filtered_results.append(next_result)
    return {
        "audit_protocol": clean_protocol_text(payload.get("audit_protocol") or payload.get("auditProtocol"))
        or AUDIT_SWARM_PROTOCOL_VERSION,
        "issue_cards": filtered_cards,
        "verification_results": filtered_results,
    }


def audit_swarm_payload_from_findings(findings: list[dict], *, verifier_role: str) -> dict:
    payload = empty_audit_swarm_payload()
    for index, finding in enumerate(findings):
        if not isinstance(finding, dict):
            continue
        card = issue_card_from_finding(finding, index)
        payload["issue_cards"].append(card)
        payload["verification_results"].append(verification_result_from_finding(finding, card, verifier_role))
    return payload


def audit_swarm_location(file_path: str, start_line: int, end_line: int) -> dict:
    if start_line and end_line and end_line != start_line:
        lines = f"{start_line}-{end_line}"
    elif start_line:
        lines = str(start_line)
    elif end_line:
        lines = str(end_line)
    else:
        lines = ""
    return {"file": file_path, "lines": lines, "startLine": start_line, "endLine": end_line}


def public_calibration_card_payload(value: object) -> dict:
    source = value if isinstance(value, dict) else {}
    decision = clean_protocol_text(source.get("decision")).lower()
    if decision not in {"reported", "audit_only", "rejected"}:
        return {}
    score_band = clean_protocol_text(source.get("scoreBand") or source.get("score_band")).lower()
    if score_band not in {"report_band", "audit_band", "reject_band"}:
        score_band = ""
    score_kind = clean_protocol_text(source.get("scoreKind") or source.get("score_kind")).lower()
    if score_kind not in {"ranking_score", "truth_probability"}:
        score_kind = ""
    status = clean_protocol_text(source.get("verificationStatus") or source.get("verification_status")).lower()
    if status not in {"verified", "static_proof", "potential_risk", "unverified"}:
        status = ""
    payload = {
        "protocol": "pullwise-review-calibration-public/0.1",
        "decision": decision,
        "reason": clean_protocol_text(source.get("reason"))[:120],
        "scoreBand": score_band,
        "scoreKind": score_kind,
        "verificationStatus": status,
        "auditOnly": source.get("auditOnly") is True or source.get("audit_only") is True,
        "guardrailApplied": source.get("guardrailApplied") is True or source.get("guardrail_applied") is True,
    }
    return {key: item for key, item in payload.items() if item not in ("", [], {})}


def issue_card_from_finding(finding: dict, index: int) -> dict:
    issue_id = clean_protocol_text(finding.get("id")) or audit_swarm_generated_id(finding, index)
    locations = []
    raw_locations = finding.get("affectedLocations") if isinstance(finding.get("affectedLocations"), list) else []
    for item in raw_locations:
        if not isinstance(item, dict):
            continue
        file_path = safe_repo_relative_file(item.get("file"))
        if file_path:
            start_line = positive_int(item.get("startLine") or item.get("line"))
            end_line = positive_int(item.get("endLine") or item.get("startLine") or item.get("line"))
            locations.append(audit_swarm_location(file_path, start_line, end_line or start_line))
    file_path = safe_repo_relative_file(finding.get("file"))
    line = positive_int(finding.get("line"))
    if file_path and not locations:
        locations.append(audit_swarm_location(file_path, line, line))
    card = {
        "issue_id": issue_id,
        "shard_id": clean_protocol_text(finding.get("category")).lower() or "repository",
        "agent_role": clean_protocol_text(finding.get("agent_role") or finding.get("agentRole")) or "deterministic-reviewer",
        "title": clean_protocol_text(finding.get("title")) or f"Audit candidate {index + 1}",
        "category": clean_protocol_text(finding.get("category")) or "Quality",
        "severity": clean_protocol_text(finding.get("severity")) or "medium",
        "confidence": finding.get("confidence", 0.9),
        "locations": locations,
        "claim": protocol_multiline_text(finding.get("summary") or finding.get("claim") or finding.get("title")),
        "violated_invariants": protocol_text_list(finding.get("violated_invariants") or finding.get("violatedInvariants")),
        "evidence": [
            item if isinstance(item, dict) else protocol_multiline_text(item)
            for item in (finding.get("evidence") if isinstance(finding.get("evidence"), list) else [])
        ],
        "reproduction_idea": protocol_multiline_text(finding.get("reproductionPath")),
        "suggested_test": audit_swarm_suggested_test_from_finding(finding),
        "false_positive_checks": protocol_text_list(finding.get("whyNotFalsePositive")),
        "limitations": protocol_text_list(finding.get("limitations")),
        "impact": protocol_multiline_text(finding.get("impact")),
        "steps": protocol_text_list(finding.get("steps")),
        "references": finding.get("references") if isinstance(finding.get("references"), list) else [],
    }
    public_calibration = public_calibration_card_payload(
        finding.get("reviewCalibration") or finding.get("review_calibration")
    )
    if public_calibration:
        card["review_calibration"] = public_calibration
    return card


def verification_result_from_finding(finding: dict, card: dict, verifier_role: str) -> dict:
    status = clean_protocol_text(finding.get("verificationStatus")).lower()
    commands = []
    reproduction = finding.get("reproduction") if isinstance(finding.get("reproduction"), dict) else {}
    raw_evidence = finding.get("evidence") if isinstance(finding.get("evidence"), list) else []
    verdict = "confirmed" if status in {"verified", "static_proof"} else "inconclusive"
    proof_type = "failing_test" if status == "verified" else "static_proof"
    if status == "verified":
        commands.extend(protocol_text_list(reproduction.get("commands")))
        commands.extend(clean_protocol_text(item.get("command")) for item in raw_evidence if isinstance(item, dict) and item.get("command"))
    return {
        "issue_id": card["issue_id"],
        "verifier_role": verifier_role,
        "verdict": verdict,
        "confidence": finding.get("confidence", 0.9),
        "proof_type": proof_type,
        "proof_strength": 3 if verdict == "confirmed" else 1,
        "evidence": audit_swarm_verification_evidence_from_finding(finding),
        "commands_run": dedupe_text(commands)[:5],
        "result_summary": protocol_multiline_text(finding.get("verificationSummary") or finding.get("summary")),
        "notes_for_fix": protocol_text_list(finding.get("steps")),
    }


def audit_swarm_suggested_test_from_finding(finding: dict) -> str:
    reproduction = finding.get("reproduction") if isinstance(finding.get("reproduction"), dict) else {}
    commands = protocol_text_list(reproduction.get("commands"))
    if commands:
        return f"Run `{commands[0]}`."
    return ""


def audit_swarm_verification_evidence_from_finding(finding: dict) -> list[str]:
    evidence = []
    summary = protocol_multiline_text(finding.get("verificationSummary"))
    if summary:
        evidence.append(summary)
    raw_evidence = finding.get("evidence") if isinstance(finding.get("evidence"), list) else []
    for item in raw_evidence:
        if not isinstance(item, dict):
            continue
        item_summary = protocol_multiline_text(item.get("summary"))
        if item_summary:
            evidence.append(item_summary)
    return dedupe_text(evidence)[:8]


def normalize_audit_swarm_files_for_checkout(payload: dict, checkout_dir: Path) -> dict:
    normalized = merge_audit_swarm_payloads(payload)
    for card in normalized["issue_cards"]:
        raw_locations = card.get("locations") if isinstance(card.get("locations"), list) else []
        for location in raw_locations:
            if isinstance(location, dict):
                location["file"] = normalize_finding_file_for_checkout(location.get("file"), checkout_dir)
        raw_evidence = card.get("evidence") if isinstance(card.get("evidence"), list) else []
        for item in raw_evidence:
            if isinstance(item, dict):
                normalize_audit_swarm_path_fields(item, checkout_dir, "file", "logPath", "log_path")
        normalize_audit_swarm_path_fields(card, checkout_dir, "file", "testFile", "test_file")
        reproduction = card.get("reproduction") if isinstance(card.get("reproduction"), dict) else {}
        normalize_audit_swarm_path_fields(reproduction, checkout_dir, "testFile", "test_file", "logPath", "log_path")
    for result in normalized["verification_results"]:
        if isinstance(result, dict):
            normalize_audit_swarm_path_fields(result, checkout_dir, "logPath", "log_path")
    return normalized


def normalize_audit_swarm_path_fields(item: dict, checkout_dir: Path, *keys: str) -> None:
    for key in keys:
        if key in item:
            item[key] = normalize_finding_file_for_checkout(item.get(key), checkout_dir)


def audit_swarm_findings_from_payload(parsed: object) -> list[dict] | None:
    if not isinstance(parsed, dict) or "event" in parsed:
        return None
    cards = first_list(parsed, "issue_cards", "issueCards")
    if cards is None:
        return None
    verification_results = first_list(parsed, "verification_results", "verificationResults") or []
    by_issue = audit_swarm_verifications_by_issue(verification_results, cards)
    findings = []
    for index, card in enumerate(cards):
        if isinstance(card, dict):
            issue_id = audit_swarm_resolved_issue_id(card, index)
            findings.append(audit_swarm_issue_card_to_finding(card, by_issue.get(issue_id, []), index))
    return findings


def first_list(source: dict, *keys: str) -> list | None:
    for key in keys:
        value = source.get(key)
        if isinstance(value, list):
            return value
    return None


def audit_swarm_issue_key(card: dict) -> str:
    return clean_protocol_text(
        card.get("issue_id")
        or card.get("issueId")
        or card.get("id")
        or card.get("candidate_id")
        or card.get("candidateId")
    )


def audit_swarm_placeholder_issue_id(issue_id: str) -> bool:
    return not issue_id or issue_id.lower() in {"null", "none"}


def audit_swarm_resolved_issue_id(card: dict, index: int) -> str:
    issue_id = audit_swarm_issue_key(card)
    return audit_swarm_generated_id(card, index) if audit_swarm_placeholder_issue_id(issue_id) else issue_id


def audit_swarm_single_placeholder_issue_id(cards: list) -> str:
    placeholder_ids = [
        audit_swarm_resolved_issue_id(card, index)
        for index, card in enumerate(cards)
        if isinstance(card, dict) and audit_swarm_placeholder_issue_id(audit_swarm_issue_key(card))
    ]
    return placeholder_ids[0] if len(placeholder_ids) == 1 else ""


def audit_swarm_resolved_verification_issue_id(result: dict, single_placeholder_id: str = "") -> str:
    issue_id = audit_swarm_issue_key(result)
    return single_placeholder_id if audit_swarm_placeholder_issue_id(issue_id) else issue_id


def audit_swarm_verifications_by_issue(results: list, cards: list | None = None) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    single_placeholder_id = audit_swarm_single_placeholder_issue_id(cards or [])
    for result in results:
        if not isinstance(result, dict):
            continue
        issue_id = audit_swarm_resolved_verification_issue_id(result, single_placeholder_id)
        if not issue_id:
            continue
        grouped.setdefault(issue_id, []).append(result)
    return grouped


def audit_swarm_issue_card_to_finding(card: dict, verifications: list[dict], index: int) -> dict:
    issue_id = audit_swarm_resolved_issue_id(card, index)
    locations = audit_swarm_locations(card)
    primary = locations[0] if locations else {}
    verdict = audit_swarm_verdict(verifications)
    severity = audit_swarm_severity(card.get("severity"))
    category = audit_swarm_category(card)
    evidence = audit_swarm_evidence(card, verifications, primary)
    reproduction = audit_swarm_reproduction(card, verifications)
    confidence = audit_swarm_confidence(card.get("confidence"), verdict)
    finding_id = issue_id
    claim = protocol_multiline_text(card.get("claim") or card.get("summary") or card.get("description"))
    title = clean_protocol_text(card.get("title")) or f"Audit candidate {index + 1}"
    verification_summary = audit_swarm_verification_summary(verifications, verdict)
    invariants = protocol_text_list(card.get("violated_invariants") or card.get("violatedInvariants"))
    false_positive_checks = protocol_text_list(card.get("false_positive_checks") or card.get("falsePositiveChecks"))
    limitations = [
        *(f"Violated invariant: {item}" for item in invariants),
        *(f"False-positive check: {item}" for item in false_positive_checks),
        *protocol_text_list(card.get("limitations")),
    ]
    why_not_false_positive = audit_swarm_positive_checks(verifications)
    return {
        "id": finding_id,
        "severity": severity,
        "category": category,
        "title": title,
        "summary": claim or title,
        "impact": protocol_multiline_text(card.get("impact")) or audit_swarm_impact_from_invariants(invariants),
        "detectionReasoning": audit_swarm_detection_reasoning(card, verifications),
        "reproductionPath": audit_swarm_reproduction_path(card, verifications),
        "verificationStatus": audit_swarm_verification_status(verdict, verifications),
        "verificationSummary": verification_summary,
        "affectedLocations": locations,
        "evidence": evidence,
        "reproduction": reproduction,
        "whyNotFalsePositive": why_not_false_positive,
        "limitations": limitations[:8],
        "file": str(primary.get("file") or ""),
        "line": int(primary.get("startLine") or 0),
        "confidence": confidence,
        "confidenceRationale": audit_swarm_confidence_rationale(card, verifications, verdict, confidence),
        "autoFix": False,
        "effort": clean_protocol_text(card.get("effort")) or "review required",
        "fixBenefits": protocol_multiline_text(card.get("fixBenefits") or card.get("fix_benefits")),
        "fixRisks": protocol_multiline_text(card.get("fixRisks") or card.get("fix_risks")),
        "tags": audit_swarm_tags(card, verifications),
        "steps": audit_swarm_steps(card),
        "badCode": [],
        "goodCode": [],
        "references": audit_swarm_references(card),
        "_auditSwarmVerdict": verdict,
        "_auditSwarmRole": clean_protocol_text(card.get("agent_role") or card.get("agentRole")),
        "_auditSwarmShard": clean_protocol_text(card.get("shard_id") or card.get("shardId")),
    }


def audit_swarm_generated_id(card: dict, index: int) -> str:
    seed = json.dumps(card, ensure_ascii=False, sort_keys=True, default=str)
    digest = hashlib.sha1(f"{index}:{seed}".encode("utf-8")).hexdigest()[:10]
    return f"audit_swarm_{digest}"


def audit_swarm_verdict(verifications: list[dict]) -> str:
    verdicts = [clean_protocol_text(item.get("verdict")).lower() for item in verifications if isinstance(item, dict)]
    if any(
        clean_protocol_text(item.get("verdict")).lower() == "confirmed"
        and audit_swarm_confirmed_verification_has_support(item)
        for item in verifications
        if isinstance(item, dict)
    ):
        return "confirmed"
    if verdicts and all(item == "rejected" for item in verdicts):
        return "rejected"
    if "inconclusive" in verdicts:
        return "inconclusive"
    return "candidate"


def audit_swarm_confirmed_verification_has_support(result: dict) -> bool:
    if protocol_text_list(result.get("commands_run") or result.get("commandsRun")):
        return True
    if protocol_text_list(result.get("evidence")):
        return True
    if protocol_multiline_text(result.get("result_summary") or result.get("resultSummary") or result.get("summary")):
        return True
    if protocol_multiline_text(result.get("output")):
        return True
    if clean_protocol_text(result.get("logPath") or result.get("log_path")):
        return True
    return False


def audit_swarm_verification_status(verdict: str, verifications: list[dict]) -> str:
    if verdict == "confirmed":
        proof_types = {
            clean_protocol_text(item.get("proof_type") or item.get("proofType")).lower()
            for item in verifications
            if isinstance(item, dict)
        }
        has_command = any(protocol_text_list(item.get("commands_run") or item.get("commandsRun")) for item in verifications)
        if proof_types & {"failing_test", "runtime_log", "test", "command"} or has_command:
            return "verified"
        return "static_proof"
    if verdict == "rejected":
        return "unverified"
    if verdict == "inconclusive":
        return "potential_risk"
    return "potential_risk"


def audit_swarm_severity(value: object) -> str:
    severity = clean_protocol_text(value).lower()
    mapping = {
        "p0": "critical",
        "p1": "high",
        "p2": "medium",
        "p3": "low",
        "p4": "info",
        "critical": "critical",
        "high": "high",
        "medium": "medium",
        "low": "low",
        "info": "info",
    }
    return mapping.get(severity, "medium")


def audit_swarm_category(card: dict) -> str:
    raw = " ".join(
        clean_protocol_text(value).lower()
        for value in (card.get("category"), card.get("agent_role"), card.get("agentRole"))
        if clean_protocol_text(value)
    )
    if "security" in raw or "auth" in raw or "permission" in raw:
        return "Security"
    if "performance" in raw:
        return "Performance"
    if "dependency" in raw or "cve" in raw:
        return "Dependencies"
    if "test" in raw or "coverage" in raw:
        return "Tests"
    if "doc" in raw:
        return "Docs"
    if "architecture" in raw or "contract" in raw or "api" in raw:
        return "Architecture"
    return "Quality"


def audit_swarm_confidence(value: object, verdict: str) -> float:
    try:
        confidence = float(value)
    except (TypeError, ValueError, OverflowError):
        confidence = 0.7
    confidence = max(0.0, min(1.0, confidence))
    if verdict == "confirmed":
        return max(confidence, 0.85)
    if verdict == "rejected":
        return min(confidence, 0.2)
    if verdict == "inconclusive":
        return min(confidence, 0.79)
    return confidence


def audit_swarm_locations(card: dict) -> list[dict]:
    raw_locations = first_list(card, "locations", "affectedLocations", "affected_locations") or []
    locations = []
    seen = set()
    for item in raw_locations:
        if not isinstance(item, dict):
            continue
        file_path = safe_repo_relative_file(item.get("file") or item.get("path"))
        if not file_path:
            continue
        start_line, end_line = audit_swarm_line_range(item)
        key = (file_path, start_line, end_line)
        if key in seen:
            continue
        seen.add(key)
        locations.append({"file": file_path, "startLine": start_line, "endLine": end_line})
    file_path = safe_repo_relative_file(card.get("file"))
    if file_path:
        line = positive_int(card.get("line"))
        key = (file_path, line, line)
        if key not in seen:
            locations.append({"file": file_path, "startLine": line, "endLine": line})
    return locations[:10]


def audit_swarm_line_range(item: dict) -> tuple[int, int]:
    start = positive_int(item.get("startLine") or item.get("start_line") or item.get("line"))
    end = positive_int(item.get("endLine") or item.get("end_line"))
    lines = clean_protocol_text(item.get("lines") or item.get("lineRange") or item.get("line_range"))
    if lines and not start:
        match = re.search(r"(\d+)(?:\s*[-:]\s*(\d+))?", lines)
        if match:
            start = int(match.group(1))
            end = int(match.group(2) or match.group(1))
    if start and (not end or end < start):
        end = start
    return start, end


def audit_swarm_evidence(card: dict, verifications: list[dict], primary: dict) -> list[dict]:
    evidence = []
    role = clean_protocol_text(card.get("agent_role") or card.get("agentRole")) or "discovery agent"
    for index, item in enumerate(first_list(card, "evidence") or []):
        if isinstance(item, dict):
            summary = protocol_multiline_text(item.get("summary") or item.get("claim") or item.get("text"))
            file_path = safe_repo_relative_file(item.get("file") or item.get("path")) or str(primary.get("file") or "")
            start_line, end_line = audit_swarm_line_range(item)
            record = {
                "type": audit_swarm_evidence_type(item.get("type"), default="code" if file_path else "path"),
                "label": clean_protocol_text(item.get("label")) or f"{role} evidence",
                "summary": summary,
                "file": file_path,
                "startLine": start_line or int(primary.get("startLine") or 0),
                "endLine": end_line or int(primary.get("endLine") or primary.get("startLine") or 0),
                "command": clean_protocol_text(item.get("command")),
                "exitCode": positive_int(item.get("exitCode") or item.get("exit_code")),
                "logPath": clean_protocol_text(item.get("logPath") or item.get("log_path")),
                "output": protocol_multiline_text(item.get("output"))[:4000],
                "url": clean_protocol_text(item.get("url")),
            }
        else:
            summary = protocol_multiline_text(item)
            record = {
                "type": "code" if primary.get("file") else "path",
                "label": f"{role} evidence" if index == 0 else "Discovery evidence",
                "summary": summary,
                "file": str(primary.get("file") or ""),
                "startLine": int(primary.get("startLine") or 0),
                "endLine": int(primary.get("endLine") or primary.get("startLine") or 0),
                "command": "",
                "exitCode": 0,
                "logPath": "",
                "output": "",
                "url": "",
            }
        if any(record.get(key) for key in ("summary", "file", "command", "logPath", "output", "url")):
            evidence.append(record)
    for result in verifications:
        if not isinstance(result, dict):
            continue
        verifier_role = clean_protocol_text(result.get("verifier_role") or result.get("verifierRole")) or "verifier"
        proof_type = clean_protocol_text(result.get("proof_type") or result.get("proofType"))
        evidence_type = audit_swarm_evidence_type(proof_type, default="test" if proof_type else "tool")
        commands = protocol_text_list(result.get("commands_run") or result.get("commandsRun"))
        for index, summary in enumerate(protocol_text_list(result.get("evidence"))):
            evidence.append(
                {
                    "type": evidence_type,
                    "label": f"{verifier_role} verification" if index == 0 else "Verification evidence",
                    "summary": summary,
                    "file": str(primary.get("file") or ""),
                    "startLine": int(primary.get("startLine") or 0),
                    "endLine": int(primary.get("endLine") or primary.get("startLine") or 0),
                    "command": commands[0] if commands else "",
                    "exitCode": 0,
                    "logPath": clean_protocol_text(result.get("logPath") or result.get("log_path")),
                    "output": protocol_multiline_text(result.get("output"))[:4000],
                    "url": "",
                }
            )
    return evidence[:20]


def audit_swarm_evidence_type(value: object, *, default: str) -> str:
    raw = clean_protocol_text(value).lower()
    if raw in {"failing_test", "test"}:
        return "test"
    if raw in {"runtime", "runtime_log", "command"}:
        return "runtime_log"
    if raw in {"static", "static_proof", "code"}:
        return "code"
    if raw in {"path", "reachability", "data_flow", "data-flow"}:
        return "path"
    if raw in {"trigger", "input"}:
        return "trigger"
    if raw in {"documentation", "docs"}:
        return "documentation"
    if raw in {"fix", "fix_verification"}:
        return "fix_verification"
    if raw in {"tool", "environment"}:
        return raw
    return default


def audit_swarm_reproduction(card: dict, verifications: list[dict]) -> dict:
    commands = []
    for result in verifications:
        if isinstance(result, dict):
            commands.extend(protocol_text_list(result.get("commands_run") or result.get("commandsRun")))
    reproduction = card.get("reproduction") if isinstance(card.get("reproduction"), dict) else {}
    commands.extend(protocol_text_list(reproduction.get("commands")))
    commands = dedupe_text(commands)[:5]
    return {
        "commands": commands,
        "input": protocol_multiline_text(reproduction.get("input") or card.get("trigger") or card.get("input")),
        "expected": protocol_multiline_text(reproduction.get("expected") or card.get("expected")),
        "actual": audit_swarm_actual_result(verifications) or protocol_multiline_text(reproduction.get("actual") or card.get("actual")),
        "testFile": clean_protocol_text(reproduction.get("testFile") or reproduction.get("test_file") or card.get("test_file") or card.get("testFile")),
        "logPath": clean_protocol_text(reproduction.get("logPath") or reproduction.get("log_path")),
    }


def audit_swarm_actual_result(verifications: list[dict]) -> str:
    for result in verifications:
        if not isinstance(result, dict):
            continue
        summary = protocol_multiline_text(result.get("result_summary") or result.get("resultSummary"))
        if summary:
            return summary
    return ""


def audit_swarm_detection_reasoning(card: dict, verifications: list[dict]) -> str:
    parts = []
    role = clean_protocol_text(card.get("agent_role") or card.get("agentRole"))
    shard = clean_protocol_text(card.get("shard_id") or card.get("shardId"))
    if role or shard:
        parts.append(f"{role or 'reviewer'} reported this candidate" + (f" in shard `{shard}`." if shard else "."))
    claim = protocol_multiline_text(card.get("claim"))
    if claim:
        parts.append(f"Claim: {claim}")
    for invariant in protocol_text_list(card.get("violated_invariants") or card.get("violatedInvariants"))[:3]:
        parts.append(f"Violated invariant: {invariant}")
    for result in verifications[:3]:
        if not isinstance(result, dict):
            continue
        verifier = clean_protocol_text(result.get("verifier_role") or result.get("verifierRole")) or "verifier"
        verdict = clean_protocol_text(result.get("verdict"))
        summary = protocol_multiline_text(result.get("result_summary") or result.get("resultSummary"))
        if verdict or summary:
            parts.append(f"{verifier} verdict: {verdict or 'reviewed'}" + (f" - {summary}" if summary else "."))
    return " ".join(parts)[:1200]


def audit_swarm_reproduction_path(card: dict, verifications: list[dict]) -> str:
    parts = []
    reproduction_idea = protocol_multiline_text(card.get("reproduction_idea") or card.get("reproductionIdea"))
    suggested_test = protocol_multiline_text(card.get("suggested_test") or card.get("suggestedTest"))
    if reproduction_idea:
        parts.append(reproduction_idea)
    if suggested_test:
        parts.append(f"Suggested test: {suggested_test}")
    for result in verifications:
        if not isinstance(result, dict):
            continue
        commands = protocol_text_list(result.get("commands_run") or result.get("commandsRun"))
        if commands:
            parts.append(f"Verifier command: {commands[0]}")
            break
    return " ".join(parts)[:1000]


def audit_swarm_verification_summary(verifications: list[dict], verdict: str) -> str:
    for result in verifications:
        if not isinstance(result, dict):
            continue
        summary = protocol_multiline_text(result.get("result_summary") or result.get("resultSummary"))
        if summary:
            role = clean_protocol_text(result.get("verifier_role") or result.get("verifierRole"))
            return f"{role}: {summary}" if role else summary
    if verdict == "confirmed":
        return "Audit verifier confirmed this candidate."
    if verdict == "rejected":
        return "Audit verifier rejected this candidate before reporting."
    if verdict == "inconclusive":
        return "Audit verifier could not conclusively prove or disprove this candidate."
    return "Discovery candidate has not been independently verified."


def audit_swarm_confidence_rationale(card: dict, verifications: list[dict], verdict: str, confidence: float) -> str:
    explicit = protocol_multiline_text(card.get("confidenceRationale") or card.get("confidence_rationale"))
    if explicit:
        return explicit
    if verifications:
        return f"Audit Swarm verdict is {verdict}; projected confidence is {confidence:.2f} after verifier evidence."
    return f"Discovery confidence is {confidence:.2f}; no separate verifier result was supplied in the payload."


def audit_swarm_impact_from_invariants(invariants: list[str]) -> str:
    if invariants:
        return f"The finding may violate this required behavior: {invariants[0]}"
    return ""


def audit_swarm_positive_checks(verifications: list[dict]) -> list[str]:
    checks = []
    for result in verifications:
        if not isinstance(result, dict):
            continue
        role = clean_protocol_text(result.get("verifier_role") or result.get("verifierRole")) or "verifier"
        for item in protocol_text_list(result.get("evidence"))[:3]:
            checks.append(f"{role}: {item}")
    return dedupe_text(checks)[:6]


def audit_swarm_tags(card: dict, verifications: list[dict]) -> list[str]:
    tags = ["audit-swarm"]
    tags.extend(protocol_text_list(card.get("risk_tags") or card.get("riskTags")))
    tags.extend(protocol_text_list(card.get("tags")))
    for value in (card.get("agent_role"), card.get("agentRole"), card.get("shard_id"), card.get("shardId")):
        text = clean_protocol_text(value)
        if text:
            tags.append(text)
    for result in verifications:
        if isinstance(result, dict):
            role = clean_protocol_text(result.get("verifier_role") or result.get("verifierRole"))
            if role:
                tags.append(role)
    return [slugify_tag(tag) for tag in dedupe_text(tags) if slugify_tag(tag)][:12]


def audit_swarm_steps(card: dict) -> list[str]:
    steps = protocol_text_list(card.get("steps"))
    suggested_test = protocol_multiline_text(card.get("suggested_test") or card.get("suggestedTest"))
    remediation = protocol_multiline_text(card.get("remediation") or card.get("fix"))
    if suggested_test:
        steps.append(f"Add or run the suggested test: {suggested_test}")
    if remediation:
        steps.append(remediation)
    return dedupe_text(steps)[:8]


def audit_swarm_references(card: dict) -> list[dict]:
    references = []
    for item in first_list(card, "references") or []:
        if isinstance(item, dict):
            label = clean_protocol_text(item.get("label")) or clean_protocol_text(item.get("url"))
            url = clean_protocol_text(item.get("url"))
        else:
            label = clean_protocol_text(item)
            url = clean_protocol_text(item)
        if label and url.startswith(("http://", "https://")):
            references.append({"label": label, "url": url})
    return references[:10]


def protocol_text_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [text for text in (protocol_multiline_text(item) for item in value) if text]


def protocol_text_items(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    items = []
    for item in value:
        if isinstance(item, dict):
            text = protocol_multiline_text(item.get("summary") or item.get("text") or item.get("claim") or item.get("label"))
        else:
            text = protocol_multiline_text(item)
        if text:
            items.append(text)
    return items


def clean_protocol_text(value: object) -> str:
    if isinstance(value, bool) or value is None:
        return ""
    text = str(value).replace("\x00", "")
    lines = text.splitlines()
    text = lines[0] if lines else text
    text = text.strip()
    return text[:500]


def protocol_multiline_text(value: object) -> str:
    if isinstance(value, bool) or value is None:
        return ""
    text = str(value).replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n").strip()
    return text[:4000]


def dedupe_text(items: list[str]) -> list[str]:
    deduped = []
    seen = set()
    for item in items:
        text = protocol_multiline_text(item)
        if text and text not in seen:
            seen.add(text)
            deduped.append(text)
    return deduped


def slugify_tag(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")[:40]


def normalize_finding_files_for_checkout(findings: list[dict], checkout_dir: Path) -> list[dict]:
    normalized: list[dict] = []
    for finding in findings:
        item = dict(finding)
        item["file"] = normalize_finding_file_for_checkout(item.get("file"), checkout_dir)
        normalized.append(item)
    return normalized


def normalize_finding_file_for_checkout(value: object, checkout_dir: Path) -> str:
    relative_path = relative_file_inside_checkout(value, checkout_dir)
    return safe_repo_relative_file(relative_path if relative_path is not None else value)


def relative_file_inside_checkout(value: object, checkout_dir: Path) -> str | None:
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw or any(char in raw for char in "\r\n\x00"):
        return None
    normalized = raw.replace("\\", "/")
    if not (normalized.startswith("/") or _WINDOWS_DRIVE_RE.match(raw)):
        return None

    root = str(checkout_dir.resolve(strict=False)).replace("\\", "/").rstrip("/")
    root_prefix = f"{root}/"
    if normalized.casefold() == root.casefold():
        return ""
    if normalized.casefold().startswith(root_prefix.casefold()):
        return normalized[len(root_prefix) :]
    return None


def safe_repo_relative_file(value: object) -> str:
    if not isinstance(value, str):
        return ""
    raw = value.strip()
    normalized = raw.replace("\\", "/")
    if (
        not raw
        or any(char in raw for char in "\r\n\x00")
        or _WINDOWS_DRIVE_RE.match(raw)
        or normalized.startswith("/")
        or normalized.startswith("//")
        or raw.startswith("\\")
    ):
        return ""
    parts = normalized.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        return ""
    if any(part.casefold() == ".git" for part in parts):
        return ""
    return "/".join(parts)


def audit_swarm_output_schema() -> dict:
    location = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "file": {"type": "string"},
            "lines": {"type": "string"},
            "startLine": {"type": "integer"},
            "endLine": {"type": "integer"},
        },
        "required": ["file", "lines", "startLine", "endLine"],
    }
    evidence = {
        "anyOf": [
            {"type": "string"},
            {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "type": {"type": "string"},
                    "label": {"type": "string"},
                    "summary": {"type": "string"},
                    "file": {"type": "string"},
                    "startLine": {"type": "integer"},
                    "endLine": {"type": "integer"},
                    "command": {"type": "string"},
                    "exitCode": {"type": "integer"},
                    "logPath": {"type": "string"},
                    "output": {"type": "string"},
                    "url": {"type": "string"},
                },
                "required": [
                    "type",
                    "label",
                    "summary",
                    "file",
                    "startLine",
                    "endLine",
                    "command",
                    "exitCode",
                    "logPath",
                    "output",
                    "url",
                ],
            },
        ]
    }
    issue_card = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "issue_id": {"type": "string"},
            "shard_id": {"type": "string"},
            "agent_role": {"type": "string"},
            "title": {"type": "string"},
            "category": {"type": "string"},
            "severity": {"type": "string"},
            "confidence": {"type": "number"},
            "locations": {"type": "array", "items": location},
            "claim": {"type": "string"},
            "violated_invariants": {"type": "array", "items": {"type": "string"}},
            "evidence": {"type": "array", "items": evidence},
            "reproduction_idea": {"type": "string"},
            "suggested_test": {"type": "string"},
            "false_positive_checks": {"type": "array", "items": {"type": "string"}},
            "limitations": {"type": "array", "items": {"type": "string"}},
        },
        "required": [
            "issue_id",
            "shard_id",
            "agent_role",
            "title",
            "category",
            "severity",
            "confidence",
            "locations",
            "claim",
            "violated_invariants",
            "evidence",
            "reproduction_idea",
            "suggested_test",
            "false_positive_checks",
            "limitations",
        ],
    }
    verification_result = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "issue_id": {"type": "string"},
            "verifier_role": {"type": "string"},
            "verdict": {"type": "string", "enum": ["confirmed", "rejected", "inconclusive"]},
            "confidence": {"type": "number"},
            "proof_type": {"type": "string"},
            "proof_strength": {"type": "integer"},
            "evidence": {"type": "array", "items": {"type": "string"}},
            "commands_run": {"type": "array", "items": {"type": "string"}},
            "result_summary": {"type": "string"},
            "notes_for_fix": {"type": "array", "items": {"type": "string"}},
        },
        "required": [
            "issue_id",
            "verifier_role",
            "verdict",
            "confidence",
            "proof_type",
            "proof_strength",
            "evidence",
            "commands_run",
            "result_summary",
            "notes_for_fix",
        ],
    }
    return {
        "type": "object",
        "required": ["audit_protocol", "issue_cards", "verification_results"],
        "additionalProperties": False,
        "properties": {
            "audit_protocol": {"type": "string"},
            "issue_cards": {"type": "array", "items": issue_card, "maxItems": 25},
            "verification_results": {"type": "array", "items": verification_result, "maxItems": 50},
        },
    }


