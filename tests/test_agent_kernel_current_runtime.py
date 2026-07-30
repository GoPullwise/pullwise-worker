from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from pullwise_worker.agent_kernel_current_contract import CURRENT_PACKAGE
from pullwise_worker.agent_kernel_current_database import CurrentAgentKernelDatabase
from pullwise_worker.agent_kernel_current_package import (
    canonical_current_document_bytes,
    verify_current_document_digest,
)
from pullwise_worker.agent_kernel_current_runtime import CurrentRuntimeRunner
from pullwise_worker.agent_kernel_source_state import SourceSelectionPolicy
from tests.agent_kernel_capture_fakes import FakeCaptureProvider
from tests.current_journal_support import CurrentJournalTestCase


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
    def _request() -> bytes:
        return canonical_current_document_bytes(
            {
                "schema_id": "agent-tool-request/v1",
                "idempotency_key": "current-runtime-r0",
                "tool_key": "internal.read_source",
                "tool_input": {"relative_path": "README.md"},
            }
        )

    def test_server_authority_bytes_drive_one_current_r0_with_exact_replay(
        self,
    ) -> None:
        authority = CurrentJournalTestCase.make_authority()
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

        first = runner.run_r0(authority.canonical_bytes, self._request())
        replay = runner.run_r0(authority.canonical_bytes, self._request())

        result = verify_current_document_digest(
            "r0-read-result/v1",
            json.loads(first),
        )
        self.assertEqual(first, replay)
        self.assertEqual(1, self.capture.begin_calls)
        self.assertEqual(len(self.payload), result["payload_ref"]["size_bytes"])
        self.assertEqual(
            hashlib.sha256(self.payload).hexdigest(),
            result["payload_ref"]["sha256"],
        )


if __name__ == "__main__":
    unittest.main()
