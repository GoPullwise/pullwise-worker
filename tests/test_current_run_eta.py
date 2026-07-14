from __future__ import annotations

import unittest

from pullwise_worker.current_run_eta import CurrentRunEstimator


class CurrentRunEstimatorTest(unittest.TestCase):
    def test_runtime_concurrency_changes_recompute_and_zero_is_unavailable(self) -> None:
        estimator = CurrentRunEstimator()
        estimator.set_resource_pool(
            'reviewer',
            configured_concurrency=3,
            effective_concurrency=3,
        )
        estimator.add_work_unit(
            'sample',
            kind='reviewer_turn',
            resource_pool='reviewer',
            state='completed',
            duration_seconds=10,
        )
        for index in range(6):
            estimator.add_work_unit(
                f'pending-{index}',
                kind='reviewer_turn',
                resource_pool='reviewer',
                order=index,
            )
        estimator.mark_plan_ready()

        self.assertEqual(estimator.snapshot()['remainingSeconds'], 20)

        estimator.set_resource_pool(
            'reviewer',
            configured_concurrency=3,
            effective_concurrency=1,
        )
        self.assertEqual(estimator.snapshot()['remainingSeconds'], 60)

        estimator.set_resource_pool(
            'reviewer',
            configured_concurrency=3,
            effective_concurrency=0,
        )
        unavailable = estimator.snapshot()
        self.assertEqual(unavailable['state'], 'unavailable')
        self.assertNotIn('remainingSeconds', unavailable)

    def test_weighted_units_use_current_run_time_per_weight(self) -> None:
        estimator = CurrentRunEstimator()
        estimator.set_resource_pool(
            'reviewer',
            configured_concurrency=2,
            effective_concurrency=2,
        )
        estimator.add_work_unit(
            'sample',
            kind='reviewer_turn',
            resource_pool='reviewer',
            weight=2,
            state='completed',
            duration_seconds=10,
        )
        estimator.add_work_unit(
            'small',
            kind='reviewer_turn',
            resource_pool='reviewer',
            weight=1,
        )
        estimator.add_work_unit(
            'large',
            kind='reviewer_turn',
            resource_pool='reviewer',
            weight=3,
        )
        estimator.mark_plan_ready()

        self.assertEqual(estimator.snapshot()['remainingSeconds'], 15)

    def test_overrun_keeps_positive_residual_and_deadline_caps_interval(self) -> None:
        now = 100.0
        estimator = CurrentRunEstimator(
            monotonic_clock=lambda: now,
            deadline_monotonic=112.0,
        )
        estimator.set_resource_pool(
            'reviewer',
            configured_concurrency=1,
            effective_concurrency=1,
        )
        estimator.add_work_unit(
            'sample',
            kind='reviewer_turn',
            resource_pool='reviewer',
            state='completed',
            duration_seconds=10,
        )
        estimator.add_work_unit(
            'straggler',
            kind='reviewer_turn',
            resource_pool='reviewer',
        )
        estimator.start_work_unit('straggler', started_at_monotonic=85.0)
        estimator.add_work_unit(
            'pending',
            kind='reviewer_turn',
            resource_pool='reviewer',
        )
        estimator.mark_plan_ready()

        estimate = estimator.snapshot()

        self.assertEqual(estimate['remainingSeconds'], 12)
        self.assertEqual(estimate['upperSeconds'], 12)
        self.assertEqual(estimate['confidence'], 'low')

    def test_missing_samples_estimates_then_terminal_state_clears_eta(self) -> None:
        estimator = CurrentRunEstimator()
        estimator.set_resource_pool(
            'reviewer',
            configured_concurrency=8,
            effective_concurrency=8,
        )
        estimator.add_work_unit(
            'pending',
            kind='reviewer_turn',
            resource_pool='reviewer',
        )

        self.assertEqual(estimator.snapshot()['state'], 'estimating')

        estimator.mark_plan_ready()
        self.assertEqual(estimator.snapshot()['state'], 'estimating')

        estimator.mark_terminal()
        self.assertIsNone(estimator.snapshot())

    def test_concurrency_above_remaining_unit_count_does_not_reduce_service_time(self) -> None:
        estimator = CurrentRunEstimator()
        estimator.set_resource_pool(
            'reviewer',
            configured_concurrency=8,
            effective_concurrency=8,
        )
        estimator.add_work_unit(
            'sample',
            kind='reviewer_turn',
            resource_pool='reviewer',
            state='completed',
            duration_seconds=10,
        )
        for index in range(3):
            estimator.add_work_unit(
                f'pending-{index}',
                kind='reviewer_turn',
                resource_pool='reviewer',
            )
        estimator.mark_plan_ready()

        self.assertEqual(estimator.snapshot()['remainingSeconds'], 10)

    def test_duplicate_completion_does_not_rewrite_the_duration_sample(self) -> None:
        estimator = CurrentRunEstimator(monotonic_clock=lambda: 110.0)
        estimator.set_resource_pool(
            'reviewer',
            configured_concurrency=1,
            effective_concurrency=1,
        )
        estimator.add_work_unit(
            'completed',
            kind='reviewer_turn',
            resource_pool='reviewer',
        )
        estimator.start_work_unit('completed', started_at_monotonic=90.0)
        estimator.finish_work_unit('completed', completed_at_monotonic=100.0)
        estimator.finish_work_unit('completed', completed_at_monotonic=110.0)
        estimator.add_work_unit(
            'pending',
            kind='reviewer_turn',
            resource_pool='reviewer',
        )
        estimator.mark_plan_ready()

        estimate = estimator.snapshot()

        self.assertEqual(estimate['remainingSeconds'], 10)

    def test_retry_attempt_is_added_as_new_work(self) -> None:
        now = 100.0
        estimator = CurrentRunEstimator(monotonic_clock=lambda: now)
        estimator.set_resource_pool(
            'reviewer',
            configured_concurrency=1,
            effective_concurrency=1,
        )
        estimator.add_work_unit(
            'attempt-1',
            kind='reviewer_turn',
            resource_pool='reviewer',
        )
        estimator.start_work_unit('attempt-1', started_at_monotonic=90.0)
        estimator.finish_work_unit(
            'attempt-1',
            completed_at_monotonic=100.0,
            state='failed',
        )
        estimator.add_work_unit(
            'attempt-2',
            kind='reviewer_turn',
            resource_pool='reviewer',
            dependencies=('attempt-1',),
            state='retrying',
        )
        estimator.mark_plan_ready()

        estimate = estimator.snapshot()

        self.assertEqual(estimate['remainingSeconds'], 10)
        self.assertEqual(estimate['parallel']['retryingUnits'], 1)

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
