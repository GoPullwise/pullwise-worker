"""Render the human-readable Worker Slice 0 evidence tables."""

from __future__ import annotations

from typing import Any


def _markdown(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def render_document(baseline: dict[str, Any]) -> str:
    lines = [
        f"> Generated from `{baseline['baseline_id']}` with `{baseline['line_count_profile']}`. Do not edit this block by hand.",
        "",
        f"Captured Worker HEAD `{baseline['captured_head']}` is informational only. This is current-implementation evidence; it does not assign future Agent Kernel ownership or authorize production implementation.",
        "",
        "### Current implementation map",
        "",
        "| Current scope | Paths | Current responsibilities | Ownership/call boundary | Candidate extraction seam |",
        "|---|---|---|---|---|",
    ]
    for entry in baseline["code_map"]:
        paths = ", ".join(f"`{source['path']}`" for source in entry["paths"])
        lines.append(
            f"| `{entry['id']}` | {paths} | {_markdown(entry['current_responsibilities'])} | {_markdown(entry['boundary'])} | {_markdown(entry['candidate_extraction_seam'])} |"
        )
    lines.extend(["", "### Current PIPELINE_PHASES registry", "", "| Order | Phase | Progress ceiling |", "|---:|---|---:|"])
    for index, (phase, progress) in enumerate(baseline["pipeline"]["values"], start=1):
        lines.append(f"| {index} | `{phase}` | {progress} |")
    lines.extend(
        [
            "",
            "### Handwritten file-size ratchet",
            "",
            "The inventory covers every Git-tracked regular file above 400 physical lines that matches the fixed handwritten code/test/maintenance suffix, name, or extensionless-executable catalog. `oversized_legacy` is the >600 grandfathered baseline; `review_trigger_existing` is the existing 401-600 review-trigger range. Any count drift or unregistered trigger file fails verification.",
            "",
            "| Path | Kind | Classification | Physical lines | Current responsibilities | Candidate extraction seam |",
            "|---|---|---|---:|---|---|",
        ]
    )
    for entry in baseline["file_baselines"]:
        lines.append(
            f"| `{entry['path']}` | `{entry['kind']}` | `{entry['classification']}` | {entry['physical_lines']} | {_markdown(entry['current_responsibilities'])} | {_markdown(entry['candidate_extraction_seam'])} |"
        )
    return "\n".join(lines)
