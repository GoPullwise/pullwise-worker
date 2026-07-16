from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from pullwise_worker import review_worker_v1
from pullwise_worker.review_worker_v1 import (
    MAX_BUNDLE_ESTIMATED_TOKENS,
    ReviewWorkerV1,
    SEMANTIC_PHASES,
    materialize_agent_bundle_plan,
    pack_bundles,
    phase_prompt,
    prepare_bundle_planning_input,
    read_json,
    write_json,
)


def _inventory_item(path: str, *, estimated_tokens: int = 10) -> dict[str, object]:
    return {
        "path": path,
        "is_source_like": True,
        "is_binary": False,
        "is_generated_candidate": False,
        "risk_hints": [],
        "estimated_tokens": estimated_tokens,
        "line_count": 1,
    }


def _job(*, max_bundles: int = 24, max_reviewer_assignments: int = 48) -> dict[str, object]:
    return {
        "model_profile": {
            "default_model": "gpt-5.5",
            "core_effort": "high",
            "non_core_effort": "medium",
        },
        "review_request": {
            "policy": {
                "allow_source_modification": False,
                "allow_dependency_install": False,
                "allow_network": False,
                "helper_scripts_standard_library_only": True,
                "turn_timeout_seconds": 60,
                "reviewer_concurrency": 2,
                "max_bundles": max_bundles,
                "max_reviewer_assignments": max_reviewer_assignments,
            },
            "budget": {"max_wall_time_seconds": 600},
        },
        "repositoryLimits": {
            "maxFiles": 100,
            "maxBytes": 1024 * 1024,
        },
    }


