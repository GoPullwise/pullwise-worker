#!/usr/bin/env python3
"""Build and smoke-test the Agent Kernel from an isolated wheel install."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import venv


ROOT = Path(__file__).resolve().parents[1]

PROBE = textwrap.dedent(
    """
    import json
    from pathlib import Path
    import sys
    import tempfile

    from pullwise_worker.agent_kernel_database import AgentKernelDatabase
    from pullwise_worker.agent_kernel_object_store import ObjectStore
    from pullwise_worker.agent_kernel_schema_registry import SchemaRegistry
    from pullwise_worker.agent_kernel_state import (
        TaskEvent,
        TaskEventKind,
        TransitionFacts,
    )
    from pullwise_worker.agent_kernel_task_store import TaskStore

    expected_schema_ids = {
        "actor/v1",
        "availability-ref/v1",
        "budget-entry/v1",
        "content-ref/v1",
        "effective-execution-policy/v1",
        "interaction-request/v1",
        "interaction-response/v1",
        "legacy-v1-task-mapping/v1",
        "requirement-entry/v1",
        "task-charter/v1",
        "task-record/v1",
        "task-request/v1",
        "waiver-event/v1",
    }
    expected_root = (
        Path(sys.prefix)
        / "share"
        / "pullwise-worker"
        / "contracts"
        / "agent-task"
        / "v1"
    )
    registry = SchemaRegistry()
    assert registry.root.resolve() == expected_root.resolve(), registry.root
    assert set(registry.schema_ids) == expected_schema_ids, registry.schema_ids
    fixture_names = {
        path.name for path in (registry.root / "fixtures").glob("*.json")
    }
    assert fixture_names == {
        "canonical-json.json",
        "schema-golden-control.json",
        "schema-golden.json",
    }, fixture_names

    with tempfile.TemporaryDirectory(prefix="agent-kernel-wheel-store-") as scratch:
        database = AgentKernelDatabase(Path(scratch) / "worker")
        database.initialize()
        object_store = ObjectStore(database)
        content_ref = object_store.put_bytes(
            b"installed-wheel",
            task_id="task_" + "1" * 32,
            artifact_id="art_" + "2" * 32,
            media_type="text/plain",
            content_schema_id="plain-text/v1",
            encoding="utf-8",
        )
        registry.validate("content-ref/v1", content_ref)
        assert object_store.read_verified(content_ref) == b"installed-wheel"

        task_record = None
        for fixture_name in ("schema-golden-control.json", "schema-golden.json"):
            fixture = json.loads(
                (registry.root / "fixtures" / fixture_name).read_text(encoding="utf-8")
            )
            for case in fixture["cases"]:
                if case["schema_id"] == "task-record/v1":
                    task_record = case["valid"]
        assert task_record is not None
        task_store = TaskStore(database)
        accepted = task_store.accept_task(
            task_record, idempotency_key="wheel-task-accepted"
        )
        claimed = task_store.apply_event(
            task_record["task_id"],
            expected_task_version=accepted.task.task_version,
            event=TaskEvent(
                kind=TaskEventKind.ATTEMPT_CLAIMED,
                idempotency_key="wheel-attempt-claimed",
                occurred_at="2026-07-18T08:00:00.000Z",
                attempt_id="attempt_" + "3" * 32,
                budget_reservation_id="wheel-budget-1",
            ),
            facts=TransitionFacts.permissive(),
        )
        assert claimed.task.lifecycle == "ACTIVE"
        assert claimed.task.task_version == 2
        assert task_store.count_attempts(task_record["task_id"]) == 1

    print(
        json.dumps(
            {
                "cas_roundtrip": "ok",
                "fixture_count": len(fixture_names),
                "schema_count": len(registry.schema_ids),
                "schema_root": str(registry.root),
                "task_transition": "QUEUED->ACTIVE",
            },
            sort_keys=True,
        )
    )
    """
)


def _run(command: list[str], *, cwd: Path) -> None:
    environment = dict(os.environ)
    environment["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    subprocess.run(command, cwd=cwd, env=environment, check=True)


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="pullwise-wheel-check-") as scratch:
        audit_root = Path(scratch)
        wheel_root = audit_root / "dist"
        wheel_root.mkdir()
        _run(
            [
                sys.executable,
                "-m",
                "pip",
                "wheel",
                "--no-build-isolation",
                "--no-deps",
                "--wheel-dir",
                str(wheel_root),
                str(ROOT),
            ],
            cwd=ROOT,
        )
        wheels = tuple(wheel_root.glob("pullwise_worker-*.whl"))
        if len(wheels) != 1:
            raise RuntimeError(f"expected one worker wheel, found {len(wheels)}")

        environment_root = audit_root / "venv"
        venv.EnvBuilder(with_pip=True).create(environment_root)
        python = environment_root / "bin" / "python"
        _run(
            [str(python), "-m", "pip", "install", "--no-deps", str(wheels[0])],
            cwd=audit_root,
        )
        _run([str(python), "-I", "-c", PROBE], cwd=audit_root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
