from __future__ import annotations

import importlib
import inspect
import json
import os
import subprocess
import threading
import tempfile
import time
import unittest
from pathlib import Path
from typing import Optional

codereview_main = importlib.import_module("codereview.main")
from codereview.candidates.normalize import normalize_candidates
from codereview import app_server_runner
from codereview.codex_runner import base_env
from codereview.config import ReviewConfig, load_config
from codereview.finder import runner as finder_runner_module
from codereview.finder.runner import run_finder, run_finders_parallel
from codereview.finder.tasks import FinderTask, plan_finder_tasks
from codereview.graph.audit import audit_graph
from codereview.graph.census import run_repository_census
from codereview.graph.link import _valid_link_result
from codereview.graph import mapper as graph_mapper_module
from codereview.graph.merge import merge_graph_results, normalize_graph_for_inventory
from codereview.graph.scheduler import plan_graph_tasks
from codereview.inventory.git_inventory import analyzable_files, build_git_inventory
from codereview.judge import runner as judge_runner_module
from codereview.judge.runner import run_judge
from codereview.judge.precheck import verify_repro_events_and_paths
from codereview.judge.validate import local_judge
from codereview.report.render import collect_confirmed, collect_rejected, render_final_report
from codereview.repository.snapshot import analyze_repository_snapshot
from codereview.repository.symbols import map_repository_symbols
from codereview.repro import runner as repro_runner_module
from codereview.repro.filesystem_guard import guard_worker_result
from codereview.repro.runner import git_status_porcelain, worker_env
from codereview.repro.worker_dir import create_worker_dir
from codereview.review import candidate_verifier as candidate_verifier_module
from codereview.snapshot import create_immutable_snapshot
from codereview.templates import ensure_project_files
from codereview.units.coverage import build_unit_coverage
from codereview.units.context import write_review_units
from codereview.units.planner import build_all_review_units
from codereview.units.risk_tags import choose_finders
from codereview.utils.jsonl import write_json, write_jsonl
from codereview.utils.process import ProcessCancelled, ProcessResult, clear_process_cancel_event, set_process_cancel_event


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
            app_server_runner.reset_app_server_clients_for_tests()
            if patcher is not None:
                patcher.undo()


def test_init_writes_v3_codereview_assets(tmp_path: Path) -> None:
    ensure_project_files(tmp_path)

    config = json.loads((tmp_path / ".codereview" / "config.json").read_text(encoding="utf-8"))
    schema = json.loads((tmp_path / ".codereview" / "schemas" / "finder_result.schema.json").read_text(encoding="utf-8"))

    assert config["scan"]["mode"] == "full-cached"
    assert config["graph"]["schema_version"] == "3"
    assert config["graph"]["target_shards"] == 12
    assert config["graph"]["mapper_subagent_limit"] == 6
    assert config["graph"]["codex_mappers"] is False
    assert config["graph"]["map_parallel"] == 2
    assert config["graph"]["graph_timeout_seconds"] == 960
    assert config["finders"]["turn_parallel"] == 1
    assert "impact" not in config
    assert set(schema["required"]) == set(schema["properties"])
    assert (tmp_path / ".codereview" / "schemas" / "graph-shard-batch.schema.json").is_file()
    assert (tmp_path / ".codereview" / "schemas" / "finder-batch.schema.json").is_file()
    assert (tmp_path / ".codereview" / "prompts" / "graph-mapper-coordinator.md").is_file()
    assert (tmp_path / ".codereview" / "prompts" / "finder-batch-coordinator.md").is_file()
    graph_props = schema["properties"]["candidates"]["items"]["properties"]["graph_evidence"]["properties"]
    assert set(graph_props) == {"unit_id", "context_files", "path_summary"}


def test_write_json_does_not_follow_symlink(tmp_path: Path) -> None:
    outside = tmp_path / "outside.json"
    outside.write_text('{"outside": true}', encoding="utf-8")
    target = tmp_path / "artifact.json"
    target.symlink_to(outside)

    write_json(target, {"ok": True})

    assert not target.is_symlink()
    assert json.loads(target.read_text(encoding="utf-8")) == {"ok": True}
    assert outside.read_text(encoding="utf-8") == '{"outside": true}'


def test_write_jsonl_does_not_follow_symlink(tmp_path: Path) -> None:
    outside = tmp_path / "outside.jsonl"
    outside.write_text('{"outside": true}\n', encoding="utf-8")
    target = tmp_path / "artifact.jsonl"
    target.symlink_to(outside)

    write_jsonl(target, [{"ok": True}])

    assert not target.is_symlink()
    assert target.read_text(encoding="utf-8") == '{"ok": true}\n'
    assert outside.read_text(encoding="utf-8") == '{"outside": true}\n'


def test_generated_codex_schemas_are_strict_objects(tmp_path: Path) -> None:
    ensure_project_files(tmp_path)

    schema_dir = tmp_path / ".codereview" / "schemas"
    for path in schema_dir.glob("*.schema.json"):
        schema = json.loads(path.read_text(encoding="utf-8"))
        assert_strict_object_schema(schema, path.name)


def test_init_refreshes_existing_managed_codex_schemas(tmp_path: Path) -> None:
    schema_dir = tmp_path / ".codereview" / "schemas"
    schema_dir.mkdir(parents=True)
    stale = schema_dir / "repo-census.schema.json"
    stale.write_text(
        json.dumps({"type": "object", "additionalProperties": True, "properties": {}}),
        encoding="utf-8",
    )

    ensure_project_files(tmp_path)

    schema = json.loads(stale.read_text(encoding="utf-8"))
    assert schema["additionalProperties"] is False
    assert "languages" in schema["properties"]


def assert_strict_object_schema(value: object, location: str) -> None:
    if isinstance(value, list):
        for index, item in enumerate(value):
            assert_strict_object_schema(item, f"{location}[{index}]")
        return
    if not isinstance(value, dict):
        return
    if "object" in schema_type_names(value.get("type")):
        assert value.get("additionalProperties") is False, location
        properties = value.get("properties")
        if isinstance(properties, dict):
            assert set(value.get("required") or []) == set(properties), location
    for key, item in value.items():
        assert_strict_object_schema(item, f"{location}.{key}")


def schema_type_names(value: object) -> set[str]:
    if isinstance(value, str):
        return {value}
    if isinstance(value, list):
        return {item for item in value if isinstance(item, str)}
    return set()


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