class AgenticBundlePlanningTest(unittest.TestCase):
    def _run_dir(self, root: Path) -> tuple[Path, Path]:
        repo_dir = root / "repo"
        run_dir = repo_dir / ".codex-review" / "runs" / "run_1"
        run_dir.mkdir(parents=True)
        return repo_dir, run_dir

    def test_bundle_planning_is_a_semantic_grouping_phase(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            _repo_dir, run_dir = self._run_dir(Path(tmp_dir))
            prompt = phase_prompt("bundle_planning", run_dir)

        self.assertIn("bundle_planning", SEMANTIC_PHASES)
        self.assertIn("bundle-planning-input.json", prompt)
        self.assertIn("bundle-grouping.json", prompt)
        self.assertIn("bundle-grouping/v1", prompt)
        self.assertIn("exactly once", prompt)
        self.assertIn("max_bundles", prompt)
        self.assertIn("max_reviewer_assignments", prompt)
        self.assertIn("P0=3", prompt)
        self.assertIn("P1=2", prompt)
        self.assertIn("P2=1", prompt)
        self.assertIn("Worker will not merge", prompt)
        self.assertIn("stable lowercase group_id", prompt)
        self.assertIn("non-empty title", prompt)
        self.assertIn("non-empty grouping_reasons", prompt)
        self.assertIn("Do not assign reviewers", prompt)

    def test_production_module_has_no_mechanical_bundle_planner(self) -> None:
        self.assertFalse(hasattr(review_worker_v1, "bundle_plan_payload"))

    def test_bundle_planning_input_exposes_server_resource_limits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            _repo_dir, run_dir = self._run_dir(Path(tmp_dir))
            write_json(
                run_dir / "inventory.json",
                {
                    "schema_version": "inventory/v1",
                    "files": [_inventory_item("src/app.py")],
                },
            )
            write_json(
                run_dir / "risk-routing.json",
                {
                    "schema_version": "risk-routing/v1",
                    "routes": [{"path": "src/app.py", "tier": "P1"}],
                },
            )

            planning_input = prepare_bundle_planning_input(
                run_dir,
                _job(max_bundles=7, max_reviewer_assignments=13),
            )

        self.assertEqual(planning_input["constraints"]["max_bundles"], 7)
        self.assertEqual(
            planning_input["constraints"]["max_reviewer_assignments"],
            13,
        )
        self.assertEqual(
            planning_input["constraints"][
                "reviewer_assignments_per_bundle_by_tier"
            ],
            {"P0": 3, "P1": 2, "P2": 1},
        )
        self.assertNotIn(
            "worker_may_coalesce_same_tier_groups",
            planning_input["constraints"],
        )
        self.assertNotIn("component_hint", planning_input["items"][0])

    def test_worker_preserves_agent_owned_semantic_bundle_boundaries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo_dir, run_dir = self._run_dir(Path(tmp_dir))
            paths = ["src/users.py", "src/orders.py"]
            for path in paths:
                source = repo_dir / path
                source.parent.mkdir(parents=True, exist_ok=True)
                source.write_text(f"# {path}\n", encoding="utf-8")
            write_json(
                run_dir / "inventory.json",
                {
                    "schema_version": "inventory/v1",
                    "files": [_inventory_item(path) for path in paths],
                },
            )
            write_json(
                run_dir / "risk-routing.json",
                {
                    "schema_version": "risk-routing/v1",
                    "routes": [{"path": path, "tier": "P1"} for path in paths],
                },
            )
            write_json(
                run_dir / "bundle-grouping.json",
                {
                    "schema_version": "bundle-grouping/v1",
                    "run_id": "run_1",
                    "groups": [
                        {
                            "group_id": "users",
                            "tier": "P1",
                            "title": "Users",
                            "paths": [paths[0]],
                            "grouping_reasons": ["user lifecycle"],
                        },
                        {
                            "group_id": "orders",
                            "tier": "P1",
                            "title": "Orders",
                            "paths": [paths[1]],
                            "grouping_reasons": ["order lifecycle"],
                        },
                    ],
                },
            )

            plan = materialize_agent_bundle_plan(run_dir, _job())

        self.assertEqual(len(plan["bundles"]), 2)
        self.assertEqual(
            [bundle["paths"] for bundle in plan["bundles"]],
            [[paths[0]], [paths[1]]],
        )
        self.assertEqual(
            [bundle["semantic_group_id"] for bundle in plan["bundles"]],
            ["users", "orders"],
        )
        self.assertEqual(plan["reviewer_assignment_count"], 4)

    def test_post_split_bundle_and_assignment_caps_fail_without_truncation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo_dir, run_dir = self._run_dir(Path(tmp_dir))
            path = "src/large.py"
            source = repo_dir / path
            source.parent.mkdir(parents=True)
            source.write_text(
                "\n".join(f"line_{index} = '{'x' * 100}'" for index in range(1500)),
                encoding="utf-8",
            )
            write_json(
                run_dir / "inventory.json",
                {
                    "schema_version": "inventory/v1",
                    "files": [
                        {
                            **_inventory_item(
                                path,
                                estimated_tokens=MAX_BUNDLE_ESTIMATED_TOKENS * 3,
                            ),
                            "line_count": 1500,
                        }
                    ],
                },
            )
            write_json(
                run_dir / "risk-routing.json",
                {
                    "schema_version": "risk-routing/v1",
                    "routes": [{"path": path, "tier": "P0"}],
                },
            )
            write_json(
                run_dir / "bundle-grouping.json",
                {
                    "schema_version": "bundle-grouping/v1",
                    "run_id": "run_1",
                    "groups": [
                        {
                            "group_id": "large-critical-module",
                            "tier": "P0",
                            "title": "Large critical module",
                            "paths": [path],
                            "grouping_reasons": ["single cohesive module"],
                        }
                    ],
                },
            )

            with self.assertRaisesRegex(
                RuntimeError,
                "REVIEW_PLAN_LIMIT_EXCEEDED.*bundles.*1",
            ):
                materialize_agent_bundle_plan(
                    run_dir,
                    _job(max_bundles=1, max_reviewer_assignments=3),
                )

            planning_input = read_json(run_dir / "bundle-planning-input.json")

        self.assertEqual(
            [item["path"] for item in planning_input["items"]],
            [path],
        )

    def test_semantic_turn_receives_worker_owned_planning_input(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            repo_dir, run_dir = self._run_dir(root)
            path = "src/app.py"
            write_json(
                run_dir / "run-state.json",
                {"thread_id": "root-thread"},
            )
            write_json(
                run_dir / "inventory.json",
                {
                    "schema_version": "inventory/v1",
                    "files": [_inventory_item(path)],
                },
            )
            write_json(
                run_dir / "risk-routing.json",
                {
                    "schema_version": "risk-routing/v1",
                    "routes": [{"path": path, "tier": "P1"}],
                },
            )
            observed: dict[str, object] = {}

            class FakeCodexClient:
                def run_turn(self, **kwargs: object) -> None:
                    observed.update(kwargs)
                    planning_input = (
                        run_dir / "bundle-planning-input.json"
                    ).read_text(encoding="utf-8")
                    self_test.assertIn(path, planning_input)
                    write_json(
                        Path(str(kwargs["turn_cwd"])) / "bundle-grouping.json",
                        {
                            "schema_version": "bundle-grouping/v1",
                            "run_id": "run_1",
                            "groups": [
                                {
                                    "group_id": "application-entrypoint",
                                    "tier": "P1",
                                    "title": "Application entrypoint",
                                    "paths": [path],
                                    "grouping_reasons": ["entrypoint cohesion"],
                                }
                            ],
                        },
                    )

            self_test = self
            worker = ReviewWorkerV1(
                SimpleNamespace(worker_id="wk_1", service_home=str(root)),
                client=object(),
            )
            job = {
                "job_id": "job_1",
                "model_profile": {
                    "default_model": "gpt-5.5",
                    "core_effort": "high",
                    "non_core_effort": "medium",
                },
                "review_request": {
                    "policy": {
                        "allow_source_modification": False,
                        "allow_dependency_install": False,
                        "allow_network": False,
                        "helper_scripts_standard_library_only": True,
                        "turn_timeout_seconds": 60,
                        "max_bundles": 24,
                        "max_reviewer_assignments": 48,
                        "reviewer_concurrency": 2,
                    },
                    "budget": {"max_wall_time_seconds": 600},
                },
                "repositoryLimits": {
                    "maxFiles": 100,
                    "maxBytes": 1024 * 1024,
                },
            }

            worker.run_semantic_phase(
                FakeCodexClient(),
                repo_dir,
                run_dir,
                job,
                "bundle_planning",
            )
            plan = materialize_agent_bundle_plan(run_dir, job)

        self.assertEqual(observed["thread_id"], "root-thread")
        self.assertEqual(observed["effort"], "medium")
        self.assertIn("bundle-grouping.json", str(observed["prompt"]))
        self.assertEqual(plan["semantic_group_count"], 1)

    def test_agent_grouping_is_compiled_into_worker_owned_bundle_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo_dir, run_dir = self._run_dir(Path(tmp_dir))
            paths = ["app/users/service.py", "tests/users/test_service.py"]
            for path in paths:
                source = repo_dir / path
                source.parent.mkdir(parents=True, exist_ok=True)
                source.write_text(f"# {path}\n", encoding="utf-8")
            write_json(
                run_dir / "inventory.json",
                {
                    "schema_version": "inventory/v1",
                    "files": [_inventory_item(path) for path in paths],
                },
            )
            write_json(
                run_dir / "risk-routing.json",
                {
                    "schema_version": "risk-routing/v1",
                    "routes": [
                        {"path": path, "tier": "P1", "reasons": ["user lifecycle"]}
                        for path in paths
                    ],
                },
            )
            planning_input = prepare_bundle_planning_input(run_dir, _job())
            write_json(
                run_dir / "bundle-grouping.json",
                {
                    "schema_version": "bundle-grouping/v1",
                    "run_id": "run_1",
                    "groups": [
                        {
                            "group_id": "users-lifecycle",
                            "tier": "P1",
                            "title": "Users lifecycle",
                            "paths": paths,
                            "grouping_reasons": [
                                "implementation and its behavioral test"
                            ],
                        }
                    ],
                },
            )

            plan = materialize_agent_bundle_plan(run_dir, _job())
            pack_bundles(repo_dir, run_dir)

        self.assertEqual(
            [(item["path"], item["tier"]) for item in planning_input["items"]],
            [(path, "P1") for path in paths],
        )
        self.assertEqual(len(plan["bundles"]), 1)
        bundle = plan["bundles"][0]
        self.assertEqual(bundle["paths"], paths)
        self.assertEqual(bundle["semantic_group_id"], "users-lifecycle")
        self.assertEqual(bundle["title"], "Users lifecycle")
        self.assertEqual(bundle["reviewers"], ["correctness", "test_gap"])
        self.assertIn(
            "implementation and its behavioral test",
            bundle["grouping_reasons"],
        )

    def test_agent_grouping_must_cover_every_eligible_path_exactly_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            _repo_dir, run_dir = self._run_dir(Path(tmp_dir))
            paths = ["src/a.py", "src/b.py"]
            write_json(
                run_dir / "inventory.json",
                {
                    "schema_version": "inventory/v1",
                    "files": [_inventory_item(path) for path in paths],
                },
            )
            write_json(
                run_dir / "risk-routing.json",
                {
                    "schema_version": "risk-routing/v1",
                    "routes": [{"path": path, "tier": "P1"} for path in paths],
                },
            )
            prepare_bundle_planning_input(run_dir, _job())
            write_json(
                run_dir / "bundle-grouping.json",
                {
                    "schema_version": "bundle-grouping/v1",
                    "groups": [
                        {
                            "group_id": "incomplete",
                            "tier": "P1",
                            "title": "Incomplete",
                            "paths": ["src/a.py", "src/a.py"],
                            "grouping_reasons": ["same component"],
                        }
                    ],
                },
            )

            with self.assertRaisesRegex(
                RuntimeError,
                "duplicate path.*src/a.py.*missing eligible path.*src/b.py",
            ):
                materialize_agent_bundle_plan(run_dir, _job())

    def test_worker_splits_an_oversized_agent_group_without_changing_its_tier(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo_dir, run_dir = self._run_dir(Path(tmp_dir))
            path = "src/large.py"
            source = repo_dir / path
            source.parent.mkdir(parents=True)
            source.write_text(
                "\n".join(
                    f"line_{index} = '{'x' * 100}'" for index in range(1500)
                ),
                encoding="utf-8",
            )
            write_json(
                run_dir / "inventory.json",
                {
                    "schema_version": "inventory/v1",
                    "files": [
                        {
                            **_inventory_item(
                                path,
                                estimated_tokens=MAX_BUNDLE_ESTIMATED_TOKENS * 3,
                            ),
                            "line_count": 1500,
                        }
                    ],
                },
            )
            write_json(
                run_dir / "risk-routing.json",
                {
                    "schema_version": "risk-routing/v1",
                    "routes": [{"path": path, "tier": "P0"}],
                },
            )
            prepare_bundle_planning_input(run_dir, _job())
            write_json(
                run_dir / "bundle-grouping.json",
                {
                    "schema_version": "bundle-grouping/v1",
                    "groups": [
                        {
                            "group_id": "large-critical-module",
                            "tier": "P0",
                            "title": "Large critical module",
                            "paths": [path],
                            "grouping_reasons": ["single cohesive module"],
                        }
                    ],
                },
            )

            plan = materialize_agent_bundle_plan(run_dir, _job())
            pack_bundles(repo_dir, run_dir)
            packed_sizes = [
                max(len(payload), len(payload.encode("utf-8")))
                for payload in (
                    (run_dir / "bundles" / f"{bundle['bundle_id']}.md").read_text(
                        encoding="utf-8"
                    )
                    for bundle in plan["bundles"]
                )
            ]

        self.assertGreater(len(plan["bundles"]), 1)
        self.assertTrue(all(bundle["tier"] == "P0" for bundle in plan["bundles"]))
        self.assertTrue(
            all(
                bundle["semantic_group_id"] == "large-critical-module"
                for bundle in plan["bundles"]
            )
        )
        self.assertTrue(
            all(size <= MAX_BUNDLE_ESTIMATED_TOKENS for size in packed_sizes),
            packed_sizes,
        )


if __name__ == "__main__":
    unittest.main()
