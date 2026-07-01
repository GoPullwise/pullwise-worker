from __future__ import annotations

from codereview.pipeline.legacy import get_legacy_simple_review
from codereview.pipeline.runner import ENGINE_VERSION, ReviewPipeline, run_review

_legacy = get_legacy_simple_review()
for _name, _value in vars(_legacy).items():
    if _name != "__version__" and _name.startswith("__") and _name.endswith("__"):
        continue
    globals().setdefault(_name, _value)

globals()["ENGINE_VERSION"] = ENGINE_VERSION
globals()["ReviewPipeline"] = ReviewPipeline
globals()["run_review"] = run_review

__all__ = [
    name
    for name in globals()
    if name == "__version__" or not (name.startswith("__") and name.endswith("__"))
]
