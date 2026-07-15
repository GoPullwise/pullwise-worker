from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from pullwise_worker.review_worker_v1 import (
    MAX_BUNDLE_ESTIMATED_TOKENS,
    SEMANTIC_PHASES,
    materialize_agent_bundle_plan,
    pack_bundles,
    phase_prompt,
    prepare_bundle_planning_input,
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
        self.assertIn("Do not assign reviewers", prompt)

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
            planning_input = prepare_bundle_planning_input(run_dir)
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

            plan = materialize_agent_bundle_plan(run_dir)
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
            prepare_bundle_planning_input(run_dir)
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
                materialize_agent_bundle_plan(run_dir)

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
            prepare_bundle_planning_input(run_dir)
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

            plan = materialize_agent_bundle_plan(run_dir)
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
