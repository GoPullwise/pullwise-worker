"""Render the human-readable Agent-First decision packet."""

from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

from scripts.agent_first_decision_core import decision_applicability


def _markdown(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def render_document(register: dict[str, Any]) -> str:
    decisions_by_id = {
        decision["id"]: decision for decision in register["decisions"]
    }
    ordered_decisions = [
        decisions_by_id[decision_id]
        for decision_id in register["question_order"]
    ]
    active_decision_id = register["active_decision_id"] or "none"
    lines = [
        f"> Generated from `{register['register_id']}`. Recommendations are non-normative and are never resolutions. Do not edit this block by hand.",
        "",
        f"Active question: `{active_decision_id}`. Questions are asked one at a time. User silence, existing prose, current code, and Agent inference cannot resolve a decision.",
        "",
        "| ID | Scope | Decision | Stored status | Applicability | Required before | Depends on | Non-normative recommendation |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for decision in ordered_decisions:
        dependencies = ", ".join(decision["depends_on"]) or "—"
        lines.append(
            f"| `{decision['id']}` | `{_markdown(decision['scope'])}` | {_markdown(decision['title'])} | `{decision['status']}` | `{decision_applicability(register, decision['id'])}` | `{decision['required_by_slice']}` | {_markdown(dependencies)} | `{decision['recommended_option_id']}` |"
        )
    for decision in ordered_decisions:
        lines.extend(
            [
                "",
                f"### {decision['id']} — {decision['title']}",
                "",
                f"**Stored status:** `{decision['status']}`; **applicability:** `{decision_applicability(register, decision['id'])}`; **required before:** `{decision['required_by_slice']}`.",
                "",
                f"**Question:** {decision['question']}",
                "",
                "**Options:**",
                "",
            ]
        )
        resolution = decision["resolution"]
        selected_option_id = (
            None if resolution is None else resolution["selected_option_id"]
        )
        for option in decision["options"]:
            if option["id"] == selected_option_id:
                suffix = " — selected by resolution"
            elif option["id"] == decision["recommended_option_id"]:
                suffix = " — non-normative recommendation, not selected"
            else:
                suffix = ""
            lines.append(
                f"- `{option['id']}`{suffix}: {option['summary']} {option['rationale']} Consequences: {'; '.join(option['consequences'])}"
            )
        if resolution is None:
            lines.extend(["", "**Resolution:** No option has been selected."])
        else:
            custom = (
                f" Custom text: {resolution['custom_text']}"
                if resolution["custom_text"] is not None
                else ""
            )
            lines.extend(
                [
                    "",
                    f"**Resolution:** `{resolution['selected_option_id']}` (`{resolution['kind']}`). {resolution['decision_text']}{custom}",
                    "",
                    f"**Authority/evidence:** `{resolution['authority']}` on `{resolution['decided_at']}`; {', '.join(f'`{item}`' for item in resolution['evidence_refs'])}; digest `{resolution['resolution_sha256']}`.",
                ]
            )
        lines.extend(
            [
                "",
                f"**Supersedes:** {', '.join(decision['supersedes']) or 'none'}",
                "",
                f"**Effects:** {', '.join(f'`{item}`' for item in decision['effects'])}",
                "",
                f"**Sources:** {', '.join(f'`{item}`' for item in decision['source_refs'])}",
            ]
        )
    return "\n".join(lines)


def render_generated_file(register: dict[str, Any]) -> str:
    document = register["document"]
    return "\n".join(
        [
            "# Agent-First Worker Specification Decision Register",
            "",
            "Status: generated Agent-First decision packet. Pending recommendations are "
            "non-normative and grant no implementation authority.",
            "",
            "Machine source: "
            "contracts/agent-first/spec-decision-register.json.",
            "",
            document["start_marker"],
            render_document(register),
            document["end_marker"],
            "",
        ]
    )


def sync_generated_file(
    register: dict[str, Any], repo_root: Path
) -> Path:
    root = repo_root.resolve()
    relative = register["document"]["path"]
    target = root.joinpath(*PurePosixPath(relative).parts)
    try:
        if target.resolve(strict=False).relative_to(root) != Path(relative):
            raise ValueError
        if target.exists() or target.is_symlink():
            if not stat.S_ISREG(os.lstat(target).st_mode):
                raise ValueError
        if not target.parent.is_dir():
            raise ValueError
    except (OSError, ValueError) as exc:
        raise ValueError(f"generated_document:unsafe_path:{relative}") from exc
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            newline="\n",
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(render_generated_file(register))
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, target)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
    return target
