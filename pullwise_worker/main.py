from __future__ import annotations

from pathlib import Path as _Path

_PART_FILES = (
    "_main_part_01_bootstrap.py",
    "_main_part_02_worker_checkout.py",
    "_main_part_03_preflight_verifier.py",
    "_main_part_04_review_audit_swarm.py",
    "_main_part_05_reportability_convergence.py",
    "_main_part_09_review_calibration.py",
    "_main_part_06_audit_artifacts.py",
    "_main_part_07_readiness_doctor.py",
    "_main_part_08_lifecycle_cleanup.py",
)


def _load_part(filename: str) -> None:
    path = _Path(__file__).with_name(filename)
    source = path.read_text(encoding="utf-8")
    exec(compile(source, str(path), "exec"), globals(), globals())


for _part_file in _PART_FILES:
    _load_part(_part_file)

del _part_file, _load_part, _PART_FILES, _Path