def test_inventory_excludes_tracked_paths_missing_from_worktree(tmp_path: Path) -> None:
    checkout = tmp_path
    subprocess.run(["git", "init"], cwd=checkout, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=checkout, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=checkout, check=True)
    removed = checkout / "removed.py"
    removed.write_text("def removed():\n    return 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "removed.py"], cwd=checkout, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=checkout, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    removed.unlink()

    inventory = build_git_inventory(checkout, include_untracked=True)
    paths = {item["path"]: item for item in inventory["files"]}

    assert paths["removed.py"]["scope"] == "excluded"
    assert paths["removed.py"]["reason"] == "missing-or-non-file"
    assert "removed.py" not in {str(item.get("path") or "") for item in analyzable_files(inventory)}


def test_snapshot_fails_before_graph_when_analyzable_inventory_file_is_missing(tmp_path: Path) -> None:
    checkout = tmp_path / "repo"
    run = checkout / ".codereview" / "runs" / "run_1"
    checkout.mkdir(parents=True)
    inventory = {"files": [{"path": "missing.py", "scope": "analyze"}]}

    try:
        create_immutable_snapshot(checkout, inventory, run)
    except RuntimeError as exc:
        message = str(exc)
    else:
        raise AssertionError("expected immutable snapshot creation to fail")

    assert "immutable snapshot missing analyzable inventory files" in message
    assert "missing.py" in message


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
    assert choose_finders({"auth", "api-contract", "concurrency"}) == ["correctness", "security_auth_dataflow"]
    assert choose_finders({"state"}) == ["correctness", "state_concurrency_resource"]
    assert choose_finders({"trust-boundary"}) == ["correctness", "security_auth_dataflow"]
    tasks = plan_finder_tasks([{"unit_id": "component:1", "unit_type": "component", "risk_tags": ["auth"]}])
    assert tasks[0] == FinderTask(unit_id="component:1", focus="correctness", unit_type="component", review_pass="baseline", risk_tags=["auth"])
    assert [task.focus for task in tasks] == ["correctness", "security_auth_dataflow"]


def test_finder_assignment_keeps_boundary_and_global_specialists() -> None:
    tasks = plan_finder_tasks(
        [
            {"unit_id": "boundary:1", "unit_type": "cross_boundary", "risk_tags": ["isolated-helper"]},
            {"unit_id": "global:1", "unit_type": "global_invariant", "risk_tags": []},
        ]
    )

    by_unit = {}
    for task in tasks:
        by_unit.setdefault(task.unit_id, []).append(task.focus)

    assert by_unit["boundary:1"] == ["correctness", "api_contract"]
    assert by_unit["global:1"] == ["correctness", "security_auth_dataflow", "state_concurrency_resource", "test_repro"]


def test_default_finder_batch_width_is_six(tmp_path: Path) -> None:
    config = load_config(tmp_path)

    assert config.finders.max_workers == 6
    assert config.finders.turn_parallel == 1


def test_run_finders_batches_tasks_into_single_app_server_waves(tmp_path: Path, monkeypatch: _MonkeyPatch) -> None:
    checkout = tmp_path / "repo"
    run = checkout / ".codereview" / "runs" / "run_1"
    checkout.mkdir(parents=True)
    ensure_project_files(checkout)
    tasks = [FinderTask(unit_id=f"component:{index}", focus="correctness", unit_type="component") for index in range(7)]
    write_review_units(
        run,
        [
            {"unit_id": task.unit_id, "unit_type": task.unit_type, "review_pass": task.review_pass, "risk_tags": [], "context": {}}
            for task in tasks
        ],
    )
    config = ReviewConfig()
    config.finders.max_workers = 6
    calls: list[dict] = []

    def fake_run_codex_turn(**kwargs):
        calls.append(kwargs)
        assert kwargs["output_schema"].name == "finder-batch.schema.json"
        prompt = kwargs["prompt"]
        payload_text = prompt.split("Assigned finder batch JSON:\n```json\n", 1)[1].split("\n```", 1)[0]
        payload = json.loads(payload_text)
        results = [
            {"unit_id": job["unit_id"], "focus": job["focus"], "context_requests": [], "candidates": []}
            for job in payload["jobs"]
        ]
        kwargs["output_file"].parent.mkdir(parents=True, exist_ok=True)
        kwargs["output_file"].write_text(json.dumps({"results": results, "warnings": []}), encoding="utf-8")
        return ProcessResult(
            command=["codex", "app-server", "turn/start"],
            cwd=str(checkout),
            returncode=0,
            stdout="",
            stderr="",
            duration_ms=25,
        )

    monkeypatch.setattr(finder_runner_module, "run_codex_turn", fake_run_codex_turn)

    results = run_finders_parallel(checkout, run, tasks, config)

    assert len(calls) == 2
    assert sorted(
        len(json.loads(call["prompt"].split("Assigned finder batch JSON:\n```json\n", 1)[1].split("\n```", 1)[0])["jobs"])
        for call in calls
    ) == [1, 6]
    assert [item["task"]["unit_id"] for item in results] == [task.unit_id for task in tasks]
    assert all(item["status"] == "ok" for item in results)


def test_run_finders_runs_batches_as_parallel_app_server_turns(tmp_path: Path, monkeypatch: _MonkeyPatch) -> None:
    checkout = tmp_path / "repo"
    run = checkout / ".codereview" / "runs" / "run_1"
    checkout.mkdir(parents=True)
    ensure_project_files(checkout)
    tasks = [FinderTask(unit_id=f"component:{index}", focus="correctness", unit_type="component") for index in range(12)]
    write_review_units(
        run,
        [
            {"unit_id": task.unit_id, "unit_type": task.unit_type, "review_pass": task.review_pass, "risk_tags": [], "context": {}}
            for task in tasks
        ],
    )
    config = ReviewConfig()
    config.finders.max_workers = 6
    config.finders.turn_parallel = 2
    lock = threading.Lock()
    second_turn_started = threading.Event()
    started_turns = 0
    first_turn_saw_parallelism: list[bool] = []

    def fake_run_codex_turn(**kwargs):
        nonlocal started_turns
        with lock:
            started_turns += 1
            turn_index = started_turns
            if started_turns >= 2:
                second_turn_started.set()
        if turn_index == 1:
            first_turn_saw_parallelism.append(second_turn_started.wait(timeout=2))
        prompt = kwargs["prompt"]
        payload_text = prompt.split("Assigned finder batch JSON:\n```json\n", 1)[1].split("\n```", 1)[0]
        payload = json.loads(payload_text)
        results = [
            {"unit_id": job["unit_id"], "focus": job["focus"], "context_requests": [], "candidates": []}
            for job in payload["jobs"]
        ]
        kwargs["output_file"].parent.mkdir(parents=True, exist_ok=True)
        kwargs["output_file"].write_text(json.dumps({"results": results, "warnings": []}), encoding="utf-8")
        return ProcessResult(["codex", "app-server", "turn/start"], str(checkout), 0, "{}", "", 1)

    monkeypatch.setattr(finder_runner_module, "run_codex_turn", fake_run_codex_turn)

    results = run_finders_parallel(checkout, run, tasks, config)

    assert first_turn_saw_parallelism == [True]
    assert len(results) == len(tasks)
    assert all(item["status"] == "ok" for item in results)


def test_run_finders_groups_module_context_before_batching(tmp_path: Path, monkeypatch: _MonkeyPatch) -> None:
    checkout = tmp_path / "repo"
    run = checkout / ".codereview" / "runs" / "run_1"
    checkout.mkdir(parents=True)
    ensure_project_files(checkout)
    tasks = [
        FinderTask(unit_id="component:auth-a", focus="correctness", unit_type="component"),
        FinderTask(unit_id="component:billing", focus="correctness", unit_type="component"),
        FinderTask(unit_id="component:auth-b", focus="api_contract", unit_type="component"),
    ]
    write_review_units(
        run,
        [
            {"unit_id": "component:auth-a", "unit_type": "component", "review_pass": "baseline", "risk_tags": [], "context_files": [{"path": "src/auth/login.py"}], "context": {}},
            {"unit_id": "component:billing", "unit_type": "component", "review_pass": "baseline", "risk_tags": [], "context_files": [{"path": "src/billing/invoice.py"}], "context": {}},
            {"unit_id": "component:auth-b", "unit_type": "component", "review_pass": "baseline", "risk_tags": [], "context_files": [{"path": "src/auth/token.py"}], "context": {}},
        ],
    )
    config = ReviewConfig()
    config.finders.max_workers = 2
    calls: list[list[str]] = []

    def fake_run_codex_turn(**kwargs):
        prompt = kwargs["prompt"]
        payload_text = prompt.split("Assigned finder batch JSON:\n```json\n", 1)[1].split("\n```", 1)[0]
        payload = json.loads(payload_text)
        calls.append([job["unit_id"] for job in payload["jobs"]])
        results = [
            {"unit_id": job["unit_id"], "focus": job["focus"], "context_requests": [], "candidates": []}
            for job in payload["jobs"]
        ]
        kwargs["output_file"].parent.mkdir(parents=True, exist_ok=True)
        kwargs["output_file"].write_text(json.dumps({"results": results, "warnings": []}), encoding="utf-8")
        return ProcessResult(
            command=["codex", "app-server", "turn/start"],
            cwd=str(checkout),
            returncode=0,
            stdout="",
            stderr="",
            duration_ms=25,
        )

    monkeypatch.setattr(finder_runner_module, "run_codex_turn", fake_run_codex_turn)

    results = run_finders_parallel(checkout, run, tasks, config)

    assert sorted(calls) == sorted([["component:auth-a", "component:auth-b"], ["component:billing"]])
    assert [item["task"]["unit_id"] for item in results] == [task.unit_id for task in tasks]


def test_context_repair_reruns_only_requesting_or_new_finder_tasks() -> None:
    old_tasks = [
        FinderTask(unit_id="component:a", focus="correctness"),
        FinderTask(unit_id="component:a", focus="test_repro", review_pass="specialist"),
        FinderTask(unit_id="component:b", focus="correctness"),
    ]
    new_tasks = [
        *old_tasks,
        FinderTask(unit_id="component:c", focus="correctness"),
    ]
    raw_candidates = [
        {
            "task": old_tasks[0].__dict__,
            "result": {
                "unit_id": "component:a",
                "focus": "correctness",
                "context_requests": [{"requested_files": ["a.py"], "reason": "need caller"}],
                "candidates": [],
            },
            "status": "ok",
        },
        {"task": old_tasks[1].__dict__, "result": {"unit_id": "component:a", "focus": "test_repro", "candidates": []}, "status": "ok"},
        {"task": old_tasks[2].__dict__, "result": {"unit_id": "component:b", "focus": "correctness", "candidates": []}, "status": "ok"},
    ]

    rerun, kept = codereview_main._finder_context_repair_rerun_plan(raw_candidates, old_tasks, new_tasks)

    assert [(task.unit_id, task.focus) for task in rerun] == [
        ("component:a", "correctness"),
        ("component:a", "test_repro"),
        ("component:c", "correctness"),
    ]
    assert [(item["task"]["unit_id"], item["task"]["focus"]) for item in kept] == [("component:b", "correctness")]


def test_git_inventory_reads_full_large_git_file_lists(tmp_path: Path) -> None:
    checkout = tmp_path / "repo"
    checkout.mkdir()
    subprocess.run(["git", "init"], cwd=checkout, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    total = 2200
    for index in range(total):
        path = checkout / "src" / f"very_long_module_path_{index:04d}_abcdefghijklmnopqrstuvwxyz.py"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"VALUE_{index} = {index}\n", encoding="utf-8")

    inventory = build_git_inventory(checkout, include_untracked=True)
    paths = {str(item.get("path") or "") for item in inventory.get("files", []) if isinstance(item, dict)}

    assert len(paths) == total
    assert "ijklmnopqrstuvwxyz.py" not in paths


def test_unit_coverage_does_not_count_blocked_baseline_review() -> None:
    coverage = build_unit_coverage(
        {},
        {},
        [{"unit_id": "component:1"}],
        [{"task": {"unit_id": "component:1", "focus": "correctness"}, "status": "blocked"}],
    )

    assert coverage["review_units"] == 1
    assert coverage["baseline_reviewed_units"] == 0


def test_collect_rejected_keeps_unsafe_confirmed_judge_visible() -> None:
    rejected = collect_rejected(
        [{"issue_id": "issue_1"}],
        [{"candidate_id": "issue_1"}],
        [{"candidate_id": "issue_1", "status": "confirmed", "safe_to_show_user": False, "reason": "not safe"}],
    )

    assert rejected == [{"candidate_id": "issue_1", "reason": "not safe"}]


def test_collect_confirmed_resolves_candidate_id_without_issue_id() -> None:
    confirmed = collect_confirmed(
        [{"candidate_id": "issue_1", "claim": "Confirmed candidate"}],
        [{"candidate_id": "issue_1", "worker": "/tmp/worker"}],
        [{"candidate_id": "issue_1", "status": "confirmed", "safe_to_show_user": True}],
    )

    assert confirmed[0]["candidate"]["claim"] == "Confirmed candidate"
    assert confirmed[0]["repro"]["worker"] == "/tmp/worker"


def test_collect_rejected_resolves_candidate_id_without_issue_id() -> None:
    rejected = collect_rejected(
        [{"candidate_id": "issue_1"}],
        [],
        [],
    )

    assert rejected == [{"candidate_id": "issue_1", "reason": "not confirmed by judge"}]


def test_graph_normalizer_repairs_live_quality_gate_failures(tmp_path: Path) -> None:
    checkout = tmp_path / "repo"
    checkout.mkdir()
    app = checkout / "app.py"
    app.write_text("def handle(value):\n    return value\n", encoding="utf-8")
    inventory = {
        "files": [
            {
                "path": "app.py",
                "scope": "analyze",
                "size_bytes": app.stat().st_size,
                "line_count": 2,
                "content_hash": "",
                "extension": ".py",
            }
        ]
    }
    graph = merge_graph_results(
        [
            {
                "task_id": "graph-map-0001",
                "shard_id": "shard-0001",
                "nodes": [
                    {
                        "id": "sym:handle",
                        "kind": "function",
                        "name": "handle",
                        "qualified_name": "handle",
                        "file": "app.py",
                        "span": {"start_line": 1, "end_line": 2},
                        "evidence": [],
                    }
                ],
                "edges": [
                    {
                        "from": "sym:handle",
                        "to": "sym:missing",
                        "type": "calls",
                        "evidence": [{"file": "app.py", "start_line": 2, "end_line": 2, "evidence_kind": "direct_syntax"}],
                    }
                ],
                "unresolved_refs": [],
                "coverage": {"assigned_files": ["app.py"], "mapped_files": ["app.py"]},
            }
        ]
    )
    graph["conflicts"] = [{"shard_id": "shard-0001", "missing_nodes": ["sym:other"]}]

    normalized = normalize_graph_for_inventory(graph, inventory, checkout)
    audit = audit_graph(normalized, inventory, checkout)
    node_ids = {str(node.get("id") or "") for node in normalized["nodes"]}

    assert "file:app.py" in node_ids
    assert not normalized["edges"]
    assert audit["dual_map_conflicts"] == 1
    assert audit["quality_gate_passed"] is True


def test_graph_audit_ignores_blank_inventory_paths(tmp_path: Path) -> None:
    checkout = tmp_path / "repo"
    checkout.mkdir()
    app = checkout / "app.py"
    app.write_text("print('ok')\n", encoding="utf-8")
    inventory = {
        "files": [
            {"path": "", "scope": "analyze"},
            {"path": "app.py", "scope": "analyze", "size_bytes": app.stat().st_size, "line_count": 1, "content_hash": "", "extension": ".py"},
        ]
    }
    graph = normalize_graph_for_inventory(
        merge_graph_results(
            [
                {
                    "task_id": "graph-map-0001",
                    "shard_id": "shard-0001",
                    "nodes": [],
                    "edges": [],
                    "unresolved_refs": [],
                    "coverage": {"assigned_files": ["app.py"], "mapped_files": ["app.py"]},
                }
            ]
        ),
        inventory,
        checkout,
    )

    audit = audit_graph(graph, inventory, checkout)

    assert audit["missing_file_nodes"] == []
    assert audit["quality_gate_passed"] is True


def test_graph_audit_repairs_critical_unresolved_source_files(tmp_path: Path) -> None:
    checkout = tmp_path / "repo"
    checkout.mkdir()
    worker = checkout / "worker.py"
    worker.write_text("def handle():\n    dispatch()\n", encoding="utf-8")
    inventory = {
        "files": [
            {"path": "worker.py", "scope": "analyze", "size_bytes": worker.stat().st_size, "line_count": 2, "content_hash": "", "extension": ".py"},
        ]
    }
    graph = normalize_graph_for_inventory(
        merge_graph_results(
            [
                {
                    "task_id": "graph-map-0001",
                    "shard_id": "shard-0001",
                    "nodes": [
                        {
                            "id": "sym:handle",
                            "kind": "function",
                            "name": "handle",
                            "qualified_name": "handle",
                            "file": "worker.py",
                            "span": {"start_line": 1, "end_line": 2},
                            "attributes": ["job-handler"],
                            "evidence": [{"file": "worker.py", "start_line": 1, "end_line": 2, "evidence_kind": "direct_syntax"}],
                        }
                    ],
                    "edges": [],
                    "unresolved_refs": [
                        {
                            "source_node": "sym:handle",
                            "reference_kind": "call",
                            "raw_reference": "dispatch",
                            "source_file": "worker.py",
                            "source_line": 2,
                            "reason": "Target is dynamically dispatched.",
                        }
                    ],
                    "coverage": {"assigned_files": ["worker.py"], "mapped_files": ["worker.py"]},
                }
            ]
        ),
        inventory,
        checkout,
    )

    audit = audit_graph(graph, inventory, checkout)

    assert audit["critical_unresolved"] == 1
    assert audit["quality_gate_passed"] is True
    assert any("worker.py" in repair.get("files", []) for repair in audit["repairs"])


def test_graph_audit_repair_tasks_include_all_missing_files(tmp_path: Path) -> None:
    checkout = tmp_path / "repo"
    checkout.mkdir()
    files = []
    for index in range(120):
        rel = f"pkg/file_{index:03d}.py"
        path = checkout / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("print('ok')\n", encoding="utf-8")
        files.append({"path": rel, "scope": "analyze", "size_bytes": path.stat().st_size, "line_count": 1, "content_hash": "", "extension": ".py"})

    audit = audit_graph({"nodes": [], "edges": [], "unresolved_refs": [], "coverage": {"assigned_files": [], "mapped_files": []}}, {"files": files}, checkout)

    assert audit["missing_mapped_file_count"] == 120
    assert len(audit["missing_mapped_files"]) == 100
    assert len(audit["repairs"][0]["files"]) == 120


def test_deterministic_graph_backfill_maps_files_missed_by_codex_mapper(tmp_path: Path) -> None:
    checkout = tmp_path / "repo"
    checkout.mkdir()
    app = checkout / "app.py"
    missed = checkout / "missed.py"
    app.write_text("def app():\n    return 1\n", encoding="utf-8")
    missed.write_text("def missed():\n    return app()\n", encoding="utf-8")
    inventory = {
        "files": [
            {"path": "app.py", "scope": "analyze", "size_bytes": app.stat().st_size, "line_count": 2, "content_hash": "", "extension": ".py"},
            {"path": "missed.py", "scope": "analyze", "size_bytes": missed.stat().st_size, "line_count": 2, "content_hash": "", "extension": ".py"},
        ]
    }
    config = ReviewConfig()
    config.graph.max_shard_files = 25
    shard_results = [
        {
            "task_id": "graph-map-0001",
            "shard_id": "shard-0001",
            "nodes": [],
            "edges": [],
            "unresolved_refs": [],
            "coverage": {"assigned_files": ["app.py", "missed.py"], "mapped_files": ["app.py"]},
            "status": "ok",
        }
    ]

    backfill = codereview_main._deterministic_graph_coverage_backfill(checkout, shard_results, inventory, config, start_index=2)
    graph = normalize_graph_for_inventory(merge_graph_results([*shard_results, *backfill]), inventory, checkout)
    audit = audit_graph(graph, inventory, checkout)

    assert [result["task_id"] for result in backfill] == ["graph-backfill-0002"]
    assert backfill[0]["coverage"]["mapped_files"] == ["missed.py"]
    assert audit["missing_mapped_files"] == []
    assert audit["quality_gate_passed"] is True


def test_graph_merge_ignores_mapped_coverage_from_blocked_shards(tmp_path: Path) -> None:
    checkout = tmp_path / "repo"
    checkout.mkdir()
    app = checkout / "app.py"
    app.write_text("def app():\n    return 1\n", encoding="utf-8")
    inventory = {
        "files": [
            {"path": "app.py", "scope": "analyze", "size_bytes": app.stat().st_size, "line_count": 2, "content_hash": "", "extension": ".py"},
        ]
    }

    graph = normalize_graph_for_inventory(
        merge_graph_results(
            [
                {
                    "task_id": "graph-map-0001",
                    "shard_id": "shard-0001",
                    "status": "blocked",
                    "blocked_reason": "codex graph mapper timed out",
                    "nodes": [
                        {
                            "id": "sym:app",
                            "kind": "function",
                            "name": "app",
                            "qualified_name": "app",
                            "file": "app.py",
                            "span": {"start_line": 1, "end_line": 2},
                            "evidence": [{"file": "app.py", "start_line": 1, "end_line": 2, "evidence_kind": "direct_syntax"}],
                        }
                    ],
                    "edges": [],
                    "unresolved_refs": [],
                    "coverage": {"assigned_files": ["app.py"], "mapped_files": ["app.py"]},
                    "warnings": [],
                }
            ]
        ),
        inventory,
        checkout,
    )
    audit = audit_graph(graph, inventory, checkout)

    assert graph["coverage"]["assigned_files"] == ["app.py"]
    assert graph["coverage"]["mapped_files"] == []
    assert "sym:app" not in {str(node.get("id") or "") for node in graph["nodes"]}
    assert any("ignored non-ok graph shard graph-map-0001" in warning for warning in graph["warnings"])
    assert audit["missing_mapped_files"] == ["app.py"]
    assert audit["quality_gate_passed"] is False


def test_dual_map_conflicts_ignore_blocked_shards() -> None:
    ok_result = {
        "task_id": "graph-map-0001",
        "shard_id": "shard-0001",
        "status": "ok",
        "nodes": [{"id": "sym:ok"}],
        "edges": [],
    }
    blocked_result = {
        "task_id": "graph-map-0002",
        "shard_id": "shard-0001",
        "status": "blocked",
        "blocked_reason": "mapper timed out",
        "nodes": [],
        "edges": [],
    }
    other_ok_result = {
        "task_id": "graph-map-0003",
        "shard_id": "shard-0001",
        "status": "ok",
        "nodes": [{"id": "sym:other"}],
        "edges": [],
    }

    assert merge_graph_results([ok_result, blocked_result])["conflicts"] == []
    assert merge_graph_results([ok_result, other_ok_result])["conflicts"]


def test_graph_repair_tasks_bypass_mapper_cache(tmp_path: Path) -> None:
    checkout = tmp_path / "repo"
    checkout.mkdir()
    ensure_project_files(checkout)
    app = checkout / "app.py"
    app.write_text("def app():\n    return 1\n", encoding="utf-8")
    inventory_by_path = {
        "app.py": {
            "path": "app.py",
            "scope": "analyze",
            "size_bytes": app.stat().st_size,
            "line_count": 2,
            "content_hash": "hash-app",
            "extension": ".py",
        }
    }
    config = ReviewConfig()
    config.graph.incremental = True
    config.graph.codex_mappers = False
    normal_task = {"task_id": "graph-map-0001", "shard_id": "shard-0001", "mapper_index": 1, "files": ["app.py"]}
    repair_task = {
        "task_id": "graph-repair-0002",
        "shard_id": "repair-0002",
        "mapper_index": 1,
        "files": ["app.py"],
        "reason": "graph audit repair",
    }
    cached_result = {
        "task_id": "graph-map-0001",
        "shard_id": "shard-0001",
        "mapper_index": 1,
        "files": ["app.py"],
        "nodes": [],
        "edges": [],
        "unresolved_refs": [],
        "coverage": {"assigned_files": ["app.py"], "mapped_files": ["app.py"]},
        "status": "ok",
    }

    graph_mapper_module._save_cached_task_result(checkout, normal_task, inventory_by_path, config, cached_result)

    assert graph_mapper_module._load_cached_task_result(checkout, normal_task, inventory_by_path, config)["cache_hit"] is True
    assert graph_mapper_module._load_cached_task_result(checkout, repair_task, inventory_by_path, config) is None


def test_deterministic_graph_config_does_not_disable_codex_enrichment() -> None:
    config = ReviewConfig()
    config.graph.codex_mappers = True

    baseline = codereview_main._deterministic_graph_config(config)

    assert baseline.graph.codex_mappers is False
    assert config.graph.codex_mappers is True


def test_graph_repair_tasks_chunk_missing_files_by_shard_limit() -> None:
    config = ReviewConfig()
    config.graph.max_shard_files = 25
    repairs = [{"type": "remap_files", "files": [f"pkg/file_{index:03d}.py" for index in range(60)], "reason": "missing"}]

    tasks = codereview_main._graph_repair_tasks(repairs, config=config, start_index=15)

    assert [task["task_id"] for task in tasks] == ["graph-repair-0015", "graph-repair-0016", "graph-repair-0017"]
    assert [len(task["files"]) for task in tasks] == [25, 25, 10]
    assert all(len(task["files"]) <= config.graph.max_shard_files for task in tasks)


def test_graph_repair_tasks_keep_conservative_file_limit_when_shards_are_large() -> None:
    config = ReviewConfig()
    config.graph.max_shard_files = 200
    repairs = [{"type": "remap_files", "files": [f"pkg/file_{index:03d}.py" for index in range(85)], "reason": "missing"}]

    tasks = codereview_main._graph_repair_tasks(repairs, config=config, start_index=15)

    assert [len(task["files"]) for task in tasks] == [25, 25, 25, 10]


def test_graph_quality_gate_failure_message_uses_full_missing_counts() -> None:
    message = codereview_main._graph_quality_gate_failure_message(
        {
            "quality_errors": ["not all analyzable files were mapped"],
            "missing_mapped_file_count": 185,
            "missing_mapped_files": ["sample.py"],
        }
    )

    assert "missing_mapped_files=185" in message


def test_graph_linker_requires_resolution_reason() -> None:
    assert _valid_link_result({"status": "resolved", "target": "sym:handle", "reason": "import binding matches"}, ["sym:handle"])
    assert not _valid_link_result({"status": "resolved", "target": "sym:handle"}, ["sym:handle"])


def test_graph_scheduler_caps_high_risk_double_mapping_to_target_shards() -> None:
    config = ReviewConfig()
    config.graph.target_shards = 12
    config.graph.double_map_high_risk = True
    census = {
        "high_risk_roots": [{"path": "."}],
        "shards": [
            {"shard_id": f"shard-{index + 1:04d}", "files": [f"app/file_{index + 1}.py"]}
            for index in range(7)
        ],
    }

    tasks = plan_graph_tasks(census, {}, config)

    assert len(tasks) == 12
    assert sum(1 for task in tasks if task["double_mapped"]) == 10


def test_codex_graph_mapper_coordinator_normalizes_empty_mapped_files(tmp_path: Path, monkeypatch: _MonkeyPatch) -> None:
    checkout = tmp_path / "repo"
    checkout.mkdir()
    ensure_project_files(checkout)
    (checkout / "one.py").write_text("def one():\n    return 1\n", encoding="utf-8")
    run = tmp_path / "run"
    config = ReviewConfig()
    config.graph.codex_mappers = True
    config.graph.mapper_subagent_limit = 6
    inventory = {"files": [{"path": "one.py", "scope": "analyze", "content_hash": "hash-one", "line_count": 2, "size_bytes": 24}]}
    tasks = [{"task_id": "graph-map-0001", "shard_id": "shard-0001", "mapper_index": 1, "files": ["one.py"], "double_mapped": False}]

    def fake_run_codex_turn(**kwargs):
        output = Path(kwargs["output_file"])
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(
                {
                    "results": [
                        {
                            "task_id": "graph-map-0001",
                            "shard_id": "shard-0001",
                            "mapper_index": 1,
                            "files": ["one.py"],
                            "status": "ok",
                            "nodes": [],
                            "edges": [],
                            "unresolved_refs": [],
                            "coverage": {"assigned_files": ["one.py"], "mapped_files": []},
                            "warnings": [],
                        }
                    ],
                    "warnings": [],
                }
            ),
            encoding="utf-8",
        )
        return ProcessResult(["codex", "app-server", "turn/start"], str(checkout), 0, "{}", "", 1)

    monkeypatch.setattr(graph_mapper_module, "run_codex_turn", fake_run_codex_turn)

    results = graph_mapper_module.map_graph_tasks(checkout, tasks, inventory, config, run=run)

    assert results[0]["coverage"]["mapped_files"] == ["one.py"]
    assert "normalized to assigned_files" in results[0]["warnings"][0]


