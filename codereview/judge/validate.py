from __future__ import annotations

from pathlib import Path

from ..utils.paths import is_within


DENIED_COMMAND_MARKERS = {
    "curl ",
    "wget ",
    "Invoke-WebRequest",
    "irm ",
    "iwr ",
    "ssh ",
    "scp ",
    "rsync ",
    "ftp ",
    "nc ",
    "netcat ",
    "rm -rf /",
    "Remove-Item -Recurse C:",
    "format ",
    "mkfs",
}
REPRO_REQUIRED_FIELDS = {
    "candidate_id",
    "status",
    "level",
    "summary",
    "commands_run",
    "files_written",
    "proof",
    "graph_path_exercised",
    "why_valid",
    "why_not_reproduced",
    "safety_notes",
}
REPRO_OPTIONAL_FIELDS = {"touched_symbols", "environment"}
REPRO_STATUSES = {"reproduced", "not_reproduced", "blocked", "unsafe", "ambiguous", "harness_error", "timeout"}
REPRO_LEVELS = {"L0", "L1", "L2", "L3"}
PROOF_TYPES = {"failing_test", "runtime_output", "assertion", "red_green", "static_check", "other", "none"}
JUDGE_REQUIRED_FIELDS = {"candidate_id", "status", "level", "safe_to_show_user", "reason", "evidence_summary", "limitations"}
JUDGE_STATUSES = {"confirmed", "rejected", "blocked"}


def local_judge(candidate: dict, repro: dict) -> dict:
    result = repro.get("result") if isinstance(repro.get("result"), dict) else {}
    violations = repro.get("filesystem_violations") if isinstance(repro.get("filesystem_violations"), list) else []
    violations = [*violations, *validate_repro_result(result, expected_candidate_id=str(repro.get("candidate_id") or ""))]
    if not valid_candidate_graph_evidence(candidate):
        violations = [*violations, "candidate graph_evidence missing or invalid"]
    command_info = primary_command(result)
    command = str(command_info.get("cmd") or "")
    log_path = str(command_info.get("log_path") or "")
    observable = proof_observable(result)
    worker_text = str(repro.get("worker") or "")
    worker = Path(worker_text) if worker_text else None
    command_violation = unsafe_repro_command(command)
    if command_violation:
        violations = [*violations, command_violation]
    log_violation = validate_log(worker, log_path, observable)
    if log_violation:
        violations = [*violations, log_violation]
    status_value = str(result.get("status") or "").lower()
    reproduced = status_value == "reproduced"
    level = str(result.get("level") or "")
    graph_path_exercised = result.get("graph_path_exercised")
    if graph_path_exercised is not True:
        violations = [*violations, "graph path was not exercised"]
    exit_code = command_info.get("exit_code") if "exit_code" in command_info else result.get("exit_code")
    if command and exit_code is None:
        violations = [*violations, "command exit_code missing"]
    environment = result.get("environment") if isinstance(result.get("environment"), dict) else {}
    if environment.get("network_used") is True:
        violations = [*violations, "reproduction used network"]
    status = "confirmed" if command and log_path and observable and not violations and reproduced and level in {"L2", "L3"} else "rejected"
    reason = "reproduction evidence satisfies the graph-verified gate" if status == "confirmed" else "missing or unsafe reproduction evidence"
    if violations:
        reason = "; ".join(violations)
    return {
        "candidate_id": str(candidate.get("issue_id") or repro.get("candidate_id") or ""),
        "status": status,
        "level": level if status == "confirmed" else "L0",
        "safe_to_show_user": status == "confirmed",
        "reason": reason,
        "evidence_summary": {
            "command": command,
            "log_path": log_path,
            "observable": observable[:1000],
        },
        "limitations": result.get("limitations") if isinstance(result.get("limitations"), list) else [],
    }


