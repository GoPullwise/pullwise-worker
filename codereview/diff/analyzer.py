from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from ..utils.process import run_process


@dataclass
class DiffResult:
    changed_files: list[str]
    hunks: list[dict]


class DiffError(RuntimeError):
    pass


_HUNK_RE = re.compile(r"@@ -(?P<old_start>\d+)(?:,(?P<old_len>\d+))? \+(?P<new_start>\d+)(?:,(?P<new_len>\d+))? @@")


def analyze_git_diff(checkout: Path, base_ref: str, head_ref: str) -> DiffResult:
    names = run_process(["git", "diff", "--name-only", f"{base_ref}...{head_ref}"], cwd=checkout, timeout=120)
    if names.returncode != 0:
        raise DiffError(f"git diff --name-only failed: {(names.stderr or names.stdout)[-500:]}")
    changed_files = [line.strip().replace("\\", "/") for line in names.stdout.splitlines() if line.strip()]
    patch = run_process(["git", "diff", "--unified=0", f"{base_ref}...{head_ref}"], cwd=checkout, timeout=120)
    if patch.returncode != 0:
        raise DiffError(f"git diff --unified=0 failed: {(patch.stderr or patch.stdout)[-500:]}")
    hunks: list[dict] = []
    current_file = ""
    for line in patch.stdout.splitlines():
        if line.startswith("+++ b/"):
            current_file = line[6:].strip().replace("\\", "/")
            continue
        match = _HUNK_RE.search(line)
        if match and current_file:
            new_start = int(match.group("new_start"))
            new_len = int(match.group("new_len") or "1")
            old_start = int(match.group("old_start"))
            old_len = int(match.group("old_len") or "1")
            hunks.append(
                {
                    "file": current_file,
                    "old_start": old_start,
                    "old_lines": old_len,
                    "new_start": new_start,
                    "new_lines": new_len,
                    "new_end": max(new_start, new_start + new_len - 1),
                }
            )
    return DiffResult(changed_files=changed_files, hunks=hunks)
