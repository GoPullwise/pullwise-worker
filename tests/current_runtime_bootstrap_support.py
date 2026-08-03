from __future__ import annotations

from copy import deepcopy

from pullwise_worker import _generated_agent_task_contract as contract


def golden_runtime_bootstrap() -> dict[str, object]:
    package = contract.package_tuple()
    request = deepcopy(
        contract.fixture("task_control_golden_task_request")["document"]
    )
    policy = deepcopy(
        contract.fixture("task_control_golden_effective_policy")["document"]
    )
    ledger = deepcopy(contract.fixture("requirements_golden_ledger")["document"])
    accept_request = contract.seal_document(
        "agent-task-accept-request/v1",
        {
            "schema_id": "agent-task-accept-request/v1",
            "package": package,
            "idempotency_key": "accept:bootstrap:one",
            "outer_job_id": "job-1",
            "run_id": "run-1",
            "task_request": request,
            "effective_policy": policy,
            "requirement_ledger": ledger,
        },
    )
    accept_response = contract.seal_document(
        "agent-task-accept-response/v1",
        {
            "schema_id": "agent-task-accept-response/v1",
            "package": package,
            "task_id": request["task_id"],
            "task_version": 1,
            "deletion_version": 0,
            "lifecycle": "QUEUED",
            "desired_state": "RUN",
            "accepted_at": "2026-07-22T00:00:00.000Z",
        },
    )

    task = deepcopy(
        contract.fixture("task_control_golden_task_record")["document"]
    )
    attempt = deepcopy(
        contract.fixture("task_control_golden_attempt_record")["document"]
    )
    owner = deepcopy(
        contract.fixture("task_control_golden_task_owner")["document"]
    )
    lease_id = "lease_22222222222222222222222222222222"
    transport_attempt_id = (
        "transport_attempt_33333333333333333333333333333333"
    )
    task.update(
        {
            "lifecycle": "ACTIVE",
            "task_version": 2,
            "lease_id": lease_id,
            "transport_attempt_id": transport_attempt_id,
            "native_epoch": 1,
            "current_attempt_id": attempt["attempt_id"],
            "owner_epoch": 1,
            "ledger_head_digest": ledger["ledger_digest"],
            "updated_at": "2026-07-22T00:00:01.000Z",
        }
    )
    attempt["transport_binding"]["lease_id"] = lease_id
    attempt["transport_binding"]["transport_attempt_id"] = transport_attempt_id

    grant = deepcopy(
        contract.fixture("authority_golden_server_authority_envelope")[
            "document"
        ]["grant"]
    )
    grant.update(
        {
            "package": package,
            "task_id": task["task_id"],
            "attempt_id": attempt["attempt_id"],
            "session_id": owner["session_id"],
            "owner_id": owner["owner_id"],
            "lease_id": lease_id,
            "task_version": task["task_version"],
            "deletion_version": task["deletion_version"],
            "owner_epoch": owner["owner_epoch"],
            "native_epoch": attempt["native_epoch"],
            "transport_epoch": task["transport_epoch"],
            "policy_digest": policy["digest"],
            "absolute_deadline_at": task["absolute_deadline_at"],
            "terminalization_reserve_ms": task["terminalization_reserve_ms"],
        }
    )
    grant.pop("grant_digest")
    grant = contract.seal_document("agent-worker-grant/v1", grant)
    authority = contract.seal_document(
        "server-authority-envelope/v1",
        {
            "schema_id": "server-authority-envelope/v1",
            "package": package,
            "task_id": task["task_id"],
            "attempt_id": attempt["attempt_id"],
            "session_id": owner["session_id"],
            "owner_id": owner["owner_id"],
            "lease_id": lease_id,
            "task_version": task["task_version"],
            "deletion_version": task["deletion_version"],
            "owner_epoch": owner["owner_epoch"],
            "native_epoch": attempt["native_epoch"],
            "transport_epoch": task["transport_epoch"],
            "absolute_deadline_at": task["absolute_deadline_at"],
            "terminalization_reserve_ms": task["terminalization_reserve_ms"],
            "lifecycle": "ACTIVE",
            "desired_state": "RUN",
            "grant": grant,
        },
    )
    return contract.seal_document(
        "agent-task-runtime-bootstrap/v1",
        {
            "schema_id": "agent-task-runtime-bootstrap/v1",
            "package": package,
            "accept_request": accept_request,
            "accept_response": accept_response,
            "authority": authority,
            "transport_binding": {
                "outer_job_id": task["outer_job_id"],
                "run_id": task["run_id"],
                "lease_id": lease_id,
                "transport_attempt_id": transport_attempt_id,
                "transport_epoch": task["transport_epoch"],
            },
            "construction_roots": {
                "task_record": task,
                "attempt": attempt,
                "owner": owner,
            },
        },
    )


def changed_same_task_bootstrap() -> dict[str, object]:
    bootstrap = golden_runtime_bootstrap()
    request = deepcopy(bootstrap["accept_request"])
    request["idempotency_key"] = "accept:bootstrap:conflict"
    request.pop("accept_request_digest")
    bootstrap["accept_request"] = contract.seal_document(
        "agent-task-accept-request/v1", request
    )
    bootstrap.pop("bootstrap_digest")
    return contract.seal_document("agent-task-runtime-bootstrap/v1", bootstrap)


def bootstrap_bytes(document: dict[str, object] | None = None) -> bytes:
    return contract.canonical_validated_bytes(
        "agent-task-runtime-bootstrap/v1",
        document or golden_runtime_bootstrap(),
    )
