from __future__ import annotations

import re
from pathlib import Path

from setuptools import find_packages, setup


def package_version() -> str:
    init_text = Path(__file__).with_name("pullwise_worker").joinpath("__init__.py").read_text(encoding="utf-8")
    match = re.search(r'^__version__ = "([^"]+)"', init_text, flags=re.MULTILINE)
    if not match:
        raise RuntimeError("Unable to read pullwise-worker version")
    return match.group(1)


setup(
    name="pullwise-worker",
    version=package_version(),
    description="Pullwise pull-based scan worker",
    python_requires=">=3.10",
    packages=find_packages(include=["pullwise_worker", "codereview", "codereview.*"]),
    entry_points={"console_scripts": ["pullwise-worker=pullwise_worker.main:main"]},
    include_package_data=True,
)
