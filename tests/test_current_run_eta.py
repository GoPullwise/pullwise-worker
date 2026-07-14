from __future__ import annotations

import unittest

from pullwise_worker.current_run_eta import CurrentRunEstimator


class CurrentRunEstimatorTest(unittest.TestCase):
    def test_cross_pool_dependencies_form_the_scan_critical_path(self) -> None:
        estimator = CurrentRunEstimator()
        estimator.set_resource_pool(
            'pipeline',
            configured_concurrency=1,
            effective_concurrency=1,
        )
        estimator.set_resource_pool(
            'reviewer',
            configured_concurrency=3,
            effective_concurrency=3,
        )
        estimator.add_work_unit(
            'reviewer-sample',
            kind='reviewer_turn',
            resource_pool='reviewer',
            state='completed',
            duration_seconds=10,
        )
        estimator.add_work_unit(
            'semantic-sample',
            kind='semantic_turn',
            resource_pool='pipeline',
            state='completed',
            duration_seconds=5,
        )
        estimator.add_work_unit(
            'bundle-ready',
            kind='barrier',
            resource_pool='pipeline',
            state='completed',
            duration_seconds=0,
        )
        reviewer_ids = []
        for index in range(3):
            unit_id = f'reviewer-{index}'
            reviewer_ids.append(unit_id)
            estimator.add_work_unit(
                unit_id,
                kind='reviewer_turn',
                resource_pool='reviewer',
                dependencies=('bundle-ready',),
                order=index,
            )
        estimator.add_work_unit(
            'final-report',
            kind='semantic_turn',
            resource_pool='pipeline',
            dependencies=tuple(reviewer_ids),
        )
        estimator.mark_plan_ready()

        estimate = estimator.snapshot()

        self.assertEqual(estimate['remainingSeconds'], 15)

    def test_active_unit_uses_residual_time_before_pending_lane_work(self) -> None:
        now = 100.0
        estimator = CurrentRunEstimator(monotonic_clock=lambda: now)
        estimator.set_resource_pool(
            'reviewer',
            configured_concurrency=3,
            effective_concurrency=3,
        )
        estimator.add_work_unit(
            'completed-sample',
            kind='reviewer_turn',
            resource_pool='reviewer',
            state='completed',
            duration_seconds=10,
        )
        estimator.add_work_unit(
            'active',
            kind='reviewer_turn',
            resource_pool='reviewer',
            order=0,
        )
        estimator.start_work_unit('active', started_at_monotonic=96.0)
        for index in range(3):
            estimator.add_work_unit(
                f'pending-{index}',
                kind='reviewer_turn',
                resource_pool='reviewer',
                order=index + 1,
            )
        estimator.mark_plan_ready()

        estimate = estimator.snapshot()

        self.assertEqual(estimate['remainingSeconds'], 16)
        self.assertEqual(estimate['parallel']['activeUnits'], 1)
        self.assertEqual(estimate['parallel']['pendingUnits'], 3)

    def test_reviewer_makespan_uses_runtime_concurrency(self) -> None:
        expected_by_concurrency = {1: 80, 3: 30, 8: 10}

        for concurrency, expected_seconds in expected_by_concurrency.items():
            with self.subTest(concurrency=concurrency):
                estimator = CurrentRunEstimator()
                estimator.set_resource_pool(
                    "reviewer",
                    configured_concurrency=concurrency,
                    effective_concurrency=concurrency,
                )
                estimator.add_work_unit(
                    "completed-sample",
                    kind="reviewer_turn",
                    resource_pool="reviewer",
                    state="completed",
                    duration_seconds=10,
                )
                for index in range(8):
                    estimator.add_work_unit(
                        f"pending-{index}",
                        kind="reviewer_turn",
                        resource_pool="reviewer",
                        order=index,
                    )
                estimator.mark_plan_ready()

                estimate = estimator.snapshot()

                self.assertEqual(estimate["state"], "available")
                self.assertEqual(estimate["remainingSeconds"], expected_seconds)
                self.assertEqual(
                    estimate["parallel"]["effectiveConcurrency"],
                    concurrency,
                )


if __name__ == "__main__":
    unittest.main()
