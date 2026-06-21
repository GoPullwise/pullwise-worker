from __future__ import annotations

import importlib
import inspect
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Optional

codereview_main = importlib.import_module("codereview.main")
from codereview.candidates.normalize import normalize_candidates
from codereview.codex_runner import base_env
from codereview.config import ReviewConfig
from codereview.finder.runner import run_finder
from codereview.finder.tasks import FinderTask, plan_finder_tasks
from codereview.graph.census import run_repository_census
from codereview.inventory.git_inventory import build_git_inventory
from codereview.judge.runner import run_judge
from codereview.judge.precheck import verify_repro_events_and_paths
from codereview.judge.validate import local_judge
from codereview.report.render import collect_confirmed, render_final_report
from codereview.repository.snapshot import analyze_repository_snapshot
from codereview.repository.symbols import map_repository_symbols
from codereview.repro.filesystem_guard import guard_worker_result
from codereview.repro.runner import git_status_porcelain, worker_env
from codereview.repro.worker_dir import create_worker_dir
from codereview.templates import ensure_project_files
from codereview.units.coverage import build_unit_coverage
from codereview.units.context import write_review_units
from codereview.units.planner import build_all_review_units
from codereview.units.risk_tags import choose_finders
from codereview.utils.process import ProcessResult


class _MonkeyPatch:
    def __init__(self) -> None:
        self._undo: list[tuple[object, str, object]] = []

    def setattr(self, target: object, name: str, value: object) -> None:
        original = getattr(target, name)
        self._undo.append((target, name, original))
        setattr(target, name, value)

    def undo(self) -> None:
        while self._undo:
            target, name, original = self._undo.pop()
            setattr(target, name, original)


def load_tests(loader: unittest.TestLoader, tests: unittest.TestSuite, pattern: Optional[str]) -> unittest.TestSuite:
    del loader, tests, pattern
    suite = unittest.TestSuite()
    for name, func in sorted(globals().items()):
        if name.startswith("test_") and callable(func):
            suite.addTest(unittest.FunctionTestCase(lambda func=func: _run_test_function(func), description=name))
    return suite


def _run_test_function(func: object) -> None:
    signature = inspect.signature(func)
    kwargs: dict[str, object] = {}
    patcher: Optional[_MonkeyPatch] = None
    with tempfile.TemporaryDirectory(prefix=f"{getattr(func, '__name__', 'test')}-") as tmp:
        if "tmp_path" in signature.parameters:
            kwargs["tmp_path"] = Path(tmp)
        if "monkeypatch" in signature.parameters:
            patcher = _MonkeyPatch()
            kwargs["monkeypatch"] = patcher
        try:
            func(**kwargs)  # type: ignore[misc]
        finally:
            if patcher is not None:
                patcher.undo()


def test_init_writes_v3_codereview_assets(tmp_path: Path) -> None:
    ensure_project_files(tmp_path)

    config = json.loads((tmp_path / ".codereview" / "config.json").read_text(encoding="utf-8"))
    schema = json.loads((tmp_path / ".codereview" / "schemas" / "finder_result.schema.json").read_text(encoding="utf-8"))

    assert config["scan"]["mode"] == "full-cached"
    assert config["graph"]["schema_version"] == "3"
    assert "impact" not in config
    assert schema["required"] == ["unit_id", "focus", "candidates"]
    graph_props = schema["properties"]["candidates"]["items"]["properties"]["graph_evidence"]["properties"]
    assert set(graph_props) == {"unit_id", "context_files", "path_summary"}