def validate_repro_result(result: object, *, expected_candidate_id: str = "") -> list[str]:
    if not isinstance(result, dict):
        return ["repro result must be an object"]
    violations: list[str] = []
    missing = sorted(field for field in REPRO_REQUIRED_FIELDS if field not in result)
    if missing:
        violations.append(f"repro result missing required fields: {', '.join(missing)}")
    unexpected = sorted(set(result) - REPRO_REQUIRED_FIELDS - REPRO_OPTIONAL_FIELDS)
    if unexpected:
        violations.append(f"repro result has unexpected fields: {', '.join(unexpected)}")
    candidate_id = str(result.get("candidate_id") or "").strip()
    if expected_candidate_id and candidate_id != expected_candidate_id:
        violations.append("repro result candidate_id does not match selected candidate")
    status = str(result.get("status") or "")
    if status and status not in REPRO_STATUSES:
        violations.append("repro result status is not allowed")
    level = str(result.get("level") or "")
    if level and level not in REPRO_LEVELS:
        violations.append("repro result level is not allowed")

    commands = result.get("commands_run")
    if not isinstance(commands, list):
        violations.append("commands_run must be a list")
    else:
        if status == "reproduced" and not commands:
            violations.append("reproduced result must include at least one command")
        for index, command in enumerate(commands):
            violations.extend(_validate_repro_command(command, index))

    files_written = result.get("files_written")
    if not isinstance(files_written, list) or any(not isinstance(item, str) for item in files_written):
        violations.append("files_written must be a list of strings")

    proof = result.get("proof")
    proof_violations = _validate_repro_proof(proof)
    violations.extend(proof_violations)
    if status == "reproduced" and isinstance(proof, dict):
        if not str(proof.get("expected") or "").strip() or not str(proof.get("actual") or "").strip():
            violations.append("reproduced proof must include expected and actual")
        if not str(proof.get("log_excerpt") or "").strip():
            violations.append("reproduced proof must include log_excerpt")

    if "graph_path_exercised" in result and not isinstance(result.get("graph_path_exercised"), bool):
        violations.append("graph_path_exercised must be a boolean")

    environment = result.get("environment")
    if environment is not None:
        if not isinstance(environment, dict):
            violations.append("environment must be an object")
        elif environment.get("network_used") is True:
            violations.append("environment.network_used must not be true")

    touched_symbols = result.get("touched_symbols")
    if touched_symbols is not None and (not isinstance(touched_symbols, list) or any(not isinstance(item, str) for item in touched_symbols)):
        violations.append("touched_symbols must be a list of strings")

    for field in ("candidate_id", "status", "level", "summary", "why_valid", "why_not_reproduced", "safety_notes"):
        if field in result and not isinstance(result.get(field), str):
            violations.append(f"{field} must be a string")
    return violations


def validate_judge_result(result: object, *, expected_candidate_id: str = "") -> list[str]:
    if not isinstance(result, dict):
        return ["judge result must be an object"]
    violations: list[str] = []
    missing = sorted(field for field in JUDGE_REQUIRED_FIELDS if field not in result)
    if missing:
        violations.append(f"judge result missing required fields: {', '.join(missing)}")
    unexpected = sorted(set(result) - JUDGE_REQUIRED_FIELDS)
    if unexpected:
        violations.append(f"judge result has unexpected fields: {', '.join(unexpected)}")
    candidate_id = str(result.get("candidate_id") or "").strip()
    if expected_candidate_id and candidate_id != expected_candidate_id:
        violations.append("judge result candidate_id does not match local judge")
    status = str(result.get("status") or "")
    if status and status not in JUDGE_STATUSES:
        violations.append("judge result status is not allowed")
    level = str(result.get("level") or "")
    if level and level not in REPRO_LEVELS:
        violations.append("judge result level is not allowed")
    if "safe_to_show_user" in result and not isinstance(result.get("safe_to_show_user"), bool):
        violations.append("safe_to_show_user must be a boolean")
    evidence = result.get("evidence_summary")
    if not isinstance(evidence, dict):
        violations.append("evidence_summary must be an object")
    else:
        missing_evidence = sorted(field for field in {"command", "log_path", "observable"} if field not in evidence)
        if missing_evidence:
            violations.append(f"evidence_summary missing required fields: {', '.join(missing_evidence)}")
        unexpected_evidence = sorted(set(evidence) - {"command", "log_path", "observable"})
        if unexpected_evidence:
            violations.append(f"evidence_summary has unexpected fields: {', '.join(unexpected_evidence)}")
        for field in ("command", "log_path", "observable"):
            if field in evidence and not isinstance(evidence.get(field), str):
                violations.append(f"evidence_summary.{field} must be a string")
    if "limitations" in result and (not isinstance(result.get("limitations"), list) or any(not isinstance(item, str) for item in result.get("limitations"))):
        violations.append("limitations must be a list of strings")
    return violations


