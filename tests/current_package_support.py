from __future__ import annotations

from pullwise_worker.agent_kernel_current_package import (
    CURRENT_PACKAGE,
    ServerAuthorityEnvelope,
    canonical_current_document_bytes,
    canonical_validated_current_bytes,
    seal_current_document,
)


GRANT_SCHEMA_ID = 'agent-worker-grant/v1'
AUTHORITY_SCHEMA_ID = 'server-authority-envelope/v1'
REQUEST_SCHEMA_ID = 'agent-tool-request/v1'
ABANDON_RESPONSE_SCHEMA_ID = 'agent-claim-abandon-response/v1'


def grant_document(
    *,
    package: dict[str, object] | None = None,
    task_version: int = 17,
    deletion_version: int = 2,
    owner_epoch: int = 7,
    native_epoch: int = 11,
    transport_epoch: int = 13,
) -> dict[str, object]:
    return seal_current_document(
        GRANT_SCHEMA_ID,
        {
            'schema_id': GRANT_SCHEMA_ID,
            'package': package or CURRENT_PACKAGE.as_document(),
            'grant_id': 'grant_' + '1' * 32,
            'task_id': 'task_' + '2' * 32,
            'attempt_id': 'attempt_' + '3' * 32,
            'session_id': 'sess_' + '4' * 32,
            'owner_id': 'owner_' + '5' * 32,
            'lease_id': 'lease_' + '6' * 32,
            'task_version': task_version,
            'deletion_version': deletion_version,
            'owner_epoch': owner_epoch,
            'native_epoch': native_epoch,
            'transport_epoch': transport_epoch,
            'policy_digest': '7' * 64,
            'absolute_deadline_at': '2026-07-22T12:01:00.000Z',
            'terminalization_reserve_ms': 1_000,
            'capability_ids': ['source.read'],
            'tool_keys': ['internal.read_source'],
            'elapsed_limit_ms': 60_000,
            'tool_call_limit': 3,
        },
    )


def authority_document(
    *,
    grant: dict[str, object] | None = None,
    task_version: int = 17,
    deletion_version: int = 2,
    owner_epoch: int = 7,
    native_epoch: int = 11,
    transport_epoch: int = 13,
) -> dict[str, object]:
    selected_grant = grant or grant_document(
        task_version=task_version,
        deletion_version=deletion_version,
        owner_epoch=owner_epoch,
        native_epoch=native_epoch,
        transport_epoch=transport_epoch,
    )
    return seal_current_document(
        AUTHORITY_SCHEMA_ID,
        {
            'schema_id': AUTHORITY_SCHEMA_ID,
            'package': CURRENT_PACKAGE.as_document(),
            'task_id': 'task_' + '2' * 32,
            'attempt_id': 'attempt_' + '3' * 32,
            'session_id': 'sess_' + '4' * 32,
            'owner_id': 'owner_' + '5' * 32,
            'lease_id': 'lease_' + '6' * 32,
            'task_version': task_version,
            'deletion_version': deletion_version,
            'owner_epoch': owner_epoch,
            'native_epoch': native_epoch,
            'transport_epoch': transport_epoch,
            'absolute_deadline_at': selected_grant['absolute_deadline_at'],
            'terminalization_reserve_ms': selected_grant[
                'terminalization_reserve_ms'
            ],
            'lifecycle': 'ACTIVE',
            'desired_state': 'RUN',
            'grant': selected_grant,
        },
    )


def authority(**changes: int) -> ServerAuthorityEnvelope:
    document = authority_document(**changes)
    raw = canonical_validated_current_bytes(AUTHORITY_SCHEMA_ID, document)
    return ServerAuthorityEnvelope.from_canonical_bytes(raw)


def request_bytes(
    idempotency_key: str = 'invoke:one',
    *,
    extra: dict[str, object] | None = None,
) -> bytes:
    document: dict[str, object] = {
        'schema_id': REQUEST_SCHEMA_ID,
        'idempotency_key': idempotency_key,
        'tool_key': 'internal.read_source',
        'tool_input': {'relative_path': 'README.md'},
    }
    document.update(extra or {})
    return canonical_current_document_bytes(document)


def abandonment_document(
    *,
    grant: dict[str, object] | None = None,
    task_version: int = 18,
    owner_epoch: int = 7,
) -> dict[str, object]:
    selected_grant = grant or grant_document(owner_epoch=owner_epoch)
    return seal_current_document(
        ABANDON_RESPONSE_SCHEMA_ID,
        {
            'schema_id': ABANDON_RESPONSE_SCHEMA_ID,
            'package': CURRENT_PACKAGE.as_document(),
            'superseded_authority_digest': '8' * 64,
            'grant': selected_grant,
            'task_id': 'task_' + '2' * 32,
            'attempt_id': 'attempt_' + '3' * 32,
            'session_id': 'sess_' + '4' * 32,
            'owner_id': 'owner_' + '5' * 32,
            'grant_id': selected_grant['grant_id'],
            'lease_id': 'lease_' + '6' * 32,
            'previous_task_version': selected_grant['task_version'],
            'task_version': task_version,
            'deletion_version': 2,
            'owner_epoch': owner_epoch,
            'native_epoch': 11,
            'transport_epoch': 13,
            'reason': 'authority_revoked',
            'state': 'FENCED',
            'abandoned_at': '2026-07-22T12:00:00Z',
        },
    )
