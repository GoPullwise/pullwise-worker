from __future__ import annotations

# Loaded by main.py; keep definitions in that module's globals for compatibility.

def summarize(findings: list[dict]) -> dict:
    summary = {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0}
    for finding in findings:
        severity = str(finding.get("severity") or "low").lower()
        if severity not in summary:
            severity = "low"
        summary[severity] += 1
    return summary


def positive_int(value: object) -> int:
    if isinstance(value, bool):
        return 0
    try:
        number = int(value or 0)
    except (OverflowError, TypeError, ValueError):
        return 0
    return number if number > 0 else 0


def finding_precise_location(finding: dict) -> bool:
    if safe_repo_relative_file(finding.get("file")) and positive_int(finding.get("line")):
        return True
    raw_locations = finding.get("affectedLocations") if isinstance(finding.get("affectedLocations"), list) else []
    for item in raw_locations:
        if not isinstance(item, dict):
            continue
        if safe_repo_relative_file(item.get("file")) and positive_int(item.get("startLine") or item.get("line")):
            return True
    raw_evidence = finding.get("evidence") if isinstance(finding.get("evidence"), list) else []
    for item in raw_evidence:
        if not isinstance(item, dict):
            continue
        if safe_repo_relative_file(item.get("file")) and positive_int(item.get("startLine") or item.get("line")):
            return True
    return False


def finding_structured_evidence(finding: dict) -> bool:
    raw_evidence = finding.get("evidence") if isinstance(finding.get("evidence"), list) else []
    for item in raw_evidence:
        if not isinstance(item, dict):
            continue
        has_summary = evidence_summary_is_substantive(item.get("summary"))
        has_command = reproduction_command_looks_executable(item.get("command"))
        has_log = evidence_log_path_is_structured(item.get("logPath") or item.get("log_path")) and evidence_has_verifier_result(item)
        has_file_line = safe_repo_relative_file(item.get("file")) and positive_int(item.get("startLine") or item.get("line"))
        if has_summary and (has_command or has_log or has_file_line):
            return True
    return False


def finding_reproduction_evidence(finding: dict) -> bool:
    reproduction = finding.get("reproduction") if isinstance(finding.get("reproduction"), dict) else {}
    commands = reproduction.get("commands") if isinstance(reproduction.get("commands"), list) else []
    if any(reproduction_command_looks_executable(command) for command in commands):
        return True
    return reproduction_path_has_executable_command(finding.get("reproductionPath"))


def evidence_summary_is_substantive(value: object) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    normalized = normalized_fingerprint_text(text).strip(" .:-")
    return normalized not in {
        "concrete evidence",
        "evidence",
        "source proof",
        "proof",
        "verified",
        "confirmed",
        "issue confirmed",
        "bug confirmed",
        "see evidence",
        "see above",
        "see code",
        "manual inspection",
        "manually inspected",
        "the issue exists",
        "this is a bug",
    }


def evidence_output_is_substantive(value: object) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    normalized = normalized_fingerprint_text(text).strip(" .:-")
    return normalized not in {
        "ok",
        "pass",
        "passed",
        "success",
        "successful",
        "succeeded",
        "no output",
        "none",
        "n/a",
        "na",
    }


def evidence_has_verifier_result(item: dict) -> bool:
    if evidence_output_is_substantive(item.get("output")):
        return True
    if item.get("outputRedacted") is True or item.get("output_redacted") is True:
        return True
    return positive_int(item.get("exitCode") or item.get("exit_code")) > 0


def evidence_log_path_is_structured(value: object) -> bool:
    path = safe_repo_relative_file(value)
    if not path or re.search(r"\s", path):
        return False
    if path.startswith((".pullwise/", "verification/")):
        return True
    return bool(re.search(r"\.(log|txt|json|out|err|trace)\Z", path, flags=re.IGNORECASE))


def reproduction_path_has_executable_command(value: object) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    verifier_match = re.search(r"Verifier command:\s*([^`.;\n\r]+)", text, flags=re.IGNORECASE)
    if verifier_match and reproduction_command_looks_executable(verifier_match.group(1)):
        return True
    return any(reproduction_command_looks_executable(match) for match in re.findall(r"`([^`]+)`", text))


