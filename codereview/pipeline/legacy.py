from __future__ import annotations

import importlib.util
import sys
import threading
from pathlib import Path
from types import ModuleType

_LEGACY_LOCK = threading.Lock()
_LEGACY_MODULE_NAME = "codereview._legacy_simple_review"
_LEGACY_MODULE: ModuleType | None = None


def get_legacy_simple_review() -> ModuleType:
    """Load the pre-refactor simple_review.py module under a private name.

    The new pipeline is published as the ``codereview.simple_review`` package so
    existing imports keep working. The old monolith remains on disk as a
    rollback/reference implementation and is loaded here without owning the
    public module name.
    """
    global _LEGACY_MODULE
    with _LEGACY_LOCK:
        if _LEGACY_MODULE is not None:
            return _LEGACY_MODULE
        existing = sys.modules.get(_LEGACY_MODULE_NAME)
        if existing is not None:
            _LEGACY_MODULE = existing
            return existing
        legacy_path = Path(__file__).resolve().parents[1] / "simple_review.py"
        spec = importlib.util.spec_from_file_location(_LEGACY_MODULE_NAME, legacy_path)
        if spec is None or spec.loader is None:
            raise ImportError(f"unable to load legacy simple_review module from {legacy_path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[_LEGACY_MODULE_NAME] = module
        spec.loader.exec_module(module)
        _LEGACY_MODULE = module
        return module
