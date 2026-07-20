from __future__ import annotations

from contextlib import redirect_stdout
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
D27_DIGEST = "f3ef27ad6318d4da20d4750cdde9387b66045f1708a909b57aba1c6e48ec2b0e"


class AgentFirstLegacyAbsenceTest(unittest.TestCase):
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

        register = self.roots["worker"] / "contracts" / "agent-first" / REGISTER.name
        register.parent.mkdir(parents=True)
        shutil.copyfile(REGISTER, register)
        self.inventory_path = register.parent / "legacy-removal-inventory.json"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _inventory(self) -> dict[str, object]:
        return {
            "schema_id": "pullwise-agent-first-legacy-removal-inventory/v1",
            "inventory_id": "synthetic-legacy-removal",
            "d27": {
                "register_path": "contracts/agent-first/spec-decision-register.json",
                "decision_id": "D27",
                "selected_option_id": "clean_break_no_legacy",
                "resolution_sha256": D27_DIGEST,
            },
            "signatures": [
                {
                    "id": "legacy-protocol",
                    "literal": "review-worker-protocol/v1",
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
                    "signature_ids": ["legacy-protocol"],
                }
            ],
        }

    def _write_inventory(self, payload: dict[str, object]) -> None:
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

    def test_default_mode_reports_inventoried_legacy_without_blocking(self) -> None:
        marker = "review-worker-" + "protocol/v1"
        (self.roots["worker"] / "legacy.py").write_text(
            f"PROTOCOL = {marker!r}\n",
            encoding="utf-8",
        )
        self._write_inventory(self._inventory())

        exit_code, report = self._invoke()

        self.assertEqual(0, exit_code)
        self.assertEqual("legacy_present", report["status"])
        self.assertFalse(report["legacy_absent"])
        self.assertTrue(report["ratchet_clean"])
        self.assertEqual("present", report["surfaces"][0]["status"])
        self.assertEqual([], report["unexpected_surfaces"])

    def test_default_mode_blocks_an_unregistered_legacy_surface(self) -> None:
        marker = "review-worker-" + "protocol/v1"
        (self.roots["worker"] / "legacy.py").write_text(
            f"PROTOCOL = {marker!r}\n", encoding="utf-8"
        )
        (self.roots["worker"] / "new_legacy.py").write_text(
            f"PROTOCOL = {marker!r}\n", encoding="utf-8"
        )
        self._write_inventory(self._inventory())

        exit_code, report = self._invoke()

        self.assertEqual(1, exit_code)
        self.assertEqual("unexpected_legacy", report["status"])
        self.assertFalse(report["ratchet_clean"])
        self.assertEqual(
            [
                {
                    "repo": "worker",
                    "path": "new_legacy.py",
                    "signature_id": "legacy-protocol",
                }
            ],
            report["unexpected_surfaces"],
        )

    def test_require_absent_blocks_an_inventoried_surface(self) -> None:
        marker = "review-worker-" + "protocol/v1"
        (self.roots["worker"] / "legacy.py").write_text(
            f"PROTOCOL = {marker!r}\n", encoding="utf-8"
        )
        self._write_inventory(self._inventory())

        exit_code, report = self._invoke("--require-absent")

        self.assertEqual(1, exit_code)
        self.assertEqual("legacy_present", report["status"])
        self.assertTrue(report["require_absent"])
        self.assertTrue(report["ratchet_clean"])
        self.assertEqual(
            [
                {
                    "code": "legacy_surface_present",
                    "surface_id": "worker.legacy-runtime",
                }
            ],
            report["failures"],
        )


if __name__ == "__main__":
    unittest.main()