def reproduction_command_looks_executable(command: object) -> bool:
    text = str(command or "").strip()
    if not text or "\n" in text or "\r" in text:
        return False
    parts = text.split(maxsplit=1)
    first = parts[0].strip("\"'")
    if first.startswith(("./", ".\\", "scripts/", "bin/")):
        return True
    if len(parts) < 2 or not parts[1].strip():
        return False
    args = parts[1].strip().lower().split()
    if args and args[0] in {"--version", "-v", "version", "--help", "-h", "help"}:
        return False
    executable = first.rsplit("/", 1)[-1].rsplit("\\", 1)[-1].lower()
    executable = executable[:-4] if executable.endswith(".exe") else executable
    return executable in {
        "bun",
        "cargo",
        "deno",
        "docker",
        "dotnet",
        "go",
        "gradle",
        "java",
        "make",
        "mvn",
        "node",
        "npm",
        "npx",
        "pnpm",
        "pytest",
        "python",
        "python3",
        "ruby",
        "ruff",
        "tox",
        "uv",
        "yarn",
    }


def finding_has_false_positive_check(finding: dict) -> bool:
    for key in ("whyNotFalsePositive", "false_positive_checks", "falsePositiveChecks"):
        values = finding.get(key) if isinstance(finding.get(key), list) else []
        if any(false_positive_check_is_substantive(item) for item in values):
            return True
    limitations = finding.get("limitations") if isinstance(finding.get("limitations"), list) else []
    return any(
        false_positive_check_is_substantive(
            re.sub(r"^.*?false-positive check:\s*", "", str(item or ""), flags=re.IGNORECASE)
        )
        for item in limitations
        if "false-positive check:" in str(item or "").strip().lower()
    )


def false_positive_check_is_substantive(value: object) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    normalized = normalized_fingerprint_text(text).strip(" .:-")
    return normalized not in {
        "n/a",
        "na",
        "none",
        "not applicable",
        "unknown",
        "unchecked",
        "not checked",
        "not verified",
        "no check",
        "no false positive check",
        "could not verify",
        "cannot verify",
        "todo",
        "tbd",
    }


def reportability_rejection_reason(finding: object) -> str:
    if not isinstance(finding, dict):
        return "invalid_candidate"
    if not str(finding.get("title") or "").strip():
        return "missing_title"
    structured_evidence = finding_structured_evidence(finding)
    reproduction_evidence = finding_reproduction_evidence(finding)
    if finding_has_verification_proof(finding) and finding_has_independent_verification_support(finding):
        return ""
    if structured_evidence or reproduction_evidence:
        if not finding_has_false_positive_check(finding):
            return "missing_false_positive_check"
        return ""
    return "missing_evidence"


def finding_has_independent_verification_support(finding: dict) -> bool:
    raw_evidence = finding.get("evidence") if isinstance(finding.get("evidence"), list) else []
    for item in raw_evidence:
        if not isinstance(item, dict):
            continue
        has_summary = evidence_summary_is_substantive(item.get("summary"))
        has_result = evidence_has_verifier_result(item)
        if reproduction_command_looks_executable(item.get("command")) and has_result:
            return True
        if evidence_log_path_is_structured(item.get("logPath") or item.get("log_path")) and has_summary and has_result:
            return True
    return False


def rejected_candidate_sample(finding: object, reason: str) -> dict:
    sample = {"reason": reason}
    if not isinstance(finding, dict):
        return sample
    title = str(finding.get("title") or "").strip()
    if title:
        sample["title"] = title[:160]
    severity = str(finding.get("severity") or "").strip().lower()
    if severity in {"critical", "high", "medium", "low", "info"}:
        sample["severity"] = severity
    category = str(finding.get("category") or "").strip()
    if category:
        sample["category"] = category[:80]
    file_path = safe_repo_relative_file(finding.get("file"))
    if file_path:
        sample["file"] = file_path
    line = positive_int(finding.get("line"))
    if line:
        sample["line"] = line
    status = str(finding.get("verificationStatus") or "").strip().lower()
    if status in _VERIFICATION_STATUSES:
        sample["verificationStatus"] = status
    return sample


