from __future__ import annotations


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


def local_judge(candidate: dict, repro: dict) -> dict:
    result = repro.get("result") if isinstance(repro.get("result"), dict) else {}
    violations = repro.get("filesystem_violations") if isinstance(repro.get("filesystem_violations"), list) else []
    command = str(result.get("command") or "")
    log_path = str(result.get("log_path") or result.get("logPath") or "")
    observable = str(result.get("observable") or result.get("observed_output") or result.get("observedOutput") or "")
    command_violation = unsafe_repro_command(command)
    if command_violation:
        violations = [*violations, command_violation]
    status = "confirmed" if command and log_path and observable and not violations and result.get("reproduced") is True else "rejected"
    reason = "reproduction evidence satisfies the graph-verified gate" if status == "confirmed" else "missing or unsafe reproduction evidence"
    if violations:
        reason = "; ".join(violations)
    return {
        "candidate_id": str(candidate.get("issue_id") or repro.get("candidate_id") or ""),
        "status": status,
        "level": "L2" if status == "confirmed" else "L0",
        "safe_to_show_user": status == "confirmed",
        "reason": reason,
        "evidence_summary": {
            "command": command,
            "log_path": log_path,
            "observable": observable[:1000],
        },
        "limitations": result.get("limitations") if isinstance(result.get("limitations"), list) else [],
    }


def unsafe_repro_command(command: str) -> str:
    normalized = f" {command.strip()} "
    lowered = normalized.lower()
    for marker in DENIED_COMMAND_MARKERS:
        if marker.lower() in lowered:
            return f"unsupported reproduction command marker: {marker.strip()}"
    return ""