def valid_candidate_graph_evidence(candidate: dict) -> bool:
    graph = candidate.get("graph_evidence") if isinstance(candidate, dict) else None
    if not isinstance(graph, dict):
        return False
    slice_id = str(graph.get("slice_id") or "").strip()
    codegraph_files = graph.get("codegraph_files")
    path_summary = graph.get("path_summary")
    if not slice_id or not isinstance(codegraph_files, list) or not isinstance(path_summary, list):
        return False
    return any(str(item or "").strip() for item in codegraph_files) and any(
        str(item or "").strip() for item in path_summary
    )


def primary_command(result: dict) -> dict:
    commands = result.get("commands_run")
    if isinstance(commands, list) and commands:
        first = commands[0]
        return first if isinstance(first, dict) else {}
    return {}


def proof_observable(result: dict) -> str:
    proof = result.get("proof") if isinstance(result.get("proof"), dict) else {}
    return str(
        proof.get("log_excerpt")
        or proof.get("actual")
        or ""
    )


def validate_log(worker: Path | None, log_path: str, observable: str) -> str:
    if not log_path:
        return "log path missing"
    if worker is None:
        return ""
    resolved = (worker / log_path).resolve(strict=False)
    if not is_within(resolved, worker):
        return f"log path outside worker directory: {log_path}"
    if not resolved.is_file():
        return f"log path missing: {log_path}"
    if observable:
        text = resolved.read_text(encoding="utf-8", errors="replace")
        if observable[:200] not in text and text[:200] not in observable:
            return "observable output is not supported by log"
    return ""


def unsafe_repro_command(command: str) -> str:
    normalized = f" {command.strip()} "
    lowered = normalized.lower()
    for marker in DENIED_COMMAND_MARKERS:
        if marker.lower() in lowered:
            return f"unsupported reproduction command marker: {marker.strip()}"
    return ""


def _validate_repro_command(command: object, index: int) -> list[str]:
    if not isinstance(command, dict):
        return [f"commands_run[{index}] must be an object"]
    violations: list[str] = []
    missing = sorted(field for field in {"cmd", "cwd", "exit_code", "log_path"} if field not in command)
    if missing:
        violations.append(f"commands_run[{index}] missing required fields: {', '.join(missing)}")
    unexpected = sorted(set(command) - {"cmd", "cwd", "exit_code", "log_path", "duration_ms"})
    if unexpected:
        violations.append(f"commands_run[{index}] has unexpected fields: {', '.join(unexpected)}")
    for field in ("cmd", "cwd", "log_path"):
        if field in command and not isinstance(command.get(field), str):
            violations.append(f"commands_run[{index}].{field} must be a string")
    if "exit_code" in command and not isinstance(command.get("exit_code"), int):
        violations.append(f"commands_run[{index}].exit_code must be an integer")
    if "duration_ms" in command and not isinstance(command.get("duration_ms"), int):
        violations.append(f"commands_run[{index}].duration_ms must be an integer")
    return violations


def _validate_repro_proof(proof: object) -> list[str]:
    if not isinstance(proof, dict):
        return ["proof must be an object"]
    violations: list[str] = []
    missing = sorted(field for field in {"type", "expected", "actual", "log_excerpt"} if field not in proof)
    if missing:
        violations.append(f"proof missing required fields: {', '.join(missing)}")
    unexpected = sorted(set(proof) - {"type", "expected", "actual", "log_excerpt"})
    if unexpected:
        violations.append(f"proof has unexpected fields: {', '.join(unexpected)}")
    proof_type = str(proof.get("type") or "")
    if proof_type and proof_type not in PROOF_TYPES:
        violations.append("proof.type is not allowed")
    for field in ("type", "expected", "actual", "log_excerpt"):
        if field in proof and not isinstance(proof.get(field), str):
            violations.append(f"proof.{field} must be a string")
    return violations
