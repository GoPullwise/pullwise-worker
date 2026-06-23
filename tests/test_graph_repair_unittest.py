from __future__ import annotations

import tempfile
import unittest
import importlib
from pathlib import Path
from unittest.mock import patch

from codereview.config import ReviewConfig

codereview_main = importlib.import_module("codereview.main")


class GraphRepairMappingTest(unittest.TestCase):
    def test_graph_repair_uses_deterministic_mapper_even_when_codex_enrichment_is_enabled(self) -> None:
        config = ReviewConfig()
        config.graph.codex_mappers = True
        captured_configs: list[object] = []

        def fake_map_graph_tasks(checkout, tasks, inventory, mapped_config, *, run):
            del checkout, tasks, inventory, run
            captured_configs.append(mapped_config)
            return []

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            with patch.object(codereview_main, "map_graph_tasks", side_effect=fake_map_graph_tasks):
                codereview_main._map_graph_repair_tasks_with_progress(
                    root,
                    [{"task_id": "graph-repair-0001", "shard_id": "repair-0001", "files": ["app.py"]}],
                    {"files": [{"path": "app.py", "scope": "analyze"}]},
                    config,
                    run=root / "run",
                    progress=None,
                    progress_label="Graph: repair round 1",
                )

        self.assertEqual(len(captured_configs), 1)
        self.assertFalse(captured_configs[0].graph.codex_mappers)
        self.assertTrue(config.graph.codex_mappers)


if __name__ == "__main__":
    unittest.main()