def test_codex_graph_mapper_batches_coordinator_turns_and_reports_batch_progress(tmp_path: Path, monkeypatch: _MonkeyPatch) -> None:
    checkout = tmp_path / "repo"
    checkout.mkdir()
    ensure_project_files(checkout)
    (checkout / "one.py").write_text("def one():\n    return 1\n", encoding="utf-8")
    (checkout / "two.py").write_text("def two():\n    return one()\n", encoding="utf-8")
    run = tmp_path / "run"
    config = ReviewConfig()
    config.graph.codex_mappers = True
    config.graph.mapper_subagent_limit = 6
    config.graph.graph_timeout_seconds = 120
    config.graph.map_parallel = 2
    config.codex.reasoning_effort = "xhigh"
    inventory = {
        "files": [
            {"path": "one.py", "scope": "analyze", "content_hash": "hash-one", "line_count": 2, "size_bytes": 24},
            {"path": "two.py", "scope": "analyze", "content_hash": "hash-two", "line_count": 2, "size_bytes": 26},
        ]
    }
    tasks = [
        {
            "task_id": f"graph-map-{index + 1:04d}",
            "shard_id": f"shard-{index + 1:04d}",
            "mapper_index": 1,
            "files": ["one.py"] if index % 2 == 0 else ["two.py"],
            "double_mapped": False,
        }
        for index in range(8)
    ]
    calls = []
    barrier = threading.Barrier(2, timeout=2)

    def fake_run_codex_turn(**kwargs):
        calls.append(kwargs)
        barrier.wait()
        assert kwargs["output_schema"].name == "graph-shard-batch.schema.json"
        assert kwargs["config"].reasoning_effort == "medium"
        assert "Use at most mapper_subagent_limit subagents at one time" in kwargs["prompt"]
        task_payload = json.loads(Path(kwargs["output_file"]).parent.joinpath("task.json").read_text(encoding="utf-8"))
        batch_tasks = task_payload["jobs"]
        assert len(batch_tasks) <= 6
        output = Path(kwargs["output_file"])
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(
                {
                    "results": [
                        {
                            "task_id": task["task_id"],
                            "shard_id": task["shard_id"],
                            "mapper_index": task["mapper_index"],
                            "files": task["files"],
                            "status": "ok",
                            "nodes": [],
                            "edges": [],
                            "unresolved_refs": [],
                            "coverage": {"assigned_files": task["files"], "mapped_files": task["files"]},
                            "warnings": [],
                        }
                        for task in reversed(batch_tasks)
                    ],
                    "warnings": [],
                }
            ),
            encoding="utf-8",
        )
        return ProcessResult(["codex", "app-server", "turn/start"], str(checkout), 0, "{}", "", 1)

    monkeypatch.setattr(graph_mapper_module, "run_codex_turn", fake_run_codex_turn)
    events: list[dict] = []

    results = graph_mapper_module.map_graph_tasks(checkout, tasks, inventory, config, run=run, progress=events.append)

    assert len(calls) == 2
    assert {call["timeout_seconds"] for call in calls} == {120}
    assert [result["task_id"] for result in results] == [task["task_id"] for task in tasks]
    assert sorted(event["current"] for event in events) == list(range(1, 9))
    assert events[-1]["message"] == "Graph: mapping shards 8/8"


