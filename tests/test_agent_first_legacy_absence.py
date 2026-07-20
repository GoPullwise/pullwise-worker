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

    def test_require_absent_passes_when_inventoried_legacy_is_gone(self) -> None:
        self._write_inventory(self._inventory())

        exit_code, report = self._invoke("--require-absent")

        self.assertEqual(0, exit_code)
        self.assertEqual("absent", report["status"])
        self.assertTrue(report["legacy_absent"])
        self.assertEqual([], report["failures"])

    def test_inventory_rejects_a_parent_traversal_surface_path(self) -> None:
        payload = self._inventory()
        payload["surfaces"][0]["path"] = "../legacy.py"
        self._write_inventory(payload)

        exit_code, report = self._invoke()

        self.assertEqual(2, exit_code)
        self.assertEqual("indeterminate", report["status"])
        self.assertEqual("inventory_invalid", report["error_kind"])

    def test_bounded_d27_evidence_is_excluded_without_hiding_the_file(self) -> None:
        marker = "review-worker-" + "protocol/v1"
        start = "<!-- BEGIN D27 EVIDENCE -->"
        end = "<!-- END D27 EVIDENCE -->"
        (self.roots["worker"] / "evidence.md").write_text(
            f"before\n{start}\n{marker}\n{end}\nafter\n",
            encoding="utf-8",
        )
        payload = self._inventory()
        payload["evidence_exclusions"].insert(0,
            {
                "id": "bounded-d27-evidence",
                "repo": "worker",
                "path": "evidence.md",
                "reason": "d27_evidence",
                "start_marker": start,
                "end_marker": end,
            }
        )
        self._write_inventory(payload)

        exit_code, report = self._invoke()

        self.assertEqual(0, exit_code)
        self.assertEqual("absent", report["status"])
        self.assertEqual([], report["unexpected_surfaces"])

        (self.roots["worker"] / "evidence.md").write_text(
            f"{marker}\n{start}\n{marker}\n{end}\n", encoding="utf-8"
        )
        exit_code, report = self._invoke()
        self.assertEqual(1, exit_code)
        self.assertEqual("evidence.md", report["unexpected_surfaces"][0]["path"])

    def test_bounded_evidence_markers_fail_closed_when_missing(self) -> None:
        payload = self._inventory()
        payload["evidence_exclusions"].insert(
            0,
            {
                "id": "bounded-d27-evidence",
                "repo": "worker",
                "path": "evidence.md",
                "reason": "d27_evidence",
                "start_marker": "<!-- BEGIN D27 EVIDENCE -->",
                "end_marker": "<!-- END D27 EVIDENCE -->",
            },
        )
        (self.roots["worker"] / "evidence.md").write_text(
            "markers removed\n", encoding="utf-8"
        )
        self._write_inventory(payload)

        exit_code, report = self._invoke()

        self.assertEqual(2, exit_code)
        self.assertEqual("inventory_invalid", report["error_kind"])

    def test_whole_file_exclusion_does_not_hide_a_similarly_named_backup(self) -> None:
        marker = "review-worker-" + "protocol/v1"
        excluded = self.roots["worker"] / "history.md"
        excluded.write_text(marker, encoding="utf-8")
        (self.roots["worker"] / "history.md.bak").write_text(marker, encoding="utf-8")
        payload = self._inventory()
        payload["evidence_exclusions"].insert(
            0,
            {
                "id": "archived-decision-history",
                "repo": "worker",
                "path": "history.md",
                "reason": "immutable_decision_history",
                "start_marker": None,
                "end_marker": None,
            },
        )
        self._write_inventory(payload)

        exit_code, report = self._invoke()

        self.assertEqual(1, exit_code)
        self.assertFalse(report["legacy_absent"])
        self.assertEqual("history.md.bak", report["unexpected_surfaces"][0]["path"])

    def test_d27_binding_rejects_a_noncanonical_digest(self) -> None:
        payload = self._inventory()
        payload["d27"]["resolution_sha256"] = "0" * 64
        self._write_inventory(payload)

        exit_code, report = self._invoke()

        self.assertEqual(2, exit_code)
        self.assertEqual("inventory_invalid", report["error_kind"])

    def test_registered_directory_is_rejected_as_nonregular(self) -> None:
        (self.roots["worker"] / "legacy.py").mkdir()
        self._write_inventory(self._inventory())

        exit_code, report = self._invoke()

        self.assertEqual(2, exit_code)
        self.assertEqual("environment_invalid", report["error_kind"])

    def test_registered_symlink_is_rejected(self) -> None:
        target = self.workspace / "outside.py"
        target.write_text("review-worker-" + "protocol/v1", encoding="utf-8")
        try:
            (self.roots["worker"] / "legacy.py").symlink_to(target)
        except OSError as exc:
            self.skipTest(f"file symlinks are unavailable: {exc}")
        self._write_inventory(self._inventory())

        exit_code, report = self._invoke()

        self.assertEqual(2, exit_code)
        self.assertEqual("environment_invalid", report["error_kind"])

    def test_duplicate_inventory_keys_are_rejected(self) -> None:
        payload = json.dumps(self._inventory())
        payload = payload.replace(
            '"inventory_id": "synthetic-legacy-removal",',
            '"inventory_id": "synthetic-legacy-removal", "inventory_id": "other",',
        )
        self.inventory_path.write_text(payload, encoding="utf-8")

        exit_code, report = self._invoke()

    def test_inventory_rejects_an_unapproved_whole_file_exclusion(self) -> None:
        marker = "review-worker-" + "protocol/v1"
        (self.roots["worker"] / "new_legacy.py").write_text(marker, encoding="utf-8")
        payload = self._inventory()
        payload["evidence_exclusions"].insert(
            0,
            {
                "id": "arbitrary-exclusion",
                "repo": "worker",
                "path": "new_legacy.py",
                "reason": "d27_evidence",
                "start_marker": None,
                "end_marker": None,
            },
        )
        self._write_inventory(payload)

        exit_code, report = self._invoke()

        self.assertEqual(2, exit_code)
        self.assertEqual("inventory_invalid", report["error_kind"])

    def test_tracked_deleted_surface_is_absent_in_strict_mode(self) -> None:
        legacy = self.roots["worker"] / "legacy.py"
        legacy.write_text("review-worker-" + "protocol/v1", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.roots["worker"]), "add", "legacy.py"], check=True)
        legacy.unlink()
        self._write_inventory(self._inventory())

        exit_code, report = self._invoke("--require-absent")

        self.assertEqual(0, exit_code)
        self.assertEqual("absent", report["status"])

    def test_extensionless_legacy_surface_is_not_skipped(self) -> None:
        marker = "review-worker-" + "protocol/v1"
        (self.roots["worker"] / "new_legacy").write_text(marker, encoding="utf-8")
        self._write_inventory(self._inventory())

        exit_code, report = self._invoke()

        self.assertEqual(1, exit_code)
        self.assertEqual("new_legacy", report["unexpected_surfaces"][0]["path"])

        self.assertEqual(2, exit_code)
        self.assertEqual("inventory_invalid", report["error_kind"])

    def test_inventory_rejects_unknown_fields(self) -> None:
        payload = self._inventory()
        payload["compatibility_mode"] = True
        self._write_inventory(payload)

        exit_code, report = self._invoke()

        self.assertEqual(2, exit_code)
        self.assertEqual("indeterminate", report["status"])
        self.assertEqual("inventory_invalid", report["error_kind"])


if __name__ == "__main__":
    unittest.main()
