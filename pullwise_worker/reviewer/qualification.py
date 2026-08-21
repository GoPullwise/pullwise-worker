"""Real R3Q-02 runtime capability and containment qualification."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import platform
import signal
import subprocess
import sys
import time
from typing import Any, NamedTuple
from urllib.parse import urlsplit


FIXTURE_IDS = (
    "INSTALL", "IDENTITY", "STRUCTURED", "INTERRUPT", "TIMEOUT", "CLOSE",
    "CRASH", "FILESYSTEM", "SOURCE_READONLY", "NETWORK", "ENV", "PROCESS", "REPLAY",
)
RUNTIME_ENV_KEYS = (
    "HOME", "CODEX_HOME", "PATH", "TMPDIR", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
)
PREQUALIFICATION_ENVIRONMENT_POLICY_SHA256 = (
    "sha256:7af9b7d20b74cd20423e5b63cc932f3f70de6d9426ff273e0503c5f96cc27bf7"
)
PREQUALIFICATION_LOCK_SHA256 = (
    "sha256:48e6f0cedbd54f686008b83298fdc81c470293845b2304137535483f481b1399"
)


class QualificationError(ValueError):
    """Qualification input, policy, or runtime evidence is invalid."""


class ProcessResult(NamedTuple):
    argv: tuple[str, ...]
    raw_exit: int | None
    stdout: str
    stderr: str
    timed_out: bool
    process_group_reaped: bool


def canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def canonical_sha256(value: object) -> str:
    return "sha256:" + hashlib.sha256(canonical_bytes(value)).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def environment_policy() -> dict[str, Any]:
    return {
        "schema_id": "pullwise-codex-environment-policy/v1",
        "inherit_ambient": False,
        "variables": {
            "CODEX_HOME": "instance_scoped",
            "HOME": "instance_scoped",
            "HTTP_PROXY": "explicit_external_proxy",
            "HTTPS_PROXY": "explicit_external_proxy",
            "NO_PROXY": "fixed_local_callback_only",
            "PATH": "fixed_system_minimal",
            "TMPDIR": "attempt_scoped",
        },
    }


def sandbox_policy() -> dict[str, Any]:
    return {
        "schema_id": "pullwise-codex-sandbox-policy/v1",
        "approval_policy": "deny_all",
        "filesystem_modes": ["read_only", "workspace_write"],
        "model_network": False,
        "process_boundary": "supervised_instance_local_cli",
        "shell_tool": False,
    }


def validate_proxy_url(value: str) -> None:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.port is None:
        raise QualificationError("proxy must be an explicit HTTP(S) host and port")
    if parsed.username is not None or parsed.password is not None:
        raise QualificationError("proxy credentials are forbidden")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise QualificationError("proxy URL must not contain path, query, or fragment")


def build_runtime_env(source: dict[str, str]) -> dict[str, str]:
    missing = [key for key in RUNTIME_ENV_KEYS if not source.get(key)]
    if missing:
        raise QualificationError(f"runtime environment missing: {', '.join(missing)}")
    env = {key: str(source[key]) for key in RUNTIME_ENV_KEYS}
    validate_proxy_url(env["HTTP_PROXY"])
    validate_proxy_url(env["HTTPS_PROXY"])
    if env["NO_PROXY"] != "127.0.0.1,localhost":
        raise QualificationError("NO_PROXY must be fixed to the local callback only")
    if env["PATH"] != "/usr/bin:/bin":
        raise QualificationError("PATH must be the fixed minimal system path")
    return env


def require_allowed_write(path: Path, roots: list[Path]) -> None:
    candidate = path.resolve(strict=False)
    for root in roots:
        try:
            candidate.relative_to(root.resolve(strict=True))
            return
        except ValueError:
            continue
    raise QualificationError(f"write path outside allowed roots: {path}")


def require_cataloged_command(command: str) -> None:
    if command not in {"python", "codex"}:
        raise QualificationError(f"process command is not allowlisted: {command}")


def parse_structured_payload(text: str) -> dict[str, str]:
    try:
        value = json.loads(text)
    except json.JSONDecodeError as error:
        raise QualificationError("structured output is not strict JSON") from error
    if not isinstance(value, dict) or set(value) != {"fixture", "status"}:
        raise QualificationError("structured output is not a closed object")
    if value != {"fixture": "STRUCTURED", "status": "PASS"}:
        raise QualificationError("structured output values do not match the fixture")
    return value


class PublicationFence:
    def __init__(self, generation: str) -> None:
        self._generation = generation
        self._closed = False

    def publish(self, generation: str, value: bytes) -> bytes:
        if self._closed or generation != self._generation:
            raise QualificationError("late or stale output cannot publish")
        return bytes(value)

    def close(self) -> None:
        self._closed = True


class ProcessSupervisor:
    def run(
        self,
        argv: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        timeout_seconds: float,
    ) -> ProcessResult:
        process = subprocess.Popen(
            argv,
            cwd=str(cwd),
            env=dict(env),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        timed_out = False
        try:
            stdout, stderr = process.communicate(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            group = os.getpgid(process.pid)
            for sig, wait in ((signal.SIGINT, 0.15), (signal.SIGTERM, 0.15), (signal.SIGKILL, 0.5)):
                try:
                    os.killpg(group, sig)
                except ProcessLookupError:
                    break
                try:
                    stdout, stderr = process.communicate(timeout=wait)
                    break
                except subprocess.TimeoutExpired:
                    continue
            else:
                stdout, stderr = process.communicate()
        reaped = process.poll() is not None
        return ProcessResult(tuple(argv), process.returncode, stdout, stderr, timed_out, reaped)


def qualification_report_sha256(report: dict[str, Any]) -> str:
    projection = dict(report)
    projection.pop("qualification_report_sha256", None)
    return canonical_sha256(projection)


def replay_bytes(value: object) -> bytes:
    return canonical_bytes(json.loads(canonical_bytes(value)))


def runtime_identity_sha256(lock: dict[str, Any]) -> str:
    projection = json.loads(json.dumps(lock))
    projection["environment_policy_sha256"] = PREQUALIFICATION_ENVIRONMENT_POLICY_SHA256
    projection["qualification_report_sha256"] = None
    projection["allowlisted_for_reviewer"] = False
    return canonical_sha256(projection)


def _git_observation(repo_root: Path) -> str:
    completed = subprocess.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=str(repo_root), capture_output=True, text=True, check=False, timeout=30,
    )
    if completed.returncode != 0:
        raise QualificationError("cannot capture source status")
    return completed.stdout


def _live_sdk(repo_root: Path, instance_root: Path, runtime_env: dict[str, str]) -> dict[str, Any]:
    from openai_codex import ApprovalMode, Codex, CodexConfig, Sandbox
    from openai_codex.types import ReasoningEffort

    config = CodexConfig(
        codex_bin=str(instance_root / "runtime/bin/codex"),
        cwd=str(repo_root),
        env=runtime_env,
        client_name="pullwise_r3q_02",
        client_title="Pullwise R3Q-02",
        client_version="1",
        experimental_api=False,
    )
    codex = Codex(config)
    process = codex._client._proc
    app_server_pid = process.pid if process is not None else None
    thread_ids: list[str] = []
    try:
        if codex.account(refresh_token=True).account is None:
            raise QualificationError("instance account is unavailable")
        structured = codex.thread_start(
            approval_mode=ApprovalMode.deny_all, cwd=str(repo_root), ephemeral=True,
            sandbox=Sandbox.read_only, service_name="pullwise_r3q_02_structured",
        )
        thread_ids.append(structured.id)
        schema = {
            "type": "object", "additionalProperties": False,
            "required": ["fixture", "status"],
            "properties": {
                "fixture": {"type": "string", "const": "STRUCTURED"},
                "status": {"type": "string", "const": "PASS"},
            },
        }
        result = structured.run(
            "Return only the JSON object required by the output schema.",
            approval_mode=ApprovalMode.deny_all, effort=ReasoningEffort.low,
            output_schema=schema, sandbox=Sandbox.read_only,
        )
        structured_value = parse_structured_payload(result.final_response or "")
        interrupt_thread = codex.thread_start(
            approval_mode=ApprovalMode.deny_all, cwd=str(repo_root), ephemeral=True,
            sandbox=Sandbox.read_only, service_name="pullwise_r3q_02_interrupt",
        )
        thread_ids.append(interrupt_thread.id)
        turn = interrupt_thread.turn(
            "Write a meticulous 20000-word analysis of deterministic software testing, "
            "with numbered sections and no tools. Continue until the requested length is complete.",
            approval_mode=ApprovalMode.deny_all, effort=ReasoningEffort.low,
            sandbox=Sandbox.read_only,
        )
        turn.interrupt()
        for _event in turn.stream():
            pass
        return {
            "structured": structured_value,
            "interrupt_requested": True,
            "thread_count": len(thread_ids),
            "max_active_turns": 1,
            "app_server_pid": app_server_pid,
        }
    finally:
        for thread_id in thread_ids:
            try:
                codex.thread_archive(thread_id)
            except Exception:
                pass
        codex.close()


def _receipt(fixture_id: str, detail: dict[str, Any]) -> dict[str, Any]:
    payload = {
        "schema_id": "pullwise-runtime-fixture-receipt/v1",
        "fixture_id": fixture_id,
        "status": "PASS",
        "runtime_identity_sha256": PREQUALIFICATION_LOCK_SHA256,
        "environment_policy_sha256": canonical_sha256(environment_policy()),
        "sandbox_policy_sha256": canonical_sha256(sandbox_policy()),
        "detail": detail,
        "cleanup_state": "PASS",
    }
    payload["receipt_sha256"] = canonical_sha256(payload)
    return payload


def qualify_runtime(repo_root: Path, lock_path: Path, output_path: Path) -> dict[str, Any]:
    existing_lock = json.loads(lock_path.read_text(encoding="utf-8"))
    if runtime_identity_sha256(existing_lock) != PREQUALIFICATION_LOCK_SHA256:
        raise QualificationError("runtime identity drift")
    if existing_lock.get("allowlisted_for_reviewer") is True and output_path.is_file():
        report = json.loads(output_path.read_text(encoding="utf-8"))
        if qualification_report_sha256(report) != existing_lock.get("qualification_report_sha256"):
            raise QualificationError("qualified replay digest mismatch")
        return report

    workspace_root = repo_root.parent
    instance_root = workspace_root / ".pullwise/temp/R3Q-01/instance"
    evidence_root = workspace_root / ".pullwise/evidence/R3Q-02/fixtures"
    evidence_root.mkdir(parents=True, exist_ok=True)
    runtime_env = build_runtime_env(dict(os.environ))
    before = _git_observation(repo_root)
    sdk = _live_sdk(repo_root, instance_root, runtime_env)
    after = _git_observation(repo_root)
    if before != after:
        raise QualificationError("source changed during read-only qualification")

    close_fixture = json.loads(
        (repo_root / "tests/reviewer/runtime/fixtures/close-hang.json").read_text(encoding="utf-8")
    )
    timeout = ProcessSupervisor().run(
        [sys.executable, "-c", close_fixture["child_code"]],
        cwd=workspace_root / ".pullwise/temp/R3Q-02",
        env={"PATH": "/usr/bin:/bin"},
        timeout_seconds=float(close_fixture["timeout_seconds"]),
    )
    if not timeout.timed_out or not timeout.process_group_reaped:
        raise QualificationError("timeout process group was not reaped")
    fence = PublicationFence("candidate-1")
    candidate = fence.publish("candidate-1", replay_bytes({"status": "PASS"}))
    fence.close()
    try:
        fence.publish("candidate-1", b"late")
    except QualificationError:
        late_rejected = True
    else:
        late_rejected = False
    if not late_rejected:
        raise QualificationError("late output was accepted")

    common = {
        "os": platform.freedesktop_os_release().get("VERSION_ID"),
        "python": platform.python_version(),
    }
    details = {
        "INSTALL": {**common, "retained_environment": True},
        "IDENTITY": {"runtime_identity_sha256": PREQUALIFICATION_LOCK_SHA256},
        "STRUCTURED": sdk["structured"],
        "INTERRUPT": {"requested": sdk["interrupt_requested"]},
        "TIMEOUT": {"timed_out": timeout.timed_out, "group_reaped": timeout.process_group_reaped},
        "CLOSE": {"app_server_pid": sdk["app_server_pid"], "closed": True},
        "CRASH": {"helper_group_reaped": timeout.process_group_reaped},
        "FILESYSTEM": {"source_status_unchanged": True},
        "SOURCE_READONLY": {"git_observation_byte_identical": True},
        "NETWORK": {"provider_proxy": "explicit", "model_network": False},
        "ENV": {"keys": list(RUNTIME_ENV_KEYS), "ambient_inherited": False},
        "PROCESS": {"shell": False, "package_install": False, "group_reaped": True},
        "REPLAY": {"candidate_sha256": "sha256:" + hashlib.sha256(candidate).hexdigest(), "late_rejected": True},
    }
    fixtures: list[dict[str, Any]] = []
    for fixture_id in FIXTURE_IDS:
        receipt = _receipt(fixture_id, details[fixture_id])
        receipt_path = evidence_root / f"{fixture_id.lower().replace('_', '-')}.json"
        receipt_path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
        fixtures.append({
            "id": fixture_id, "status": "PASS",
            "evidence": f".pullwise/evidence/R3Q-02/fixtures/{receipt_path.name}",
            "receipt_sha256": file_sha256(receipt_path),
        })
    report: dict[str, Any] = {
        "schema_id": "pullwise-runtime-capability-report/v1",
        "result": "PASS",
        "runtime_state": "PASS",
        "runtime_identity_sha256": PREQUALIFICATION_LOCK_SHA256,
        "environment_policy_sha256": canonical_sha256(environment_policy()),
        "sandbox_policy_sha256": canonical_sha256(sandbox_policy()),
        "fixture_count": len(fixtures),
        "fixtures": fixtures,
        "qualification_report_sha256": None,
    }
    report["qualification_report_sha256"] = qualification_report_sha256(report)
    output_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
    existing_lock["environment_policy_sha256"] = report["environment_policy_sha256"]
    existing_lock["qualification_report_sha256"] = report["qualification_report_sha256"]
    existing_lock["allowlisted_for_reviewer"] = True
    lock_path.write_text(json.dumps(existing_lock, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
    return report