def test_codex_graph_mapper_coordinator_batches_keep_per_turn_timeout(tmp_path: Path, monkeypatch: _MonkeyPatch) -> None:
    checkout = tmp_path / "repo"
    checkout.mkdir()
    ensure_project_files(checkout)
    config = ReviewConfig()
    config.graph.codex_mappers = True
    config.graph.mapper_subagent_limit = 6
    config.graph.graph_timeout_seconds = 100
    config.codex.reasoning_effort = "xhigh"
    inventory = {"files": []}
    tasks = [
        {"task_id": f"graph-map-{index + 1:04d}", "shard_id": f"shard-{index + 1:04d}", "mapper_index": 1, "files": []}
        for index in range(8)
    ]
    observed = {}

    def fake_run_codex_turn(**kwargs):
        observed["timeout_seconds"] = kwargs["timeout_seconds"]
        observed["reasoning_effort"] = kwargs["config"].reasoning_effort
        Path(kwargs["output_file"]).write_text(
            json.dumps(
                {
                    "results": [
                        {
                            "task_id": task["task_id"],
                            "shard_id": task["shard_id"],
                            "mapper_index": 1,
                            "files": [],
                            "status": "ok",
                            "nodes": [],
                            "edges": [],
                            "unresolved_refs": [],
                            "coverage": {"assigned_files": [], "mapped_files": []},
                            "warnings": [],
                        }
                        for task in tasks
                    ],
                    "warnings": [],
                }
            ),
            encoding="utf-8",
        )
        return ProcessResult(["codex", "app-server", "turn/start"], str(checkout), 0, "{}", "", 1)

    monkeypatch.setattr(graph_mapper_module, "run_codex_turn", fake_run_codex_turn)

    graph_mapper_module.map_graph_tasks(checkout, tasks, inventory, config, run=tmp_path / "run")

    assert observed["timeout_seconds"] == 100
    assert observed["reasoning_effort"] == "medium"


