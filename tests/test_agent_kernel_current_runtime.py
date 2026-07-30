from __future__ import annotations

import base64
from contextlib import closing
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from pullwise_worker.agent_kernel_current_contract import CURRENT_PACKAGE
from pullwise_worker.agent_kernel_current_database import CurrentAgentKernelDatabase
from pullwise_worker.agent_kernel_current_objects import PublishedCurrentObject
from pullwise_worker.agent_kernel_current_package import (
    canonical_current_document_bytes,
    verify_current_document_digest,
)
from pullwise_worker.agent_kernel_current_runtime import CurrentRuntimeRunner
from pullwise_worker.agent_kernel_gateway import GatewayError
from pullwise_worker.agent_kernel_source_state import SourceSelectionPolicy
from tests.agent_kernel_capture_fakes import FakeCaptureProvider
from tests.current_journal_support import CurrentJournalTestCase
from tests.current_runtime_bootstrap_support import (
    bootstrap_bytes,
    golden_runtime_bootstrap,
)


class CurrentRuntimeRunnerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.scratch = tempfile.TemporaryDirectory(prefix="current-runtime-")
        self.root = Path(self.scratch.name)
        self.checkout = self.root / "checkout"
        self.checkout.mkdir()
        self.payload = b"current runtime tracer bullet\n"
        (self.checkout / "README.md").write_bytes(self.payload)
        self.base_revision = "a" * 40
        policy = SourceSelectionPolicy.pullwise_full_scan(
            root_identity="current-runtime-test"
        )
        self.capture = FakeCaptureProvider(
            self.checkout,
            policy,
            base_revision=self.base_revision,
        )
        self.database = CurrentAgentKernelDatabase.open(
            self.root / "worker",
            CURRENT_PACKAGE,
        )

    def tearDown(self) -> None:
        self.scratch.cleanup()

    @staticmethod
    def _published_object(content_ref: dict[str, object]) -> PublishedCurrentObject:
        digest = content_ref["sha256"]
        return PublishedCurrentObject(
            sha256=digest,
            size_bytes=content_ref["size_bytes"],
            relative_path=f"objects/{digest[:2]}/{digest}",
        )

    @staticmethod
    def _request() -> bytes:
        return canonical_current_document_bytes(
            {
                "schema_id": "agent-tool-request/v1",
                "idempotency_key": "current-runtime-r0",
                "tool_key": "internal.read_source",
                "tool_input": {"relative_path": "README.md"},
            }
        )

    def test_runtime_bootstrap_drives_one_current_r0_with_exact_replay(
        self,
    ) -> None:
        raw_bootstrap = bootstrap_bytes()
        timestamps = iter(
            (
                "2026-07-22T12:34:50.000Z",
                "2026-07-22T12:34:51.000Z",
                "2026-07-22T12:34:52.000Z",
            )
        )
        runner = CurrentRuntimeRunner(
            self.database,
            capture_provider=self.capture,
            base_revision=self.base_revision,
            max_read_bytes=1024,
            clock=lambda: next(timestamps),
        )

        first = runner.run_r0(raw_bootstrap, self._request())
        replay = runner.run_r0(raw_bootstrap, self._request())

        result = verify_current_document_digest(
            "r0-read-result/v1",
            json.loads(first),
        )
        self.assertEqual(first, replay)
        self.assertEqual(1, self.capture.begin_calls)
        payload_bytes = runner.journal.object_store.read_verified(
            self._published_object(result["payload_ref"])
        )
        payload_document = verify_current_document_digest(
            "r0-read-payload/v1",
            json.loads(payload_bytes),
        )
        source_bytes = runner.journal.object_store.read_verified(
            self._published_object(payload_document["content_ref"])
        )
        source_document = verify_current_document_digest(
            "source-content/v1",
            json.loads(source_bytes),
        )
        actual = base64.b64decode(source_document["data_base64"], validate=True)
        self.assertEqual(self.payload, actual)
        self.assertEqual(len(self.payload), source_document["size_bytes"])
        self.assertEqual(
            hashlib.sha256(self.payload).hexdigest(),
            source_document["byte_sha256"],
        )

    def test_noncanonical_bootstrap_is_rejected_before_any_runtime_write(
        self,
    ) -> None:
        raw_bootstrap = bootstrap_bytes()
        runner = CurrentRuntimeRunner(
            self.database,
            capture_provider=self.capture,
            base_revision=self.base_revision,
            max_read_bytes=1024,
        )

        with self.assertRaises(GatewayError) as raised:
            runner.run_r0(raw_bootstrap + b"\n", self._request())

        self.assertEqual("RUNTIME_BOOTSTRAP_NONCANONICAL", raised.exception.code)
        state_tables = (
            "authority_history",
            "authority_heads",
            "dispatch_budgets",
            "dispatch_intents",
            "content_objects",
            "content_bindings",
            "dispatch_settlements",
            "dispatch_abandonments",
        )
        with closing(self.database.connect()) as connection:
            counts = tuple(
                connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in state_tables
            )
        self.assertEqual((0,) * len(state_tables), counts)
        self.assertEqual(0, self.capture.begin_calls)

    def test_bare_authority_has_no_runtime_compatibility_path(self) -> None:
        authority = CurrentJournalTestCase.make_authority()
        runner = CurrentRuntimeRunner(
            self.database,
            capture_provider=self.capture,
            base_revision=self.base_revision,
            max_read_bytes=1024,
        )

        with self.assertRaises(GatewayError) as raised:
            runner.run_r0(authority.canonical_bytes, self._request())

        self.assertEqual("RUNTIME_BOOTSTRAP_INVALID", raised.exception.code)
        self.assertEqual(0, self.capture.begin_calls)


if __name__ == "__main__":
    unittest.main()
