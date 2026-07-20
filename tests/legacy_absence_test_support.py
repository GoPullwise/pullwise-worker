from __future__ import annotations

from contextlib import redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from scripts import verify_agent_first_legacy_absence as absence


ROOT = Path(__file__).resolve().parents[1]
REGISTER = ROOT / "contracts" / "agent-first" / "spec-decision-register.json"
BASELINE = ROOT / "contracts" / "agent-first" / "legacy-v1-contract-baseline.json"
D27_DIGEST = "f3ef27ad6318d4da20d4750cdde9387b66045f1708a909b57aba1c6e48ec2b0e"
CATALOG_FIELDS = (
    "d27",
    "frozen_baseline",
    "signatures",
    "evidence_exclusions",
    "surfaces",
)
FROZEN_SURFACE_IDS = [
    "server.artifact-event-wire",
    "server.cancellation-fixtures",
    "server.claim-policy-source",
    "server.claim-result-projection",
    "server.durable-protocol-storage",
    "server.policy-fixtures",
    "server.progress-debug-projection",
    "server.result-fixtures",
    "server.route-fixtures",
    "server.status-projection",
    "server.system-limit-fixtures",
    "server.system-limits",
    "web.api-consumer",
    "web.api-fixtures",
    "web.flow-fixtures",
    "web.flow-projection",
    "web.history-fixtures",
    "web.history-projection",
    "web.normalizer-consumer",
    "web.normalizer-fixtures",
    "web.progress-fixtures",
    "web.progress-projection",
    "web.timing-fixtures",
    "web.timing-projection",
    "worker.public-scan-canonical-fixture",
    "worker.public-scan-fixture-validator",
    "worker.strict-v1-wire-canonical-fixture",
    "worker.strict-v1-wire-fixture-validator",
]


def catalog_sha256(payload: dict[str, object]) -> str:
    catalog = {key: payload[key] for key in CATALOG_FIELDS}
    canonical = json.dumps(
        catalog,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class LegacyAbsenceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(prefix="pullwise-legacy-absence-")
        self.workspace = Path(self.temp_dir.name)
        self.roots = {
            repo_id: self.workspace / directory
            for repo_id, directory in {
                "server": "pullwise-server",
                "web": "pullwise-web",
                "worker": "pullwise-worker",
            }.items()
        }
        for root in self.roots.values():
            root.mkdir()
            subprocess.run(
                ["git", "init", "--quiet", str(root)],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

        contract_root = self.roots["worker"] / "contracts" / "agent-first"
        contract_root.mkdir(parents=True)
        shutil.copyfile(REGISTER, contract_root / REGISTER.name)
        shutil.copyfile(BASELINE, contract_root / BASELINE.name)
        self.inventory_path = contract_root / "legacy-removal-inventory.json"
        git_exclude = self.roots["worker"] / ".git" / "info" / "exclude"
        git_exclude.write_text(
            "contracts/agent-first/legacy-v1-contract-baseline.json\n"
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _inventory(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "schema_id": "pullwise-agent-first-legacy-removal-inventory/v1",
            "inventory_id": "synthetic-legacy-removal",
            "catalog_sha256": "",
            "d27": {
                "register_path": "contracts/agent-first/spec-decision-register.json",
                "decision_id": "D27",
                "selected_option_id": "clean_break_no_legacy",
                "resolution_sha256": D27_DIGEST,
            },
            "frozen_baseline": {
                "path": "contracts/agent-first/legacy-v1-contract-baseline.json",
                "baseline_id": "legacy-v1-server-web-2026-07-17",
                "text_sha256": "16b564b52cfa14e7504cd71af382fde1ff6b35e71ed85f91e722b0ccf450f6fd",
                "surface_ids": FROZEN_SURFACE_IDS,
            },
            "signatures": [
                {
                    "id": "legacy-protocol",
                    "literal": "review-worker-" + "protocol/v1",
                }
            ],
            "evidence_exclusions": [
                {
                    "id": "decision-history",
                    "repo": "worker",
                    "path": "contracts/agent-first/spec-decision-register.json",
                    "reason": "immutable_decision_history",
                    "start_marker": None,
                    "end_marker": None,
                },
                {
                    "id": "inventory-control",
                    "repo": "worker",
                    "path": "contracts/agent-first/legacy-removal-inventory.json",
                    "reason": "absence_gate_control",
                    "start_marker": None,
                    "end_marker": None,
                },
            ],
            "surfaces": [
                {
                    "id": "worker.legacy-runtime",
                    "repo": "worker",
                    "path": "legacy.py",
                    "signature_occurrence_ceilings": {"legacy-protocol": 1},
                }
            ],
        }
        payload["catalog_sha256"] = catalog_sha256(payload)
        return payload

    def _write_inventory(self, payload: dict[str, object]) -> None:
        payload["catalog_sha256"] = catalog_sha256(payload)
        self.inventory_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def _invoke(self, *extra: str) -> tuple[int, dict[str, object]]:
        output = io.StringIO()
        with redirect_stdout(output):
            exit_code = absence.main(
                [
                    "--inventory",
                    str(self.inventory_path),
                    "--workspace-root",
                    str(self.workspace),
                    *extra,
                ]
            )
        return exit_code, json.loads(output.getvalue())