def test_core_candidate_verifier_keeps_plan_reasoning_effort(tmp_path: Path, monkeypatch: _MonkeyPatch) -> None:
    checkout = tmp_path / "repo"
    checkout.mkdir()
    ensure_project_files(checkout)
    run = tmp_path / "run"
    config = ReviewConfig()
    config.codex.reasoning_effort = "xhigh"
    candidate = {
        "candidate_id": "issue_1",
        "claim": "Bug survives local checks",
        "graph_evidence": {"context_files": ["app.py"], "path_summary": ["app.py -> handle"]},
        "expected_behavior_source": ["test documents behavior"],
        "minimal_repro_idea": "call handle(None)",
        "actual_behavior_hypothesis": "raises AttributeError",
    }
    graph = {"nodes": [], "edges": [], "unresolved_refs": []}
    observed = {}

    def fake_run_codex_turn(**kwargs):
        observed["reasoning_effort"] = kwargs["config"].reasoning_effort
        output = Path(kwargs["output_file"])
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(
                {
                    "candidate_id": "issue_1",
                    "verdict": "reproducible",
                    "claim_survived": True,
                    "graph_path_valid": True,
                    "expected_behavior_supported": True,
                    "reproduction": {
                        "harness": "local",
                        "target_test": "",
                        "commands": [],
                        "expected_signal": "AttributeError",
                        "needs_network": False,
                        "estimated_scope": "targeted",
                    },
                    "rejection_reason": "",
                }
            ),
            encoding="utf-8",
        )
        return ProcessResult(["codex", "app-server", "turn/start"], str(checkout), 0, "{}", "", 1)

    monkeypatch.setattr(candidate_verifier_module, "run_codex_turn", fake_run_codex_turn)

    result = candidate_verifier_module.verify_candidate(candidate, graph, config, checkout, run)

    assert observed["reasoning_effort"] == "xhigh"
    assert result["verifier_source"] == "codex"