def filter_reportable_findings(
    findings: list[dict],
    decision_records: list[dict] | None = None,
) -> tuple[list[dict], dict[str, int], list[dict]]:
    reportable: list[dict] = []
    rejected_reasons: dict[str, int] = {}
    rejected_samples: list[dict] = []
    for finding in findings:
        reason = reportability_rejection_reason(finding)
        if not reason:
            reportable.append(finding)
            continue
        rejected_reasons[reason] = rejected_reasons.get(reason, 0) + 1
        if len(rejected_samples) < 5:
            rejected_samples.append(rejected_candidate_sample(finding, reason))
        if decision_records is not None:
            decision_records.append(
                {
                    "stage": "reportability",
                    "decision": "rejected",
                    "reason": reason,
                    "finding": finding if isinstance(finding, dict) else {},
                }
            )
    return reportable, rejected_reasons, rejected_samples


def normalized_fingerprint_text(value: object) -> str:
    text = re.sub(r"\s+", " ", str(value or "").strip().lower())
    return re.sub(r"[^a-z0-9_./:-]+", " ", text).strip()


def finding_primary_file(finding: dict) -> str:
    file_path = safe_repo_relative_file(finding.get("file"))
    if file_path:
        return file_path
    for key in ("affectedLocations", "evidence"):
        items = finding.get(key) if isinstance(finding.get(key), list) else []
        for item in items:
            if not isinstance(item, dict):
                continue
            file_path = safe_repo_relative_file(item.get("file"))
            if file_path:
                return file_path
    return ""


def finding_source(finding: dict) -> str:
    source = str(finding.get("_auditSwarmRole") or finding.get("source") or finding.get("category") or "reviewer")
    return normalized_source_key(source) or "reviewer"


def normalized_source_key(value: object) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()[:80]


def finding_fingerprint(finding: dict) -> str:
    parts = [
        normalized_fingerprint_text(finding.get("category")),
        normalized_fingerprint_text(finding_primary_file(finding)),
        normalized_fingerprint_text(finding.get("title") or finding.get("summary")),
        normalized_fingerprint_text(finding.get("summary") or finding.get("impact")),
    ]
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()
    return digest


def finding_delta_files(finding: dict) -> set[str]:
    files = set()
    primary = finding_primary_file(finding)
    if primary:
        files.add(primary)
    for key in ("affectedLocations", "evidence"):
        items = finding.get(key) if isinstance(finding.get(key), list) else []
        for item in items:
            if isinstance(item, dict):
                file_path = safe_repo_relative_file(item.get("file"))
                if file_path:
                    files.add(file_path)
    return files


def finding_primary_line(finding: dict) -> int:
    _file_path, line = finding_primary_line_location(finding)
    return line


def finding_primary_line_location(finding: dict) -> tuple[str, int]:
    top_file = safe_repo_relative_file(finding.get("file"))
    top_line = positive_int(finding.get("line") or finding.get("startLine"))
    if top_file and top_line:
        return top_file, top_line
    if top_file:
        for key in ("affectedLocations", "evidence"):
            items = finding.get(key) if isinstance(finding.get(key), list) else []
            for item in items:
                if not isinstance(item, dict):
                    continue
                file_path = safe_repo_relative_file(item.get("file"))
                line = positive_int(item.get("startLine") or item.get("line"))
                if file_path == top_file and line:
                    return top_file, line
    for key in ("affectedLocations", "evidence"):
        items = finding.get(key) if isinstance(finding.get(key), list) else []
        for item in items:
            if not isinstance(item, dict):
                continue
            file_path = safe_repo_relative_file(item.get("file"))
            line = positive_int(item.get("startLine") or item.get("line"))
            if file_path and line:
                return file_path, line
    return top_file, 0


