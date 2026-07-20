from __future__ import annotations

import re
from pathlib import Path

from setuptools import find_packages, setup


REPO_ROOT = Path(__file__).resolve().parent
CONTRACT_ROOT = REPO_ROOT / 'contracts' / 'agent-task' / 'v1'


def agent_kernel_contract_data_files() -> list[tuple[str, list[str]]]:
    def relative_json_files(directory: Path) -> list[str]:
        return [
            path.relative_to(REPO_ROOT).as_posix()
            for path in sorted(directory.glob('*.json'))
        ]

    destination = 'share/pullwise-worker/contracts/agent-task/v1'
    return [
        (destination, relative_json_files(CONTRACT_ROOT)),
        (
            f'{destination}/fixtures',
            relative_json_files(CONTRACT_ROOT / 'fixtures'),
        ),
    ]


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
    packages=find_packages(include=["pullwise_worker"]),
    install_requires=["openai-codex"],
    entry_points={"console_scripts": ["pullwise-worker=pullwise_worker.main:main"]},
    include_package_data=True,
    data_files=agent_kernel_contract_data_files(),
)