def test_candidate_verifier_parallel_blocks_one_exception_and_keeps_others(monkeypatch: _MonkeyPatch) -> None:
    config = ReviewConfig()
    candidates = [{"issue_id": "bad"}, {"issue_id": "ok"}]
    graph = {"unresolved_refs": [{"id": "ref1"}]}

    def fake_verify_candidate(candidate: dict, graph: dict, config: ReviewConfig, checkout: Path | None = None, run: Path | None = None) -> dict:
        del graph, config, checkout, run
        if candidate["issue_id"] == "bad":
            raise RuntimeError("verifier crashed")
        return {
            "candidate_id": "ok",
            "verdict": "reproducible",
            "claim_survived": True,
            "graph_path_valid": True,
            "expected_behavior_supported": True,
            "reproduction": {},
            "rejection_reason": "",
            "verifier_source": "test",
        }

    monkeypatch.setattr(candidate_verifier_module, "verify_candidate", fake_verify_candidate)

    results = candidate_verifier_module.run_candidate_verifiers_parallel(candidates, graph, config)

    assert [result["candidate_id"] for result in results] == ["bad", "ok"]
    assert results[0]["verdict"] == "blocked"
    assert results[0]["graph_unresolved_refs"] == 1
    assert "RuntimeError: verifier crashed" in results[0]["blocked_reason"]
    assert results[1]["verdict"] == "reproducible"


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


def test_finder_codex_failure_is_blocked_not_silent_empty(tmp_path: Path, monkeypatch: _MonkeyPatch) -> None:
    checkout = tmp_path / "repo"
    run = checkout / ".codereview" / "runs" / "run_1"
    checkout.mkdir(parents=True)
    ensure_project_files(checkout)
    write_review_units(
        run,
        [{"unit_id": "component:handle", "unit_type": "component", "review_pass": "baseline", "risk_tags": [], "context": {}}],
    )
    config = ReviewConfig()

    def fake_run_codex_turn(**kwargs):
        del kwargs
        return ProcessResult(
            command=["codex", "app-server", "turn/start"],
            cwd=str(checkout),
            returncode=42,
            stdout="finder stdout detail",
            stderr="finder failed",
            duration_ms=25,
        )

    monkeypatch.setattr(finder_runner_module, "run_codex_turn", fake_run_codex_turn)

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
                "command": ["codex", "app-server", "turn/start"],
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


def test_repro_worker_dir_replaces_symlink_without_touching_target(tmp_path: Path) -> None:
    checkout = tmp_path / "repo"
    checkout.mkdir()
    (checkout / "app.py").write_text("print('ok')\n", encoding="utf-8")
    target = tmp_path / "outside"
    target.mkdir()
    (target / "keep.txt").write_text("keep", encoding="utf-8")
    worker = tmp_path / "workers" / "issue_1"
    worker.parent.mkdir()
    worker.symlink_to(target, target_is_directory=True)

    create_worker_dir(checkout, worker, {"candidate_id": "issue_1"})

    assert worker.is_dir()
    assert not worker.is_symlink()
    assert (worker / "repo" / "app.py").is_file()
    assert (target / "keep.txt").read_text(encoding="utf-8") == "keep"


def test_repro_parallel_blocks_one_failed_worker_and_keeps_others(tmp_path: Path, monkeypatch: _MonkeyPatch) -> None:
    config = ReviewConfig()
    config.repro.enabled = True
    config.repro.max_workers = 2
    checkout = tmp_path / "repo"
    run = tmp_path / "run"
    checkout.mkdir()
    run.mkdir()
    candidates = [{"issue_id": "bad"}, {"issue_id": "ok"}]

    def fake_repro_worker(checkout: Path, run: Path, candidate: dict, config: ReviewConfig) -> dict:
        del checkout, run, config
        if candidate["issue_id"] == "bad":
            raise RuntimeError("setup exploded")
        return {"candidate_id": "ok", "status": "reproduced"}

    monkeypatch.setattr(repro_runner_module, "run_repro_worker", fake_repro_worker)

    results = repro_runner_module.run_repro_workers_parallel(checkout, run, candidates, config)

    assert [result["candidate_id"] for result in results] == ["bad", "ok"]
    assert results[0]["status"] == "blocked"
    assert "RuntimeError: setup exploded" in results[0]["blocked_reason"]
    assert results[0]["event_precheck"]["status"] == "passed"
    assert results[1]["status"] == "reproduced"


def test_worker_env_and_codex_base_env_are_isolated(tmp_path: Path) -> None:
    previous_api_key = os.environ.get("OPENAI_API_KEY")
    os.environ["OPENAI_API_KEY"] = "global-secret"
    worker = tmp_path / "worker"
    try:
        env = worker_env(worker)

        assert env["HOME"] == str(worker / "home")
        assert env["TMPDIR"] == str(worker / "tmp")
        assert env["XDG_CACHE_HOME"] == str(worker / "cache")

        config = ReviewConfig()
        config.codex.env = {"HOME": str(tmp_path / "home"), "CODEX_HOME": str(tmp_path / "home" / ".codex")}
        provider_env = base_env(tmp_path, config.codex)
        repro_env = worker_env(worker, config.codex)
        assert provider_env["HOME"] == str(tmp_path / "home")
        assert provider_env["CODEX_HOME"] == str(tmp_path / "home" / ".codex")
        assert "OPENAI_API_KEY" not in provider_env
        assert "OPENAI_API_KEY" not in repro_env
    finally:
        if previous_api_key is None:
            os.environ.pop("OPENAI_API_KEY", None)
        else:
            os.environ["OPENAI_API_KEY"] = previous_api_key