def finding_line_locations(finding: dict) -> list[tuple[str, int]]:
    locations: list[tuple[str, int]] = []
    top_file = safe_repo_relative_file(finding.get("file"))
    top_line = positive_int(finding.get("line") or finding.get("startLine"))
    if top_file and top_line:
        locations.append((top_file, top_line))
    for key in ("affectedLocations", "evidence"):
        items = finding.get(key) if isinstance(finding.get(key), list) else []
        for item in items:
            if not isinstance(item, dict):
                continue
            file_path = safe_repo_relative_file(item.get("file"))
            line = positive_int(item.get("startLine") or item.get("line"))
            if file_path and line:
                locations.append((file_path, line))
    return locations


def finding_location_exists_in_checkout(checkout_dir: Path, finding: dict, fallback: dict | None = None) -> bool:
    locations = finding_line_locations(finding)
    if not locations and isinstance(fallback, dict):
        current_file = finding_primary_file(finding)
        fallback_locations = finding_line_locations(fallback)
        locations = [
            (file_path, line)
            for file_path, line in fallback_locations
            if not current_file or file_path == current_file
        ]
    if locations:
        for file_path, line in locations:
            path = checkout_dir / file_path
            if not path.is_file():
                return False
            try:
                with path.open("r", encoding="utf-8", errors="ignore") as handle:
                    line_count = sum(1 for _ in handle)
            except OSError:
                return False
            if line > line_count:
                return False
        return True
    file_path = finding_primary_file(finding)
    if not file_path and isinstance(fallback, dict):
        file_path = finding_primary_file(fallback)
    if not file_path:
        return True
    path = checkout_dir / file_path
    return path.is_file()


def normalized_head_sha(value: object) -> str:
    text = str(value or "").strip().lower()
    if text and text != "pending" and re.fullmatch(r"[0-9a-f]{7,64}", text):
        return text
    return ""


