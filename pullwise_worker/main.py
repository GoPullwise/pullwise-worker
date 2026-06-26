from __future__ import annotations

import importlib as _importlib
import sys as _sys
from types import ModuleType as _ModuleType

_PART_MODULE_NAMES = (
    "_main_part_01_bootstrap",
    "_main_part_02_worker_checkout",
    "_main_part_03_preflight",
    "_main_part_04_graph_verified_review",
    "_main_part_07_readiness_doctor",
    "_main_part_08_lifecycle_cleanup",
)

_PART_MODULES: list[_ModuleType] = []


def _export_module(module: _ModuleType) -> None:
    for name, value in vars(module).items():
        if name != "__version__" and name.startswith("__") and name.endswith("__"):
            continue
        globals()[name] = value


for _module_name in _PART_MODULE_NAMES:
    _module = _importlib.import_module(f"{__package__}.{_module_name}")
    _PART_MODULES.append(_module)
    _export_module(_module)

for _module in _PART_MODULES:
    for _name, _value in list(globals().items()):
        if _name != "__version__" and _name.startswith("__") and _name.endswith("__"):
            continue
        vars(_module).setdefault(_name, _value)

_PART_MODULES_FOR_PATCH = tuple(_PART_MODULES)


class _AggregateModule(_ModuleType):
    def __getattribute__(self, name: str) -> object:
        if name == "__version__" or not (name.startswith("__") and name.endswith("__")):
            modules = super().__getattribute__("__dict__").get("_PART_MODULES_FOR_PATCH", ())
            for module in modules:
                if hasattr(module, name):
                    return getattr(module, name)
        return super().__getattribute__(name)

    def __setattr__(self, name: str, value: object) -> None:
        super().__setattr__(name, value)
        for module in self.__dict__.get("_PART_MODULES_FOR_PATCH", ()):
            if hasattr(module, name):
                setattr(module, name, value)


_sys.modules[__name__].__class__ = _AggregateModule

del _module, _module_name, _name, _value, _export_module, _importlib, _sys, _ModuleType, _PART_MODULES, _PART_MODULE_NAMES

if __name__ == "__main__":
    main()