def test_codex_turn_dispatches_to_app_server(tmp_path: Path, monkeypatch: _MonkeyPatch) -> None:
    from codereview import codex_runner

    captured = {}
    schema = tmp_path / "schema.json"
    schema.write_text("{}", encoding="utf-8")

    def fake_app_server_turn(**kwargs):
        captured.update(kwargs)
        kwargs["output_file"].write_text('{"ok": true}', encoding="utf-8")
        return ProcessResult(["codex", "app-server", "turn/start"], str(kwargs["cd"]), 0, "{}", "", 1)

    monkeypatch.setattr(codex_runner, "run_codex_app_server_turn", fake_app_server_turn)

    result = codex_runner.run_codex_turn(
        cd=tmp_path,
        prompt="review",
        output_schema=schema,
        output_file=tmp_path / "result.json",
        sandbox="read-only",
        timeout_seconds=30,
        config=ReviewConfig().codex,
    )

    assert result.returncode == 0
    assert captured["prompt"] == "review"
    assert captured["sandbox"] == "read-only"
    assert captured["events_file"] is None


def test_codex_turn_app_server_writes_requested_output_path(tmp_path: Path, monkeypatch: _MonkeyPatch) -> None:
    from codereview import codex_runner

    checkout = tmp_path / "repo"
    checkout.mkdir()
    schema = checkout / "schema.json"
    schema.write_text("{}", encoding="utf-8")
    requested_output = tmp_path / "run" / "workers" / "result.json"
    captured = {}

    def fake_app_server_turn(**kwargs):
        captured["output_arg"] = kwargs["output_file"]
        kwargs["output_file"].parent.mkdir(parents=True, exist_ok=True)
        kwargs["output_file"].write_text('{"ok": true}', encoding="utf-8")
        return ProcessResult(["codex", "app-server", "turn/start"], str(kwargs["cd"]), 0, "{}", "", 1)

    monkeypatch.setattr(codex_runner, "run_codex_app_server_turn", fake_app_server_turn)

    result = codex_runner.run_codex_turn(
        cd=checkout,
        prompt="review",
        output_schema=schema,
        output_file=requested_output,
        sandbox="read-only",
        timeout_seconds=30,
        config=ReviewConfig().codex,
    )

    assert result.returncode == 0
    assert captured["output_arg"] == requested_output
    assert json.loads(requested_output.read_text(encoding="utf-8")) == {"ok": True}


def test_app_server_turn_writes_structured_output_and_turn_events(tmp_path: Path, monkeypatch: _MonkeyPatch) -> None:
    schema = tmp_path / "schema.json"
    schema.write_text(json.dumps({"type": "object", "properties": {"ok": {"type": "boolean"}}}), encoding="utf-8")
    output = tmp_path / "result.json"
    events = tmp_path / "events.jsonl"
    captured = {}

    class FakeAppServerClient:
        def run_turn(self, **kwargs):
            captured.update(kwargs)
            turn = app_server_runner.AppServerTurn(thread_id="thread_1")
            turn.assistant_messages.append('{"ok": true}')
            turn.events.append({"method": "turn/completed", "params": {"threadId": "thread_1"}})
            return turn

    monkeypatch.setattr(app_server_runner, "get_codex_app_server_client", lambda command, env, cwd: FakeAppServerClient())

    result = app_server_runner.run_codex_app_server_turn(
        cd=tmp_path,
        prompt="review",
        output_schema=schema,
        output_file=output,
        sandbox="workspace-write",
        timeout_seconds=30,
        config=ReviewConfig().codex,
        env={"CODEX_HOME": str(tmp_path / ".codex")},
        events_file=events,
    )

    assert result.returncode == 0
    assert json.loads(output.read_text(encoding="utf-8")) == {"ok": True}
    assert captured["cwd"] == tmp_path
    assert captured["output_schema"]["type"] == "object"
    assert captured["sandbox"] == "workspace-write"
    assert events.is_file()


def test_app_server_client_interrupts_running_turn_when_process_cancelled(tmp_path: Path) -> None:
    client = app_server_runner.CodexAppServerClient(command="codex", env={}, cwd=tmp_path)
    client.ensure_started = lambda: None
    requests = []
    cancel_event = threading.Event()

    def fake_request(method: str, params: dict | None = None, *, timeout_seconds: int = 30) -> dict:
        requests.append((method, params or {}, timeout_seconds))
        if method == "thread/start":
            return {"thread": {"id": "thread_cancel"}}
        if method == "turn/start":
            cancel_event.set()
            return {"turn": {"id": "turn_cancel"}}
        if method == "turn/interrupt":
            return {"ok": True}
        raise AssertionError(f"unexpected app-server request: {method}")

    client.request = fake_request
    set_process_cancel_event(cancel_event)
    try:
        try:
            client.run_turn(
                cwd=tmp_path,
                prompt="review",
                output_schema={"type": "object"},
                sandbox="read-only",
                model="gpt-5",
                reasoning_effort="medium",
                timeout_seconds=1,
            )
        except ProcessCancelled:
            pass
        else:
            raise AssertionError("expected app-server turn cancellation to propagate")
    finally:
        clear_process_cancel_event()

    assert any(method == "turn/interrupt" for method, _params, _timeout in requests)
    assert client._turns == {}


def test_app_server_turn_propagates_process_cancelled(tmp_path: Path, monkeypatch: _MonkeyPatch) -> None:
    schema = tmp_path / "schema.json"
    schema.write_text(json.dumps({"type": "object"}), encoding="utf-8")

    class FakeAppServerClient:
        def run_turn(self, **kwargs):
            raise ProcessCancelled("codex app-server turn cancelled")

        def close(self) -> None:
            pass

    monkeypatch.setattr(app_server_runner, "get_codex_app_server_client", lambda command, env, cwd: FakeAppServerClient())

    try:
        app_server_runner.run_codex_app_server_turn(
            cd=tmp_path,
            prompt="review",
            output_schema=schema,
            output_file=tmp_path / "result.json",
            sandbox="read-only",
            timeout_seconds=30,
            config=ReviewConfig().codex,
            env={"CODEX_HOME": str(tmp_path / ".codex")},
        )
    except ProcessCancelled:
        pass
    else:
        raise AssertionError("expected ProcessCancelled to propagate out of app-server turn")


def test_graphverified_progress_callback_cancellation_is_not_swallowed() -> None:
    cancel_event = threading.Event()
    cancel_event.set()
    set_process_cancel_event(cancel_event)

    def cancelled_progress(_payload: dict) -> None:
        raise RuntimeError("job was cancelled")

    try:
        try:
            codereview_main._emit_progress(cancelled_progress, "graph", "Graph: mapping shards 0/10")
        except ProcessCancelled:
            pass
        else:
            raise AssertionError("expected cancelled progress callback to stop the review")
    finally:
        clear_process_cancel_event()


def test_prepare_app_server_state_creates_codex_dirs_and_config(tmp_path: Path) -> None:
    env = {
        "HOME": str(tmp_path / "home"),
        "USERPROFILE": str(tmp_path / "home"),
        "CODEX_HOME": str(tmp_path / "home" / ".codex"),
        "CODEX_SQLITE_HOME": str(tmp_path / "home" / ".codex-sqlite"),
        "XDG_CONFIG_HOME": str(tmp_path / "home" / ".config"),
        "XDG_CACHE_HOME": str(tmp_path / "home" / ".cache"),
        "XDG_DATA_HOME": str(tmp_path / "home" / ".local" / "share"),
    }

    app_server_runner.prepare_app_server_state(env)

    for key in env:
        assert Path(env[key]).is_dir()
    config_path = Path(env["CODEX_HOME"]) / "config.toml"
    assert config_path.is_file()

    config_path.write_text("model = \"gpt-5\"\n", encoding="utf-8")
    app_server_runner.prepare_app_server_state(env)
    assert config_path.read_text(encoding="utf-8") == "model = \"gpt-5\"\n"


def test_app_server_state_lock_reports_busy_process_on_posix(tmp_path: Path) -> None:
    if not app_server_runner.app_server_state_lock_supported():
        raise unittest.SkipTest("POSIX fcntl locks are not available on this platform.")

    lock_path = tmp_path / ".codex" / ".pullwise-app-server.lock"
    first_lock = app_server_runner.AppServerStateLock(lock_path, timeout_seconds=0)
    first_lock.acquire()
    try:
        second_lock = app_server_runner.AppServerStateLock(lock_path, timeout_seconds=0)
        try:
            second_lock.acquire()
        except RuntimeError as exc:
            assert "deferred" in str(exc)
            assert "codex is running" in str(exc)
        else:
            second_lock.release()
            raise AssertionError("second app-server state lock unexpectedly succeeded")
    finally:
        first_lock.release()