def git_diff_name_only(checkout_dir: Path, previous: str, current: str) -> set[str] | None:
    try:
        completed = subprocess.run(
            ["git", "-C", str(checkout_dir), "diff", "--name-only", f"{previous}..{current}"],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=env_int("PULLWISE_GIT_TIMEOUT_SECONDS", 600),
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    return {safe_repo_relative_file(line.strip()) for line in completed.stdout.splitlines() if safe_repo_relative_file(line.strip())}


def parse_git_diff_changed_line_ranges(diff_text: str) -> dict[str, list[tuple[int, int]]]:
    ranges: dict[str, list[tuple[int, int]]] = {}
    current_file = ""
    for line in diff_text.splitlines():
        if line.startswith("+++ "):
            file_path = line[4:].strip()
            if file_path == "/dev/null":
                current_file = ""
                continue
            if file_path.startswith("b/"):
                file_path = file_path[2:]
            current_file = safe_repo_relative_file(file_path)
            continue
        if not current_file or not line.startswith("@@"):
            continue
        match = re.search(r"\+(\d+)(?:,(\d+))?", line)
        if not match:
            continue
        start = positive_int(match.group(1))
        count = positive_int(match.group(2) or 1)
        if not start or not count:
            continue
        ranges.setdefault(current_file, []).append((start, start + count - 1))
    return ranges


def git_diff_changed_line_ranges(checkout_dir: Path, previous: str, current: str) -> dict[str, list[tuple[int, int]]] | None:
    try:
        completed = subprocess.run(
            ["git", "-C", str(checkout_dir), "diff", "--unified=0", f"{previous}..{current}"],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=env_int("PULLWISE_GIT_TIMEOUT_SECONDS", 600),
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    return parse_git_diff_changed_line_ranges(completed.stdout)


def fetch_git_head(checkout_dir: Path, head_sha: str, job: dict | None = None) -> bool:
    head = normalized_head_sha(head_sha)
    if not head:
        return False
    git_env = None
    if isinstance(job, dict):
        try:
            git_env = git_auth_env(job.get("clone_token"), job.get("clone_url"), job.get("repo"))
        except RuntimeError:
            return False
    try:
        subprocess.run(
            ["git", "-C", str(checkout_dir), "fetch", "--depth", "1", "origin", head],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=env_int("PULLWISE_GIT_TIMEOUT_SECONDS", 600),
            env=git_env,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return False
    return True


def changed_files_between_heads(
    checkout_dir: Path,
    previous_head_sha: str,
    current_head_sha: str,
    *,
    job: dict | None = None,
) -> set[str] | None:
    previous = normalized_head_sha(previous_head_sha)
    current = normalized_head_sha(current_head_sha)
    if not previous or not current:
        return None
    if previous == current:
        return set()
    changed_files = git_diff_name_only(checkout_dir, previous, current)
    if changed_files is not None:
        return changed_files
    if fetch_git_head(checkout_dir, previous, job):
        return git_diff_name_only(checkout_dir, previous, current)
    return None


def changed_line_ranges_between_heads(
    checkout_dir: Path,
    previous_head_sha: str,
    current_head_sha: str,
    *,
    job: dict | None = None,
) -> dict[str, list[tuple[int, int]]] | None:
    previous = normalized_head_sha(previous_head_sha)
    current = normalized_head_sha(current_head_sha)
    if not previous or not current:
        return None
    if previous == current:
        return {}
    changed_ranges = git_diff_changed_line_ranges(checkout_dir, previous, current)
    if changed_ranges is not None:
        return changed_ranges
    if fetch_git_head(checkout_dir, previous, job):
        return git_diff_changed_line_ranges(checkout_dir, previous, current)
    return None


def finding_line_within_changed_ranges(finding: dict, changed_line_ranges: dict[str, list[tuple[int, int]]] | None) -> bool:
    file_path, line = finding_primary_line_location(finding)
    if changed_line_ranges is None:
        return finding_has_verification_proof(finding) or not line
    if not file_path:
        return True
    if not line:
        return False
    ranges = changed_line_ranges.get(file_path)
    if ranges and any(start <= line <= end for start, end in ranges):
        return True
    return bool(ranges and finding_has_verification_proof(finding))


def source_stat_count(stats: dict, key: str) -> int:
    return positive_int(stats.get(key)) if isinstance(stats, dict) else 0


def wilson_lower_bound(successes: int, total: int, *, z: float = 1.0) -> float:
    if total <= 0:
        return 1.0
    p = successes / total
    denominator = 1 + (z * z / total)
    centre = p + (z * z / (2 * total))
    margin = z * ((p * (1 - p) + (z * z / (4 * total))) / total) ** 0.5
    return max(0.0, min(1.0, (centre - margin) / denominator))


def statistically_calibrated_confidence(finding: dict, source_stats: dict) -> float:
    try:
        base_confidence = float(finding.get("confidence") or 0.0)
    except (OverflowError, TypeError, ValueError):
        base_confidence = 0.0
    base_confidence = max(0.0, min(1.0, base_confidence))
    confirmed = source_stat_count(source_stats, "confirmed")
    resolved = source_stat_count(source_stats, "resolved")
    rejected = source_stat_count(source_stats, "rejected")
    if finding_has_verification_proof(finding):
        return base_confidence
    if not confirmed and resolved and not rejected:
        reliability = wilson_lower_bound(1, resolved + 2)
        return base_confidence * reliability
    positive_feedback = confirmed
    if not positive_feedback and not rejected:
        return base_confidence
    if rejected < positive_feedback:
        total = positive_feedback + rejected
        if total < 8 or rejected <= 1:
            return base_confidence
    reliability = wilson_lower_bound(positive_feedback + 1, positive_feedback + rejected + 2)
    return base_confidence * reliability


def convergence_min_confidence(finding: dict) -> float:
    if finding_has_verification_proof(finding):
        return CONVERGENCE_MIN_VERIFIED_CONFIDENCE
    return CONVERGENCE_MIN_UNVERIFIED_CONFIDENCE


def finding_has_verification_proof(finding: dict) -> bool:
    status = str(finding.get("verificationStatus") or "").strip().lower()
    return status in {"verified", "static_proof"}


def merge_source_stats(context: dict) -> dict[str, dict[str, int]]:
    raw_stats = context.get("source_stats") if isinstance(context.get("source_stats"), dict) else {}
    stats: dict[str, dict[str, int]] = {}
    for raw_source, raw_counts in raw_stats.items():
        source = normalized_source_key(raw_source)
        if not source or not isinstance(raw_counts, dict):
            continue
        stats[source] = {
            "reported": source_stat_count(raw_counts, "reported"),
            "confirmed": source_stat_count(raw_counts, "confirmed"),
            "resolved": source_stat_count(raw_counts, "resolved"),
            "rejected": source_stat_count(raw_counts, "rejected"),
        }
    return stats


def bump_source_stat(stats: dict[str, dict[str, int]], source: str, key: str) -> None:
    bucket = stats.setdefault(source or "reviewer", {"reported": 0, "confirmed": 0, "resolved": 0, "rejected": 0})
    bucket[key] = positive_int(bucket.get(key)) + 1


def convergence_record_for_finding(finding: dict, fingerprint: str) -> dict:
    record = {
        "fingerprint": fingerprint,
        "issue_id": str(finding.get("id") or "").strip()[:120],
        "title": str(finding.get("title") or "").strip()[:180],
        "file": finding_primary_file(finding),
        "line": positive_int(finding.get("line")),
        "confidence": max(0.0, min(1.0, float(finding.get("confidence") or 0.0))),
        "source": finding_source(finding),
        "status": "open",
    }
    return {key: value for key, value in record.items() if value not in ("", 0, [], {})}


def finding_issue_id(finding: dict) -> str:
    return str(finding.get("id") or finding.get("issue_id") or finding.get("issueId") or "").strip()


def convergence_scope_key(job: dict) -> str:
    repo = normalized_fingerprint_text(job.get("repo"))
    branch = normalized_fingerprint_text(job.get("branch") or "main")
    return f"repo:{repo}|branch:{branch}"


def convergence_context_for_job(job: dict) -> dict:
    context = job.get("convergence_context") if isinstance(job.get("convergence_context"), dict) else {}
    if not context:
        return {}
    scope_key = normalized_fingerprint_text(context.get("scope_key") or context.get("scopeKey"))
    expected_scope_key = normalized_fingerprint_text(convergence_scope_key(job))
    if scope_key and scope_key != expected_scope_key:
        return {}
    protocol = str(context.get("protocol") or "").strip()
    if protocol and protocol != CONVERGENCE_PROTOCOL_VERSION:
        return {}
    return context


def apply_convergence_gate(
    job: dict,
    checkout_dir: Path,
    findings: list[dict],
    decision_records: list[dict] | None = None,
) -> tuple[list[dict], dict[str, int], list[dict], dict]:
    context = convergence_context_for_job(job)
    previous_open = [
        item
        for item in (context.get("open_findings") if isinstance(context.get("open_findings"), list) else [])
        if isinstance(item, dict) and str(item.get("fingerprint") or "").strip()
    ]
    previous_by_fingerprint = {str(item.get("fingerprint")).strip(): item for item in previous_open}
    previous_fingerprint_by_issue_id = {
        str(item.get("issue_id") or item.get("issueId")).strip(): str(item.get("fingerprint")).strip()
        for item in previous_open
        if str(item.get("issue_id") or item.get("issueId")).strip()
    }
    previous_head_sha = normalized_head_sha(context.get("previous_head_sha"))
    current_head_sha = normalized_head_sha(job.get("resolved_commit") or job.get("commit"))
    has_prior_run = bool(previous_head_sha or previous_open or context.get("source_stats"))
    source_stats = merge_source_stats(context)
    known_sources = set(source_stats)
    changed_files: set[str] | None = None
    changed_files_loaded = False
    changed_line_ranges: dict[str, list[tuple[int, int]]] | None = None
    changed_line_ranges_loaded = False
    reportable: list[dict] = []
    open_fingerprints = set()
    seen_current_fingerprints = set()
    rejected_reasons: dict[str, int] = {}
    rejected_samples: list[dict] = []

    def record_decision(finding: dict, decision: str, reason: str, fingerprint: str, source_counts: dict | None = None) -> None:
        if decision_records is None:
            return
        decision_records.append(
            {
                "stage": "convergence",
                "decision": decision,
                "reason": reason,
                "finding": finding,
                "fingerprint": fingerprint,
                "source_stats": dict(source_counts or {}),
            }
        )

    def reject(finding: dict, reason: str, fingerprint: str = "", source_counts: dict | None = None) -> None:
        rejected_reasons[reason] = rejected_reasons.get(reason, 0) + 1
        bump_source_stat(source_stats, finding_source(finding), "rejected")
        if len(rejected_samples) < 5:
            rejected_samples.append(rejected_candidate_sample(finding, reason))
        record_decision(finding, "rejected", reason, fingerprint or finding_fingerprint(finding), source_counts)

    for finding in findings:
        if not isinstance(finding, dict):
            continue
        fingerprint = finding_fingerprint(finding)
        matched_fingerprint = previous_fingerprint_by_issue_id.get(finding_issue_id(finding)) or fingerprint
        source = finding_source(finding)
        source_counts = source_stats.get(source, {})
        if matched_fingerprint in seen_current_fingerprints:
            reject(finding, "duplicate_finding", matched_fingerprint, source_counts)
            continue
        if matched_fingerprint in previous_by_fingerprint:
            if not finding_location_exists_in_checkout(checkout_dir, finding, previous_by_fingerprint[matched_fingerprint]):
                reject(finding, "stale_previous_location", matched_fingerprint, source_counts)
                continue
            reportable.append(finding)
            open_fingerprints.add(matched_fingerprint)
            seen_current_fingerprints.add(matched_fingerprint)
            bump_source_stat(source_stats, source, "reported")
            if str(finding.get("verificationStatus") or "").lower() in {"verified", "static_proof"}:
                bump_source_stat(source_stats, source, "confirmed")
            record_decision(finding, "reported", "matched_previous_open_finding", matched_fingerprint, source_counts)
            continue
        if has_prior_run and known_sources and source not in known_sources and not finding_has_verification_proof(finding):
            reject(finding, "unknown_source_after_prior_run", matched_fingerprint, source_counts)
            continue
        if statistically_calibrated_confidence(finding, source_counts) < convergence_min_confidence(finding):
            reject(finding, "low_statistical_confidence", matched_fingerprint, source_counts)
            continue
        if has_prior_run:
            if not changed_files_loaded:
                changed_files = changed_files_between_heads(checkout_dir, previous_head_sha, current_head_sha, job=job)
                changed_files_loaded = True
            finding_files = finding_delta_files(finding)
            if changed_files is None or not finding_files or not (finding_files & changed_files):
                reject(finding, "not_introduced_by_current_delta", matched_fingerprint, source_counts)
                continue
            if not changed_line_ranges_loaded:
                changed_line_ranges = changed_line_ranges_between_heads(checkout_dir, previous_head_sha, current_head_sha, job=job)
                changed_line_ranges_loaded = True
            if not finding_line_within_changed_ranges(finding, changed_line_ranges):
                reject(finding, "not_introduced_by_current_delta", matched_fingerprint, source_counts)
                continue
            if not finding_location_exists_in_checkout(checkout_dir, finding):
                reject(finding, "invalid_candidate_location", matched_fingerprint, source_counts)
                continue
        reportable.append(finding)
        open_fingerprints.add(matched_fingerprint)
        seen_current_fingerprints.add(matched_fingerprint)
        bump_source_stat(source_stats, source, "reported")
        if str(finding.get("verificationStatus") or "").lower() in {"verified", "static_proof"}:
            bump_source_stat(source_stats, source, "confirmed")
        record_decision(finding, "reported", "passed_convergence_gate", matched_fingerprint, source_counts)

    resolved_fingerprints = []
    for fingerprint, record in previous_by_fingerprint.items():
        if fingerprint not in open_fingerprints:
            resolved_fingerprints.append(fingerprint)
            source = normalized_source_key(record.get("source")) or "reviewer"
            bump_source_stat(source_stats, source, "resolved")

    state = {
        "protocol": CONVERGENCE_PROTOCOL_VERSION,
        "scope_key": convergence_scope_key(job),
        "head_sha": current_head_sha,
        "open_findings": [
            convergence_record_for_finding(
                finding,
                previous_fingerprint_by_issue_id.get(finding_issue_id(finding)) or finding_fingerprint(finding),
            )
            for finding in reportable
        ],
        "resolved_fingerprints": sorted(resolved_fingerprints),
        "source_stats": source_stats,
    }
    return reportable, rejected_reasons, rejected_samples, state