def test_inventory_uses_current_full_repository_scope(tmp_path: Path) -> None:
    checkout = tmp_path
    subprocess.run(["git", "init"], cwd=checkout, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=checkout, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=checkout, check=True)
    (checkout / "app.py").write_text("def handler():\n    return 1\n", encoding="utf-8")
    (checkout / "untracked.py").write_text("def extra():\n    return 2\n", encoding="utf-8")
    ensure_project_files(checkout)
    subprocess.run(["git", "add", "app.py"], cwd=checkout, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=checkout, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    inventory = build_git_inventory(checkout, include_untracked=True)
    paths = {item["path"]: item for item in inventory["files"]}

    assert paths["app.py"]["scope"] == "analyze"
    assert paths["untracked.py"]["scope"] == "analyze"
    assert paths[".codereview/config.json"]["scope"] == "excluded"
    assert inventory["summary"]["inventory_mode"] == "full-repository-snapshot"


def test_repository_snapshot_uses_inventory_not_git_ref(tmp_path: Path) -> None:
    checkout = tmp_path
    (checkout / "app.py").write_text("def handler():\n    return 1\n", encoding="utf-8")
    inventory = {"files": [{"path": "app.py", "scope": "analyze"}]}

    snapshot = analyze_repository_snapshot(checkout, inventory)
    symbols = map_repository_symbols(checkout, snapshot)

    assert snapshot.files == ["app.py"]
    assert snapshot.spans[0]["file"] == "app.py"
    assert [(item["symbol"], item["line"]) for item in symbols] == [("handler", 1)]


def test_finder_assignment_uses_review_unit_risk_tags() -> None:
    assert choose_finders({"auth", "api-contract", "concurrency"}) == [
        "correctness",
        "security_auth_dataflow",
        "api_contract",
        "state_concurrency_resource",
        "test_repro",
    ]
    tasks = plan_finder_tasks([{"unit_id": "component:1", "unit_type": "component", "risk_tags": ["auth"]}])
    assert tasks[0] == FinderTask(unit_id="component:1", focus="correctness", unit_type="component", review_pass="baseline", risk_tags=["auth"])
    assert {task.focus for task in tasks} >= {"correctness", "security_auth_dataflow", "test_repro"}


def test_fast_profile_does_not_cap_full_review_unit_coverage() -> None:
    config = ReviewConfig(mode="fast")
    nodes = [
        {
            "id": f"sym:python:src/app.py::func_{index}::function::{index}",
            "kind": "function",
            "name": f"func_{index}",
            "qualified_name": f"func_{index}",
            "file": "src/app.py",
            "span": {"start_line": index + 1, "end_line": index + 1},
            "attributes": ["source"],
        }
        for index in range(20)
    ]
    graph = {"nodes": nodes, "edges": [], "unresolved_refs": []}
    inventory = {"files": [{"path": "src/app.py", "scope": "analyze"}]}

    units = build_all_review_units(graph, inventory, {"packages": []}, config)
    coverage = build_unit_coverage(graph, inventory, units)

    assert coverage["production_symbols"] == 20
    assert coverage["covered_production_symbols"] == 20
    assert coverage["review_units"] >= 20


def test_candidate_pipeline_requires_unit_graph_evidence(tmp_path: Path) -> None:
    checkout = tmp_path / "repo"
    run = checkout / ".codereview" / "runs" / "run_1"
    checkout.mkdir(parents=True)
    (checkout / "src").mkdir()
    (checkout / "src" / "app.py").write_text("def handle(value):\n    return value.strip()\n", encoding="utf-8")
    write_review_units(
        run,
        [
            {
                "unit_id": "component:handle",
                "unit_type": "component",
                "review_pass": "baseline",
                "file": "src/app.py",
                "symbol": "handle",
                "line": 1,
                "risk_tags": ["source"],
                "context": {},
            }
        ],
    )
    raw = [
        {
            "task": {"unit_id": "component:handle", "focus": "correctness"},
            "result": {
                "candidates": [
                    {
                        "candidate_id": "issue_1",
                        "dedupe_key": "correctness|component:handle|src/app.py|none-input",
                        "severity": "high",
                        "category": "correctness",
                        "confidence": "high",
                        "claim": "Bug",
                        "graph_evidence": {
                            "unit_id": "component:handle",
                            "context_files": ["src/app.py"],
                            "path_summary": ["handle -> strip"],
                        },
                        "evidence": [{"file": "src/app.py", "lines": "1-2", "why_it_matters": "repository path"}],
                        "trigger_condition": "None input",
                        "expected_behavior": "rejects input",
                        "expected_behavior_source": ["component:handle repository invariant"],
                        "actual_behavior_hypothesis": "raises AttributeError",
                        "minimal_repro_idea": "call handle(None)",
                        "repro_likelihood": "high",
                        "needs_network": False,
                    },
                    {
                        "candidate_id": "issue_2",
                        "dedupe_key": "correctness|component:handle|src/app.py|old-field",
                        "severity": "high",
                        "category": "correctness",
                        "confidence": "high",
                        "claim": "Invalid old evidence",
                        "graph_evidence": {
                            "unit_id": "component:handle",
                            "files": ["src/app.py"],
                            "path_summary": ["handle"],
                        },
                        "evidence": [{"file": "src/app.py", "lines": "1", "why_it_matters": "repository path"}],
                        "trigger_condition": "input",
                        "expected_behavior": "rejects input",
                        "expected_behavior_source": ["component:handle repository invariant"],
                        "actual_behavior_hypothesis": "fails",
                        "minimal_repro_idea": "call function",
                        "repro_likelihood": "high",
                    },
                    {
                        "candidate_id": "issue_3",
                        "dedupe_key": "correctness|component:handle|src/app.py|empty-source",
                        "severity": "high",
                        "category": "correctness",
                        "confidence": "high",
                        "claim": "Invalid expected source",
                        "graph_evidence": {
                            "unit_id": "component:handle",
                            "context_files": ["src/app.py"],
                            "path_summary": ["handle"],
                        },
                        "evidence": [{"file": "src/app.py", "lines": "1", "why_it_matters": "repository path"}],
                        "trigger_condition": "input",
                        "expected_behavior": "rejects input",
                        "expected_behavior_source": [],
                        "actual_behavior_hypothesis": "fails",
                        "minimal_repro_idea": "call function",
                        "repro_likelihood": "high",
                    },
                ]
            },
        }
    ]

    normalized = normalize_candidates(raw, checkout=checkout, run=run)

    assert [item["issue_id"] for item in normalized if item["valid"]] == ["issue_1"]
    assert any("graph_evidence has unexpected fields" in "; ".join(item["invalid_reasons"]) for item in normalized)
    assert any("expected_behavior_source must be a non-empty list" in "; ".join(item["invalid_reasons"]) for item in normalized)


def test_finder_codex_failure_is_blocked_not_silent_empty(tmp_path: Path) -> None:
    checkout = tmp_path / "repo"
    run = checkout / ".codereview" / "runs" / "run_1"
    checkout.mkdir(parents=True)
    ensure_project_files(checkout)
    write_review_units(
        run,
        [{"unit_id": "component:handle", "unit_type": "component", "review_pass": "baseline", "risk_tags": [], "context": {}}],
    )
    fake_codex = write_fake_cli(
        tmp_path,
        "fake_codex_fails",
        r'''
import sys

print("finder stdout detail")
print("finder failed", file=sys.stderr)
sys.exit(42)
''',
    )
    config = ReviewConfig()
    config.codex.command = str(fake_codex)

    result = run_finder(checkout, run, FinderTask(unit_id="component:handle", focus="correctness"), config)

    assert result["status"] == "blocked"
    assert result["result"]["candidates"] == []
    assert "exit code 42" in result["blocked_reason"]
    assert "stderr: finder failed" in result["blocked_reason"]
    assert "stdout: finder stdout detail" in result["blocked_reason"]


def test_pipeline_summary_preserves_blocked_process_evidence() -> None:
    snapshot = type("Snapshot", (), {"files": [], "spans": []})()
    raw_candidates = [
        {
            "status": "blocked",
            "task": {"unit_id": "component:handle", "focus": "correctness"},
            "blocked_reason": "finder failed",
            "process": {
                "command": ["codex", "exec", "-"],
                "returncode": 1,
                "timed_out": False,
                "duration_ms": 123,
                "queueWaitMs": 7,
                "stdout": "stdout detail",
                "stderr": "stderr detail",
                "stdout_path": "/tmp/stdout.log",
                "stderr_path": "/tmp/stderr.log",
            },
            "result": {"candidates": []},
        }
    ]

    summary = codereview_main.build_pipeline_summary(
        preflight={"ok": True},
        snapshot=snapshot,
        review_units=[],
        unit_coverage={},
        snapshot_manifest={},
        finder_tasks=[FinderTask(unit_id="component:handle", focus="correctness")],
        raw_candidates=raw_candidates,
        normalized=[],
        deduped=[],
        scored=[],
        selected=[],
        repro_results=[],
        judge_results=[],
        confirmed=[],
        rejected=[],
    )

    item = summary["finder"]["blockedItems"][0]
    assert item["unitId"] == "component:handle"
    assert item["reason"] == "finder failed"
    assert item["process"]["queueWaitMs"] == 7
    assert item["process"]["stdoutTail"] == "stdout detail"


def test_judge_and_report_are_confirmed_only(tmp_path: Path) -> None:
    worker = tmp_path / "worker"
    (worker / "logs").mkdir(parents=True)
    (worker / "logs" / "repro.log").write_text("observable failure", encoding="utf-8")
    candidate = {
        "candidate_id": "issue_1",
        "issue_id": "issue_1",
        "claim": "Confirmed bug",
        "severity": "high",
        "category": "correctness",
        "graph_evidence": {
            "unit_id": "component:handle",
            "context_files": ["src/app.py"],
            "path_summary": ["entrypoint -> sink"],
        },
        "evidence": [{"file": "src/app.py", "lines": "10-12", "why_it_matters": "repository path"}],
        "code_evidence": ["src/app.py:10-12"],
        "trigger_condition": "bad input",
        "expected_behavior": "reject",
        "actual_behavior_hypothesis": "accept",
        "minimal_repro_idea": "run test",
    }
    repro = {
        "candidate_id": "issue_1",
        "worker": str(worker),
        "result": {
            "candidate_id": "issue_1",
            "status": "reproduced",
            "level": "L2",
            "summary": "observable failure",
            "commands_run": [{"cmd": "python repro.py", "cwd": str(worker), "exit_code": 1, "log_path": "logs/repro.log"}],
            "proof": {"type": "runtime_output", "expected": "reject", "actual": "observable failure", "log_excerpt": "observable failure"},
            "graph_path_exercised": True,
            "files_written": ["logs/repro.log"],
            "why_valid": "local command exercises the path",
            "why_not_reproduced": "",
            "safety_notes": "",
        },
        "filesystem_violations": [],
    }

    judge = local_judge(candidate, repro)
    confirmed = collect_confirmed([candidate], [repro], [judge])
    report = render_final_report(
        confirmed,
        [{"candidate_id": "issue_2"}],
        run_id="run",
        mode="standard",
        coverage={"review_units": 1, "baseline_reviewed_units": 1, "production_symbols": 1, "covered_production_symbols": 1},
        snapshot={"copied_files_count": 1},
    )

    assert judge["safe_to_show_user"] is True
    assert len(confirmed) == 1
    assert "# Full-Repository Graph-Verified Code Review" in report
    assert "Confirmed bug" in report
    assert "issue_2" not in report


def test_final_report_states_no_confirmed_findings_with_passed_coverage() -> None:
    report = render_final_report(
        [],
        [],
        run_id="run",
        mode="standard",
        coverage={
            "review_units": 2,
            "baseline_reviewed_units": 2,
            "production_symbols": 4,
            "covered_production_symbols": 4,
            "critical_unresolved_graph_edges": 0,
        },
        snapshot={"copied_files_count": 1},
    )

    assert "No confirmed findings." in report
    assert "Full-repository coverage: passed." in report


def test_repro_event_precheck_requires_log_path_and_excerpt_in_events(tmp_path: Path) -> None:
    worker = tmp_path / "worker"
    (worker / "logs").mkdir(parents=True)
    (worker / "logs" / "repro.log").write_text("observable failure", encoding="utf-8")
    events = tmp_path / "events.jsonl"
    events.write_text(
        json.dumps({"cmd": "python repro.py", "cwd": str(worker), "exit_code": 1}) + "\n",
        encoding="utf-8",
    )
    repro = {
        "worker": str(worker),
        "process": {"events_path": str(events)},
        "result": {
            "commands_run": [{"cmd": "python repro.py", "cwd": str(worker), "exit_code": 1, "log_path": "logs/repro.log"}],
            "proof": {"actual": "observable failure", "log_excerpt": "observable failure"},
        },
    }

    result = verify_repro_events_and_paths(repro)

    assert result["status"] == "rejected"
    assert "command log path not found" in result["reason"]


def test_repro_event_precheck_accepts_complete_event_evidence(tmp_path: Path) -> None:
    worker = tmp_path / "worker"
    (worker / "logs").mkdir(parents=True)
    (worker / "logs" / "repro.log").write_text("observable failure", encoding="utf-8")
    events = tmp_path / "events.jsonl"
    events.write_text(
        json.dumps(
            {
                "type": "exec_command",
                "cmd": "python repro.py",
                "cwd": str(worker),
                "exit_code": 1,
                "log_path": "logs/repro.log",
                "stderr": "observable failure",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    repro = {
        "worker": str(worker),
        "process": {"events_path": str(events)},
        "result": {
            "commands_run": [{"cmd": "python repro.py", "cwd": str(worker), "exit_code": 1, "log_path": "logs/repro.log"}],
            "proof": {"actual": "observable failure", "log_excerpt": "observable failure"},
        },
    }

    result = verify_repro_events_and_paths(repro)

    assert result["status"] == "passed"


def test_repro_worker_dir_uses_snapshot_repo_without_extra_checkouts(tmp_path: Path) -> None:
    checkout = tmp_path / "repo"
    checkout.mkdir()
    subprocess.run(["git", "init"], cwd=checkout, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=checkout, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=checkout, check=True)
    (checkout / "app.py").write_text("print('ok')\n", encoding="utf-8")
    subprocess.run(["git", "add", "app.py"], cwd=checkout, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=checkout, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    worker = tmp_path / "worker"
    before = git_status_porcelain(checkout)
    create_worker_dir(checkout, worker, {"candidate_id": "issue_1"})
    after = git_status_porcelain(checkout)

    assert before == after == []
    assert (worker / "repo" / ".git").exists()
    assert sorted(path.name for path in worker.iterdir()) == [
        "cache",
        "candidate.json",
        "home",
        "input_candidate.json",
        "logs",
        "repo",
        "repro",
        "tmp",
    ]
    assert (worker / "input_candidate.json").is_file()


def test_worker_env_and_codex_base_env_are_isolated(tmp_path: Path) -> None:
    worker = tmp_path / "worker"
    env = worker_env(worker)

    assert env["HOME"] == str(worker / "home")
    assert env["TMPDIR"] == str(worker / "tmp")
    assert env["XDG_CACHE_HOME"] == str(worker / "cache")

    config = ReviewConfig()
    config.codex.env = {"HOME": str(tmp_path / "home"), "CODEX_HOME": str(tmp_path / "home" / ".codex")}
    provider_env = base_env(tmp_path, config.codex)
    assert provider_env["HOME"] == str(tmp_path / "home")
    assert provider_env["CODEX_HOME"] == str(tmp_path / "home" / ".codex")


def test_codex_exec_places_approval_flag_before_exec_when_only_top_level_supports_it(tmp_path: Path, monkeypatch: _MonkeyPatch) -> None:
    from codereview import codex_runner

    captured = {}

    def fake_run_process(command, *, cwd, env=None, timeout=600, queue_wait_ms=0, **kwargs):
        del env, timeout, queue_wait_ms
        captured["command"] = command
        captured["stdin_text"] = kwargs.get("stdin_text")
        return ProcessResult(command, str(cwd), 0, "{}", "", 1)

    monkeypatch.setattr(codex_runner, "run_process", fake_run_process)
    monkeypatch.setattr(
        codex_runner,
        "codex_cli_capabilities",
        lambda command, env=None: codex_runner.CodexCliCapabilities(
            frozenset({"--ask-for-approval"}),
            frozenset({"--cd", "--skip-git-repo-check", "--sandbox", "--output-schema", "--output-last-message", "--json", "--model", "--config"}),
        ),
    )
    config = ReviewConfig()
    schema = tmp_path / "schema.json"
    schema.write_text("{}", encoding="utf-8")

    result = codex_runner.run_codex_exec(
        cd=tmp_path,
        prompt="review",
        output_schema=schema,
        output_file=tmp_path / "result.json",
        sandbox="read-only",
        timeout_seconds=30,
        config=config.codex,
    )

    assert result.returncode == 0
    command = captured["command"]
    assert command[:4] == ["codex", "--ask-for-approval", "never", "exec"]
    assert command[command.index("--sandbox") + 1] == "workspace-write"
    assert command[-1] == "-"
    assert captured["stdin_text"] == "review"


def test_codex_exec_copies_workspace_local_output_to_requested_path(tmp_path: Path, monkeypatch: _MonkeyPatch) -> None:
    from codereview import codex_runner

    checkout = tmp_path / "repo"
    checkout.mkdir()
    schema = checkout / "schema.json"
    schema.write_text("{}", encoding="utf-8")
    requested_output = tmp_path / "run" / "workers" / "result.json"
    captured = {}

    def fake_run_process(command, *, cwd, env=None, timeout=600, queue_wait_ms=0, **kwargs):
        del env, timeout, queue_wait_ms, kwargs
        output_arg = Path(command[command.index("--output-last-message") + 1])
        captured["output_arg"] = output_arg
        assert output_arg.resolve(strict=False).relative_to(Path(cwd).resolve(strict=False))
        output_arg.parent.mkdir(parents=True, exist_ok=True)
        output_arg.write_text('{"ok": true}', encoding="utf-8")
        return ProcessResult(command, str(cwd), 0, "{}", "", 1)

    monkeypatch.setattr(codex_runner, "run_process", fake_run_process)
    monkeypatch.setattr(
        codex_runner,
        "codex_cli_capabilities",
        lambda command, env=None: codex_runner.CodexCliCapabilities(
            frozenset(),
            frozenset({"--cd", "--skip-git-repo-check", "--sandbox", "--output-schema", "--output-last-message", "--json"}),
        ),
    )

    result = codex_runner.run_codex_exec(
        cd=checkout,
        prompt="review",
        output_schema=schema,
        output_file=requested_output,
        sandbox="read-only",
        timeout_seconds=30,
        config=ReviewConfig().codex,
    )

    assert result.returncode == 0
    assert captured["output_arg"] != requested_output
    assert json.loads(requested_output.read_text(encoding="utf-8")) == {"ok": True}


def test_repository_census_failure_includes_process_output(tmp_path: Path, monkeypatch: _MonkeyPatch) -> None:
    from codereview.graph import census as census_module

    checkout = tmp_path / "repo"
    checkout.mkdir()
    ensure_project_files(checkout)
    (checkout / "app.py").write_text("def handle(value):\n    return value\n", encoding="utf-8")
    run = tmp_path / "run"
    inventory = {
        "summary": {"inventory_mode": "test"},
        "files": [
            {
                "path": "app.py",
                "scope": "analyze",
                "size_bytes": 36,
                "line_count": 2,
                "content_hash": "sha256:test",
                "extension": ".py",
            }
        ],
    }
    stderr = "Error: failed to initialize in-process app-server client: Read-only file system (os error 30)"

    def fake_run_codex_exec(**kwargs):
        del kwargs
        return ProcessResult(["codex", "exec"], str(checkout), 1, "", stderr, 12)

    monkeypatch.setattr(census_module, "run_codex_exec", fake_run_codex_exec)
    try:
        run_repository_census(checkout, run, inventory, ReviewConfig())
    except RuntimeError as exc:
        message = str(exc)
    else:
        raise AssertionError("expected repository census to fail")

    assert "repository census agent failed with exit code 1" in message
    assert "failed to initialize in-process app-server client" in message
    process_payload = json.loads((run / "workers" / "census-0001" / "process.json").read_text(encoding="utf-8"))
    assert process_payload["stderr"] == stderr


def test_filesystem_guard_rejects_missing_or_outside_logs(tmp_path: Path) -> None:
    worker = tmp_path / "worker"
    (worker / "logs").mkdir(parents=True)
    (worker / "logs" / "repro.log").write_text("failed as expected", encoding="utf-8")

    assert guard_worker_result(worker, {"files_written": ["notes.txt"], "commands_run": [{"log_path": "logs/repro.log"}]}) == []
    assert "outside worker" in "; ".join(
        guard_worker_result(worker, {"files_written": ["../escape.txt"], "commands_run": [{"log_path": "logs/repro.log"}]})
    )
    assert "missing" in "; ".join(guard_worker_result(worker, {"files_written": []}))


def test_codex_judge_cannot_promote_failed_local_gate(tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    ensure_project_files(checkout)
    fake_codex = write_fake_cli(
        tmp_path,
        "fake_codex_judge",
        r'''
import json
import sys
from pathlib import Path

args = sys.argv[1:]
out = ""
for index, arg in enumerate(args):
    if arg == "--output-last-message" and index + 1 < len(args):
        out = args[index + 1]
payload = {
    "candidate_id": "issue_1",
    "status": "confirmed",
    "level": "L2",
    "safe_to_show_user": True,
    "reason": "agent tried to promote",
    "evidence_summary": {"command": "python repro.py", "log_path": "logs/missing.log", "observable": "failure"},
    "limitations": [],
}
Path(out).parent.mkdir(parents=True, exist_ok=True)
Path(out).write_text(json.dumps(payload), encoding="utf-8")
''',
    )
    config = ReviewConfig()
    config.codex.command = str(fake_codex)
    worker = tmp_path / "worker"
    worker.mkdir()
    repro = {
        "candidate_id": "issue_1",
        "worker": str(worker),
        "result": {
            "candidate_id": "issue_1",
            "status": "reproduced",
            "level": "L2",
            "summary": "failure",
            "commands_run": [{"cmd": "python repro.py", "cwd": str(worker), "exit_code": 1, "log_path": "logs/missing.log"}],
            "proof": {"type": "runtime_output", "expected": "safe", "actual": "failure", "log_excerpt": "failure"},
            "graph_path_exercised": True,
            "files_written": [],
            "why_valid": "ran command",
            "why_not_reproduced": "",
            "safety_notes": "",
        },
        "filesystem_violations": ["log path missing: logs/missing.log"],
    }

    judge = run_judge(tmp_path / "run", {"candidate_id": "issue_1", "issue_id": "issue_1"}, repro, checkout, config)

    assert judge["status"] == "rejected"
    assert judge["safe_to_show_user"] is False
    assert "missing" in judge["reason"]


def test_run_review_writes_full_repository_report_with_stubbed_repro(tmp_path: Path) -> None:
    checkout = tmp_path / "repo"
    checkout.mkdir()
    subprocess.run(["git", "init"], cwd=checkout, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=checkout, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=checkout, check=True)
    (checkout / "app.py").write_text("def handle(value):\n    return value.strip()\n", encoding="utf-8")
    subprocess.run(["git", "add", "app.py"], cwd=checkout, check=True)
    subprocess.run(["git", "commit", "-m", "snapshot"], cwd=checkout, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    ensure_project_files(checkout)
    config = json.loads((checkout / ".codereview" / "config.json").read_text(encoding="utf-8"))
    config["graph"]["codex_census"] = False
    config["graph"]["codex_mappers"] = False
    config["graph"]["use_sqlite_index"] = False
    config["finders"]["enabled"] = True
    (checkout / ".codereview" / "config.json").write_text(json.dumps(config), encoding="utf-8")

    def fake_run_finders(snapshot_repo: Path, run: Path, tasks: list[FinderTask], config: ReviewConfig) -> list[dict]:
        del snapshot_repo, run, config
        rows = []
        candidate_written = False
        for task in tasks:
            candidates = []
            if task.focus == "correctness" and not candidate_written:
                candidate_written = True
                candidates.append(
                    {
                        "candidate_id": "issue_1",
                        "dedupe_key": f"correctness|{task.unit_id}|app.py|none-input",
                        "severity": "high",
                        "category": "correctness",
                        "confidence": "high",
                        "claim": "Confirmed bug",
                        "graph_evidence": {
                            "unit_id": task.unit_id,
                            "context_files": ["app.py"],
                            "path_summary": ["handle"],
                        },
                        "evidence": [{"file": "app.py", "lines": "1-2", "why_it_matters": "repository behavior"}],
                        "trigger_condition": "value is None",
                        "expected_behavior": "rejects None",
                        "expected_behavior_source": ["function contract should reject invalid input"],
                        "actual_behavior_hypothesis": "raises AttributeError",
                        "minimal_repro_idea": "call handle(None)",
                        "repro_likelihood": "high",
                        "needs_network": False,
                    }
                )
            rows.append({"task": task.__dict__, "result": {"unit_id": task.unit_id, "focus": task.focus, "candidates": candidates}, "status": "ok"})
        return rows

    def fake_repro(snapshot_repo: Path, run: Path, selected: list[dict], config: ReviewConfig) -> list[dict]:
        del snapshot_repo, config
        worker = run / "workers" / "issue_1"
        (worker / "logs").mkdir(parents=True)
        (worker / "logs" / "repro.log").write_text("AttributeError", encoding="utf-8")
        return [
            {
                "candidate_id": "issue_1",
                "worker": str(worker),
                "result": {
                    "candidate_id": "issue_1",
                    "status": "reproduced",
                    "level": "L2",
                    "summary": "AttributeError",
                    "commands_run": [{"cmd": "python repro.py", "cwd": str(worker), "exit_code": 1, "log_path": "logs/repro.log"}],
                    "files_written": ["logs/repro.log"],
                    "proof": {"type": "runtime_output", "expected": "rejects None", "actual": "AttributeError", "log_excerpt": "AttributeError"},
                    "graph_path_exercised": True,
                    "why_valid": "local command exercises handle",
                    "why_not_reproduced": "",
                    "safety_notes": "",
                },
                "filesystem_violations": [],
            }
        ]

    def fake_judge(run: Path, selected: list[dict], repro_results: list[dict], snapshot_repo: Path, config: ReviewConfig) -> list[dict]:
        del run, selected, repro_results, snapshot_repo, config
        return [
            {
                "candidate_id": "issue_1",
                "status": "confirmed",
                "level": "L2",
                "safe_to_show_user": True,
                "reason": "confirmed",
                "evidence_summary": {"command": "python repro.py", "log_path": "logs/repro.log", "observable": "AttributeError"},
                "limitations": [],
            }
        ]

    patcher = _MonkeyPatch()
    try:
        patcher.setattr(codereview_main, "run_finders_parallel", fake_run_finders)
        patcher.setattr(codereview_main, "run_repro_workers_parallel", fake_repro)
        patcher.setattr(codereview_main, "run_judges_parallel", fake_judge)
        final = codereview_main.run_review(checkout, mode="fast")
    finally:
        patcher.undo()

    final_text = final.read_text(encoding="utf-8")
    summary = json.loads(final.with_name("summary.json").read_text(encoding="utf-8"))

    assert "# Full-Repository Graph-Verified Code Review" in final_text
    assert "Confirmed findings: 1" in final_text
    assert summary["reviewUnits"]["count"] >= 1
    assert summary["reviewUnits"]["coverage"]["baseline_reviewed_units"] == summary["reviewUnits"]["count"]
    assert (final.parent.parent / "artifacts" / "review-units" / "coverage-executed.json").is_file()
    assert not (final.parent.parent / "artifacts" / "inventory" / "diff.json").exists()


def write_fake_cli(directory: Path, name: str, body: str) -> Path:
    script = directory / f"{name}.py"
    script.write_text(body.strip() + "\n", encoding="utf-8")
    if os.name == "nt":
        wrapper = directory / f"{name}.cmd"
        wrapper.write_text(f'@echo off\r\n"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")
        return wrapper
    wrapper = directory / name
    wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding="utf-8")
    wrapper.chmod(0o755)
    return wrapper


if __name__ == "__main__":
    unittest.main()
