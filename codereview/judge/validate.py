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


def local_judge(candidate: dict, repro: dict) -> dict:
    result = repro.get("result") if isinstance(repro.get("result"), dict) else {}
    violations = repro.get("filesystem_violations") if isinstance(repro.get("filesystem_violations"), list) else []
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
    level = str(result.get("level") or ("L2" if reproduced else "L0"))
    graph_path_exercised = result.get("graph_path_exercised")
    if graph_path_exercised is not True:
        violations = [*violations, "graph path was not exercised"]
    exit_code = command_info.get("exit_code") if "exit_code" in command_info else result.get("exit_code")
    if command and exit_code is None:
        violations = [*violations, "command exit_code missing"]
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
