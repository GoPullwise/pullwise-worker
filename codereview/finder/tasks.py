from __future__ import annotations

from dataclasses import dataclass

from ..slicing.risk_tags import choose_finders


@dataclass
class FinderTask:
    slice_id: str
    focus: str


def plan_finder_tasks(slices: list[dict]) -> list[FinderTask]:
    tasks: list[FinderTask] = []
    for item in slices:
        risk_tags = set(str(tag) for tag in (item.get("risk_tags") or []))
        for focus in choose_finders(risk_tags):
            tasks.append(FinderTask(slice_id=str(item.get("slice_id")), focus=focus))
    return tasks
