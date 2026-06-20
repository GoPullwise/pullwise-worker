from __future__ import annotations

from dataclasses import dataclass

from ..units.risk_tags import choose_finders


@dataclass
class FinderTask:
    unit_id: str
    focus: str
    unit_type: str = ""
    review_pass: str = "baseline"
    risk_tags: list[str] | None = None


def plan_finder_tasks(units: list[dict]) -> list[FinderTask]:
    tasks: list[FinderTask] = []
    for item in units:
        risk_tags = set(str(tag) for tag in (item.get("risk_tags") or []))
        unit_type = str(item.get("unit_type") or "component")
        foci = choose_finders(risk_tags)
        if "correctness" not in foci:
            foci = ["correctness", *foci]
        if unit_type == "cross_boundary" and "api_contract" not in foci:
            foci.append("api_contract")
        if unit_type == "global_invariant":
            for focus in ("security_auth_dataflow", "state_concurrency_resource", "test_repro"):
                if focus not in foci:
                    foci.append(focus)
        for focus in foci:
            tasks.append(
                FinderTask(
                    unit_id=str(item.get("unit_id") or ""),
                    unit_type=unit_type,
                    review_pass="baseline" if focus == "correctness" else "specialist",
                    focus=focus,
                    risk_tags=sorted(risk_tags),
                )
            )
    return tasks
