from __future__ import annotations

from dataclasses import fields, FrozenInstanceError
import hashlib
import os
from pathlib import Path
import unittest
from unittest.mock import Mock

from pullwise_worker import _generated_agent_task_contract as generated_contract
from pullwise_worker.agent_kernel_current_package import (
    AgentClaimAbandonResponse,
    CURRENT_PACKAGE,
    CURRENT_TOOL_CATALOG,
    CurrentInvocationCodec,
    ServerAuthorityEnvelope,
    canonical_current_document_bytes,
    canonical_validated_current_bytes,
    seal_current_document,
    verify_current_document_digest,
    verify_current_package,
)
from pullwise_worker.agent_kernel_gateway import CheckedInvocation, GatewayError
from pullwise_worker.agent_kernel_r0_read import ReadSourceFileInput
from tests.current_package_support import (
    ABANDON_RESPONSE_SCHEMA_ID,
    AUTHORITY_SCHEMA_ID,
    abandonment_document,
    authority,
    authority_document,
    request_bytes,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIGURED_SERVER_ROOT = os.environ.get('PULLWISE_CURRENT_SERVER_ROOT')
SERVER_ROOT = (
    Path(CONFIGURED_SERVER_ROOT)
    if CONFIGURED_SERVER_ROOT is not None
    else REPO_ROOT.parent / 'pullwise-server'
)
WORKER_WRAPPER = REPO_ROOT / "pullwise_worker" / "_generated_agent_task_contract.py"
SERVER_WRAPPER = (
    SERVER_ROOT / "pullwise_server" / "_generated_agent_task_contract.py"
)
SERVER_BUNDLE = (
    SERVER_ROOT
    / "contracts"
    / "agent-first"
    / "current"
    / "published"
    / "contract-bundle.json"
)


INVOCATION_SCHEMA_ID = 'tool-invocation/v1'


class AgentKernelCurrentPackageTest(unittest.TestCase):
    def test_worker_pin_is_the_generated_package_tuple(self) -> None:
        self.assertEqual(generated_contract.PACKAGE_TUPLE, CURRENT_PACKAGE.as_tuple())
        self.assertIs(CURRENT_PACKAGE, verify_current_package())

    def test_worker_wrapper_is_exact_server_artifact(self) -> None:
        self.assertTrue(SERVER_WRAPPER.is_file(), "Server wrapper artifact is required")
        self.assertTrue(SERVER_BUNDLE.is_file(), "Server bundle artifact is required")
        self.assertEqual(SERVER_WRAPPER.read_bytes(), WORKER_WRAPPER.read_bytes())
        self.assertEqual(SERVER_BUNDLE.read_bytes(), generated_contract.bundle_bytes())

    def test_d22_release_gate_schemas_and_golden_fixtures_are_public(self) -> None:
        expected = {
            'benchmark-bundle': (
                'benchmark-bundle/v1',
                'benchmark_bundle_golden_current',
            ),
            'release-gate-policy': (
                'release-gate-policy/v1',
                'release_gate_policy_golden_bootstrap',
            ),
            'release-gate-report': (
                'release-gate-report/v1',
                'release_gate_report_golden_bootstrap_pass',
            ),
            'release-gate-attestation': (
                'release-gate-attestation/v1',
                'release_gate_attestation_golden_bootstrap_pass',
            ),
        }
        families = {
            family['family_id']: family
            for family in generated_contract.bundle()['families']
        }
        public_schema_ids = set(generated_contract.schema_ids())

        for family_id, (schema_id, fixture_id) in expected.items():
            with self.subTest(family_id=family_id):
                family = families[family_id]
                self.assertEqual(
                    [schema_id],
                    [schema['$id'] for schema in family['schemas']],
                )
                self.assertIn(schema_id, public_schema_ids)
                self.assertEqual(schema_id, generated_contract.schema(schema_id)['$id'])

                fixture = generated_contract.fixture(fixture_id)
                self.assertEqual(
                    fixture,
                    next(
                        item
                        for item in family['fixtures']
                        if item['fixture_id'] == fixture_id
                    ),
                )
                self.assertEqual('golden', fixture['fixture_class'])
                self.assertEqual(schema_id, fixture['schema_id'])
                self.assertEqual(
                    fixture['document'],
                    generated_contract.validate_document(
                        schema_id,
                        fixture['document'],
                    ),
                )

    def test_current_tool_catalog_is_package_owned_and_gateway_ready(self) -> None:
        descriptor = CURRENT_TOOL_CATALOG.resolve('internal.read_source')
        self.assertEqual('1.0.0', descriptor.tool_version)
        self.assertEqual('R0', descriptor.risk)
        self.assertEqual('source.read', descriptor.capability)
        self.assertEqual(
            (False, False, False, False),
            (
                descriptor.uses_command,
                descriptor.uses_network,
                descriptor.uses_secret,
                descriptor.requests_approval,
            ),
        )
        document = verify_current_document_digest(
            'tool-catalog/v1', CURRENT_TOOL_CATALOG.as_document()
        )
        self.assertEqual(document['catalog_digest'], CURRENT_TOOL_CATALOG.catalog_digest)
        self.assertEqual(
            canonical_validated_current_bytes('tool-catalog/v1', document),
            CURRENT_TOOL_CATALOG.canonical_bytes,
        )
        with self.assertRaisesRegex(GatewayError, 'TOOL_NOT_FOUND'):
            CURRENT_TOOL_CATALOG.resolve('unknown.tool')
        with self.assertRaises(FrozenInstanceError):
            setattr(CURRENT_TOOL_CATALOG, 'catalog_digest', '0' * 64)
        with self.assertRaises(TypeError):
            CURRENT_TOOL_CATALOG._descriptors['unknown.tool'] = descriptor

    def test_authority_projection_preserves_exact_canonical_bytes_and_grant(self) -> None:
        complete = authority_document()
        raw = canonical_validated_current_bytes(AUTHORITY_SCHEMA_ID, complete)

        parsed = ServerAuthorityEnvelope.from_canonical_bytes(raw)

        self.assertEqual(raw, parsed.canonical_bytes)
        self.assertEqual(CURRENT_PACKAGE, parsed.package)
        self.assertEqual(complete['grant']['grant_digest'], parsed.grant_digest)
        self.assertEqual(17, parsed.task_version)
        self.assertEqual(2, parsed.deletion_version)
        self.assertEqual(7, parsed.owner_epoch)
        self.assertEqual(11, parsed.native_epoch)
        self.assertEqual(13, parsed.transport_epoch)
        self.assertEqual(
            '2026-07-22T12:01:00.000Z',
            parsed.absolute_deadline_at,
        )
        self.assertEqual(1_000, parsed.terminalization_reserve_ms)
        self.assertEqual(
            parsed.grant.absolute_deadline_at,
            parsed.absolute_deadline_at,
        )
        self.assertEqual(
            parsed.grant.terminalization_reserve_ms,
            parsed.terminalization_reserve_ms,
        )
        self.assertEqual('ACTIVE', parsed.lifecycle)
        self.assertEqual('RUN', parsed.desired_state)

    def test_authority_rejects_noncanonical_bytes_and_untrusted_outer_binding(
        self,
    ) -> None:
        canonical = canonical_validated_current_bytes(
            AUTHORITY_SCHEMA_ID, authority_document()
        )
        with self.assertRaisesRegex(GatewayError, 'AUTHORITY_ENVELOPE_NONCANONICAL'):
            ServerAuthorityEnvelope.from_canonical_bytes(canonical + b'\n')

        mismatched = authority_document()
        mismatched['absolute_deadline_at'] = '2026-07-22T12:02:00.000Z'
        unsigned = {
            name: value
            for name, value in mismatched.items()
            if name != 'authority_digest'
        }
        mismatched['authority_digest'] = hashlib.sha256(
            b'pullwise:server-authority-envelope:v1\0'
            + canonical_current_document_bytes(unsigned)
        ).hexdigest()

        with self.assertRaises(GatewayError) as raised:
            ServerAuthorityEnvelope.from_canonical_bytes(
                canonical_current_document_bytes(mismatched)
            )
        self.assertEqual('AUTHORITY_ENVELOPE_INVALID', raised.exception.code)
        self.assertEqual(
            'AUTHORITY_INPUT_UNTRUSTED:$',
            raised.exception.detail,
        )

    def test_abandonment_response_preserves_successor_and_nested_old_grant(self) -> None:
        complete = abandonment_document()
        raw = canonical_validated_current_bytes(ABANDON_RESPONSE_SCHEMA_ID, complete)

        parsed = AgentClaimAbandonResponse.from_canonical_bytes(raw)

        self.assertEqual(raw, parsed.canonical_bytes)
        self.assertEqual(CURRENT_PACKAGE, parsed.package)
        self.assertEqual(complete['response_digest'], parsed.digest)
        self.assertEqual(
            complete['superseded_authority_digest'],
            parsed.superseded_authority_digest,
        )
        self.assertEqual(17, parsed.grant.task_version)
        self.assertEqual(18, parsed.task_version)
        self.assertEqual('authority_revoked', parsed.reason)
        self.assertEqual('FENCED', parsed.state)
        self.assertEqual(complete, parsed.as_document())

    def test_abandonment_response_rejects_noncanonical_or_wrong_successor_fence(self) -> None:
        canonical = canonical_validated_current_bytes(
            ABANDON_RESPONSE_SCHEMA_ID, abandonment_document()
        )
        with self.assertRaisesRegex(GatewayError, 'ABANDON_RESPONSE_NONCANONICAL'):
            AgentClaimAbandonResponse.from_canonical_bytes(canonical + b'\n')

        stale = abandonment_document()
        stale['task_version'] = 17
        with self.assertRaisesRegex(
            GatewayError, 'ABANDON_RESPONSE_GRANT_BINDING_MISMATCH'
        ):
            AgentClaimAbandonResponse.from_canonical_bytes(
                canonical_current_document_bytes(stale)
            )

        mismatched = abandonment_document()
        mismatched['owner_epoch'] = 8
        with self.assertRaisesRegex(
            GatewayError, 'ABANDON_RESPONSE_GRANT_BINDING_MISMATCH'
        ):
            AgentClaimAbandonResponse.from_canonical_bytes(
                canonical_current_document_bytes(mismatched)
            )

    def test_codec_derives_a_fully_bound_canonical_invocation(self) -> None:
        trusted = authority()

        checked = CurrentInvocationCodec(trusted).validate(request_bytes())

        self.assertIsInstance(checked, CheckedInvocation)
        self.assertEqual(trusted.authority_digest, checked.authority_digest)
        self.assertEqual(trusted.package.content_sha256, checked.package_content_sha256)
        self.assertEqual(trusted.package.root_sha256, checked.package_root_sha256)
        self.assertEqual(trusted.grant_digest, checked.grant_digest)
        for name in (
            'task_id',
            'attempt_id',
            'owner_id',
            'session_id',
            'lease_id',
            'task_version',
            'deletion_version',
            'owner_epoch',
            'native_epoch',
            'transport_epoch',
        ):
            self.assertEqual(getattr(trusted, name), getattr(checked, name), name)
        self.assertEqual(ReadSourceFileInput('README.md'), checked.tool_input)
        expected = seal_current_document(
            INVOCATION_SCHEMA_ID,
            {
                'schema_id': INVOCATION_SCHEMA_ID,
                'package': CURRENT_PACKAGE.as_document(),
                'authority_digest': trusted.authority_digest,
                'grant_digest': trusted.grant_digest,
                'task_id': trusted.task_id,
                'attempt_id': trusted.attempt_id,
                'session_id': trusted.session_id,
                'owner_id': trusted.owner_id,
                'lease_id': trusted.lease_id,
                'task_version': trusted.task_version,
                'deletion_version': trusted.deletion_version,
                'owner_epoch': trusted.owner_epoch,
                'native_epoch': trusted.native_epoch,
                'transport_epoch': trusted.transport_epoch,
                'idempotency_key': 'invoke:one',
                'tool_key': 'internal.read_source',
                'tool_input': {'relative_path': 'README.md'},
            },
        )
        self.assertEqual(expected['invocation_digest'], checked.invocation_digest)
        self.assertEqual(
            expected,
            verify_current_document_digest(INVOCATION_SCHEMA_ID, expected),
        )

    def test_agent_input_cannot_supply_authority_grant_or_observation_fields(self) -> None:
        forbidden = {
            'package': CURRENT_PACKAGE.as_document(),
            'grant_digest': '1' * 64,
            'authority_digest': '2' * 64,
            'task_id': 'task_' + '9' * 32,
            'owner_epoch': 999,
            'status': 'succeeded',
            'observation_id': 'obs_' + '3' * 32,
        }
        for name, value in forbidden.items():
            with self.subTest(name=name), self.assertRaisesRegex(
                GatewayError, 'AGENT_TOOL_REQUEST_INVALID'
            ):
                CurrentInvocationCodec(authority()).validate(
                    request_bytes(extra={name: value})
                )

        checked_fields = {field.name for field in fields(CheckedInvocation)}
        self.assertNotIn('status', checked_fields)
        self.assertNotIn('observation_id', checked_fields)

    def test_codec_requires_raw_request_to_be_canonical(self) -> None:
        with self.assertRaisesRegex(GatewayError, 'AGENT_TOOL_REQUEST_NONCANONICAL'):
            CurrentInvocationCodec(authority()).validate(b' ' + request_bytes())

    def test_historical_authority_resolver_precedes_current_authority(self) -> None:
        original = authority()
        successor = authority(
            task_version=18,
            owner_epoch=8,
            native_epoch=12,
            transport_epoch=14,
        )
        resolver = Mock(return_value=original)

        replay = CurrentInvocationCodec(successor, resolver).validate(request_bytes())

        resolver.assert_called_once_with(successor.task_id, 'invoke:one')
        self.assertEqual(original.authority_digest, replay.authority_digest)
        self.assertEqual(original.task_version, replay.task_version)

        missing = Mock(return_value=None)
        fresh = CurrentInvocationCodec(successor, missing).validate(
            request_bytes('invoke:new')
        )
        self.assertEqual(successor.authority_digest, fresh.authority_digest)

    def test_found_but_invalid_historical_authority_never_falls_back(self) -> None:
        resolver = Mock(return_value=object())
        with self.assertRaisesRegex(GatewayError, 'HISTORICAL_AUTHORITY_INVALID'):
            CurrentInvocationCodec(authority(), resolver).validate(request_bytes())


if __name__ == "__main__":
    unittest.main()
