from __future__ import annotations

from ..config import ReviewConfig


def plan_graph_tasks(census: dict, inventory: dict, config: ReviewConfig) -> list[dict]:
    del inventory
    high_risk_paths = {
        str(path)
        for root in census.get("high_risk_roots", [])
        if isinstance(root, dict)
        for path in _paths_under_root(root.get("path"), census)
    }
    tasks: list[dict] = []
    for shard in census.get("shards", []):
        if not isinstance(shard, dict):
            continue
        files = [str(path) for path in shard.get("files", []) if str(path)]
        needs_double = config.graph.double_map_high_risk and bool(high_risk_paths & set(files))
        mapper_count = 2 if needs_double else 1
        for mapper_index in range(mapper_count):
            tasks.append(
                {
                    "task_id": f"graph-map-{len(tasks) + 1:04d}",
                    "shard_id": str(shard.get("shard_id") or f"shard-{len(tasks) + 1:04d}"),
                    "mapper_index": mapper_index + 1,
                    "files": files,
                    "reason": str(shard.get("reason") or ""),
                    "double_mapped": mapper_count > 1,
                }
            )
    return tasks


def _paths_under_root(root: object, census: dict) -> list[str]:
    root_text = str(root or "").strip("./")
    paths: list[str] = []
    for shard in census.get("shards", []):
        if not isinstance(shard, dict):
            continue
        for path in shard.get("files", []):
            text = str(path)
            if not root_text or root_text == "." or text.startswith(f"{root_text}/"):
                paths.append(text)
    return paths
