from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from pullwise_worker.agent_kernel_current_authority import (
    CurrentAuthorityProjection,
)
from pullwise_worker.agent_kernel_current_bootstrap import (
    CurrentRuntimeBootstrapConsumer,
)
from pullwise_worker.agent_kernel_current_database import (
    CurrentAgentKernelDatabase,
)
from pullwise_worker.agent_kernel_current_package import CURRENT_PACKAGE
from pullwise_worker.agent_kernel_current_terminalization import (
    CurrentTerminalizationError,
    CurrentTerminalizationStore,
)
from tests.current_runtime_bootstrap_support import bootstrap_bytes
from tests.current_s5_support import (
    blocked_task_result_bytes,
    fenced_authority,
    terminalization_inputs,
)


class CurrentTerminalizationStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.scratch = tempfile.TemporaryDirectory(prefix="current-s5-terminal-")
        self.database = CurrentAgentKernelDatabase.open(
            Path(self.scratch.name) / "current", CURRENT_PACKAGE
        )
        self.authority = CurrentRuntimeBootstrapConsumer(
            self.database
        ).ingest(bootstrap_bytes())
        self.inputs = terminalization_inputs(self.database, self.authority)

    def tearDown(self) -> None:
        self.scratch.cleanup()

    def result_bytes(self, prepared: object) -> bytes:
        return blocked_task_result_bytes(
            self.authority,
            prepared,
            self.inputs["documents"],
        )

    @staticmethod
    def prepare_inputs(
        inputs: dict[str, object],
    ) -> dict[str, object]:
        return {key: value for key, value in inputs.items() if key != "documents"}

    def test_prepare_and_freeze_are_mechanical_closed_and_exact_replay(self) -> None:
        store = CurrentTerminalizationStore(self.database)
        prepared = store.prepare(**self.prepare_inputs(self.inputs))

        self.assertEqual("BLOCKED", prepared.outcome)
        self.assertEqual("CAPABILITY_UNAVAILABLE", prepared.reason_code)
        result_bytes = self.result_bytes(prepared)
        frozen = store.freeze(prepared, result_bytes, self.inputs["objects"])
        replay = store.freeze(prepared, result_bytes, self.inputs["objects"])

        self.assertEqual(frozen, replay)
        self.assertEqual("BLOCKED", frozen.outcome)
        self.assertEqual(prepared.selector_input_digest, frozen.selector_input_digest)
        connection = self.database.connect()
        try:
            candidate = connection.execute(
                "SELECT outcome,reason_code,result_digest,task_result_core_sha256 "
                "FROM terminalization_candidates"
            ).fetchone()
            core = connection.execute(
                "SELECT content_schema_id,object_bytes FROM checkpoint_objects "
                "WHERE sha256=?",
                (candidate["task_result_core_sha256"],),
            ).fetchone()
        finally:
            connection.close()
        self.assertEqual(("BLOCKED", "CAPABILITY_UNAVAILABLE"), tuple(candidate[:2]))
        self.assertEqual("task-result-core/v1", core["content_schema_id"])

    def test_forged_outcome_missing_closure_and_budget_drift_fail_closed(self) -> None:
        store = CurrentTerminalizationStore(self.database)
        prepared = store.prepare(**self.prepare_inputs(self.inputs))
        raw = self.result_bytes(prepared)
        forged = raw.replace(b'"BLOCKED"', b'"FAILED"', 1)
        with self.assertRaises(CurrentTerminalizationError) as outcome:
            store.freeze(prepared, forged, self.inputs["objects"])
        self.assertIn(
            outcome.exception.code,
            {"TASK_RESULT_INVALID", "TASK_RESULT_CONTEXT_INVALID"},
        )

        missing = deepcopy(self.inputs)
        missing_objects = dict(missing["objects"])
        missing_objects.pop(next(iter(missing_objects)))
        missing["objects"] = missing_objects
        with self.assertRaises(CurrentTerminalizationError) as closure:
            store.prepare(**self.prepare_inputs(missing))
        self.assertEqual("EVIDENCE_OBJECT_MISSING", closure.exception.code)

        connection = self.database.connect()
        try:
            connection.execute(
                "UPDATE dispatch_budgets SET consumed_ms=1 WHERE task_id=?",
                (self.authority.task_id,),
            )
        finally:
            connection.close()
        with self.assertRaises(CurrentTerminalizationError) as budget_error:
            store.prepare(**self.prepare_inputs(self.inputs))
        self.assertEqual("BUDGET_CLOSURE_INVALID", budget_error.exception.code)

    def test_fence_and_every_freeze_stage_roll_back_candidate_and_objects(self) -> None:
        store = CurrentTerminalizationStore(self.database)
        prepared = store.prepare(**self.prepare_inputs(self.inputs))
        raw = self.result_bytes(prepared)
        CurrentAuthorityProjection(self.database).record_fenced(
            fenced_authority(self.authority),
            expected_previous_digest=self.authority.digest,
        )
        with self.assertRaises(CurrentTerminalizationError) as fenced:
            store.freeze(prepared, raw, self.inputs["objects"])
        self.assertEqual("AUTHORITY_FENCED", fenced.exception.code)

        for index, stage in enumerate(
            ("after_objects", "after_candidate", "before_commit"),
            start=1,
        ):
            with self.subTest(stage=stage):
                database = CurrentAgentKernelDatabase.open(
                    Path(self.scratch.name) / f"terminal-fault-{index}",
                    CURRENT_PACKAGE,
                )
                authority = CurrentRuntimeBootstrapConsumer(database).ingest(
                    bootstrap_bytes()
                )
                inputs = terminalization_inputs(database, authority)

                def inject(actual: str, selected: str = stage) -> None:
                    if actual == selected:
                        raise RuntimeError(f"injected:{selected}")

                store = CurrentTerminalizationStore(database, fault_hook=inject)
                prepared = store.prepare(**self.prepare_inputs(inputs))
                result = blocked_task_result_bytes(
                    authority, prepared, inputs["documents"]
                )
                with self.assertRaisesRegex(RuntimeError, "injected"):
                    store.freeze(prepared, result, inputs["objects"])
                connection = database.connect()
                try:
                    counts = (
                        connection.execute(
                            "SELECT COUNT(*) FROM terminalization_candidates"
                        ).fetchone()[0],
                        connection.execute(
                            "SELECT COUNT(*) FROM checkpoint_objects"
                        ).fetchone()[0],
                    )
                finally:
                    connection.close()
                self.assertEqual((0, 3), counts)


if __name__ == "__main__":
    unittest.main()