def test_app_server_client_recycles_after_age_or_turn_limit(tmp_path: Path) -> None:
    client = app_server_runner.CodexAppServerClient(
        command="codex",
        env={"PULLWISE_CODEX_APP_SERVER_MAX_TURNS": "2", "PULLWISE_CODEX_APP_SERVER_MAX_AGE_SECONDS": "0"},
        cwd=tmp_path,
    )
    client.mark_turn_completed()
    assert client.should_recycle() is False
    client.mark_turn_completed()
    assert client.should_recycle() is True
    client._turns["active"] = app_server_runner.AppServerTurn(thread_id="active")
    assert client.should_recycle() is False

    aged = app_server_runner.CodexAppServerClient(
        command="codex",
        env={"PULLWISE_CODEX_APP_SERVER_MAX_TURNS": "0", "PULLWISE_CODEX_APP_SERVER_MAX_AGE_SECONDS": "10"},
        cwd=tmp_path,
    )
    aged._started_at = time.monotonic() - 11
    assert aged.should_recycle() is True


def test_codex_turn_uses_app_server_final_message(tmp_path: Path, monkeypatch: _MonkeyPatch) -> None:
    from codereview import codex_runner

    checkout = tmp_path / "repo"
    checkout.mkdir()
    schema = checkout / "schema.json"
    schema.write_text("{}", encoding="utf-8")
    requested_output = tmp_path / "run" / "workers" / "result.json"
    payload = {"languages": ["python"], "shards": []}

    def fake_app_server_turn(**kwargs):
        kwargs["output_file"].parent.mkdir(parents=True, exist_ok=True)
        kwargs["output_file"].write_text(json.dumps(payload), encoding="utf-8")
        return ProcessResult(["codex", "app-server", "turn/start"], str(kwargs["cd"]), 0, "{}", "", 1)

    monkeypatch.setattr(codex_runner, "run_codex_app_server_turn", fake_app_server_turn)

    result = codex_runner.run_codex_turn(
        cd=checkout,
        prompt="review",
        output_schema=schema,
        output_file=requested_output,
        sandbox="read-only",
        timeout_seconds=30,
        config=ReviewConfig().codex,
    )

    assert result.returncode == 0
    assert json.loads(requested_output.read_text(encoding="utf-8")) == payload


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

    def fake_run_codex_turn(**kwargs):
        del kwargs
        return ProcessResult(["codex", "app-server", "turn/start"], str(checkout), 1, "", stderr, 12)

    monkeypatch.setattr(census_module, "run_codex_turn", fake_run_codex_turn)
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


def test_codex_judge_cannot_promote_failed_local_gate(tmp_path: Path, monkeypatch: _MonkeyPatch) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    ensure_project_files(checkout)
    config = ReviewConfig()
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

    def fail_if_called(**kwargs):
        del kwargs
        raise AssertionError("judge should not call app-server when local gate rejects")

    monkeypatch.setattr(judge_runner_module, "run_codex_turn", fail_if_called)

    judge = run_judge(tmp_path / "run", {"candidate_id": "issue_1", "issue_id": "issue_1"}, repro, checkout, config)

    assert judge["status"] == "rejected"
    assert judge["safe_to_show_user"] is False
    assert "missing" in judge["reason"]


def test_codex_judge_confirmed_result_uses_local_verified_evidence(tmp_path: Path, monkeypatch: _MonkeyPatch) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    ensure_project_files(checkout)
    config = ReviewConfig()
    worker = tmp_path / "worker"
    (worker / "logs").mkdir(parents=True)
    (worker / "logs" / "repro.log").write_text("AttributeError", encoding="utf-8")
    repro = {
        "candidate_id": "issue_1",
        "worker": str(worker),
        "result": {
            "candidate_id": "issue_1",
            "status": "reproduced",
            "level": "L2",
            "summary": "AttributeError",
            "commands_run": [{"cmd": "python repro.py", "cwd": str(worker), "exit_code": 1, "log_path": "logs/repro.log"}],
            "files_written": ["logs/repro.log"],
            "proof": {"type": "runtime_output", "expected": "safe", "actual": "AttributeError", "log_excerpt": "AttributeError"},
            "graph_path_exercised": True,
            "why_valid": "ran command",
            "why_not_reproduced": "",
            "safety_notes": "",
        },
        "filesystem_violations": [],
    }
    calls = []

    def fake_run_codex_turn(**kwargs):
        calls.append(kwargs)
        payload = {
            "candidate_id": "issue_1",
            "status": "confirmed",
            "level": "L2",
            "safe_to_show_user": True,
            "reason": "agent confirmed",
            "evidence_summary": {"command": "fake command", "log_path": "logs/fake.log", "observable": "fake output"},
            "limitations": [],
        }
        kwargs["output_file"].parent.mkdir(parents=True, exist_ok=True)
        kwargs["output_file"].write_text(json.dumps(payload), encoding="utf-8")
        return ProcessResult(["codex", "app-server", "turn/start"], str(checkout), 0, "{}", "", 1)

    monkeypatch.setattr(judge_runner_module, "run_codex_turn", fake_run_codex_turn)

    judge = run_judge(tmp_path / "run", {"candidate_id": "issue_1", "issue_id": "issue_1", "graph_evidence": {"unit_id": "u1", "context_files": ["app.py"], "path_summary": ["handle"]}}, repro, checkout, config)

    assert len(calls) == 1
    assert judge["status"] == "confirmed"
    assert judge["evidence_summary"] == {"command": "python repro.py", "log_path": "logs/repro.log", "observable": "AttributeError"}


def test_judge_parallel_blocks_one_exception_and_keeps_others(tmp_path: Path, monkeypatch: _MonkeyPatch) -> None:
    config = ReviewConfig()
    config.repro.max_workers = 2
    checkout = tmp_path / "checkout"
    run = tmp_path / "run"
    checkout.mkdir()
    run.mkdir()
    candidates = [{"issue_id": "bad"}, {"issue_id": "ok"}]
    repro_results = [{"candidate_id": "bad"}, {"candidate_id": "ok"}]

    def fake_run_judge(run: Path, candidate: dict, repro: dict, checkout: Path, config: ReviewConfig) -> dict:
        del run, candidate, checkout, config
        if repro["candidate_id"] == "bad":
            raise RuntimeError("judge crashed")
        return {
            "candidate_id": "ok",
            "status": "rejected",
            "level": "L0",
            "safe_to_show_user": False,
            "reason": "not enough evidence",
            "evidence_summary": {"command": "", "log_path": "", "observable": ""},
            "limitations": [],
        }

    monkeypatch.setattr(judge_runner_module, "run_judge", fake_run_judge)

    results = judge_runner_module.run_judges_parallel(run, candidates, repro_results, checkout, config)

    assert [result["candidate_id"] for result in results] == ["bad", "ok"]
    assert results[0]["status"] == "blocked"
    assert results[0]["safe_to_show_user"] is False
    assert "RuntimeError: judge crashed" in results[0]["reason"]
    assert results[1]["status"] == "rejected"


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

    def fake_verifiers(candidates: list[dict], graph: dict, config: ReviewConfig, checkout: Path, run: Path) -> list[dict]:
        del graph, config, checkout, run
        return [
            {
                "candidate_id": str(candidate.get("issue_id") or candidate.get("candidate_id") or ""),
                "verdict": "reproducible",
                "claim_survived": True,
                "graph_path_valid": True,
                "expected_behavior_supported": True,
                "reproduction": {
                    "harness": "local",
                    "target_test": "",
                    "commands": [],
                    "expected_signal": "AttributeError",
                    "needs_network": False,
                    "estimated_scope": "targeted",
                },
                "rejection_reason": "",
                "verifier_source": "test",
            }
            for candidate in candidates
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
        patcher.setattr(codereview_main, "run_candidate_verifiers_parallel", fake_verifiers)
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


if __name__ == "__main__":
    unittest.main()
