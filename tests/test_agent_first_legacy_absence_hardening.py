from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from unittest.mock import patch

from scripts import agent_first_contract_files as contract_files
from scripts import verify_agent_first_legacy_absence as absence
from scripts.agent_first_decision_core import canonical_resolution_sha256
from scripts.agent_first_legacy_inventory import InventoryError, load_inventory
from tests.legacy_absence_test_support import (
    BASELINE,
    LegacyAbsenceTestCase,
    catalog_sha256,
)


class AgentFirstLegacyAbsenceHardeningTest(LegacyAbsenceTestCase):
    def test_existing_surface_blocks_occurrences_above_its_ceiling(self) -> None:
        marker = "review-worker-" + "protocol/v1"
        (self.roots["worker"] / "legacy.py").write_text(
            f"{marker}\n{marker}\n", encoding="utf-8"
        )
        self._write_inventory(self._inventory())

        exit_code, report = self._invoke()

        self.assertEqual(1, exit_code)
        excess = report["unexpected_surfaces"][0]
        self.assertEqual("occurrence_ceiling_exceeded", excess["kind"])
        self.assertEqual(1, excess["allowed_occurrences"])
        self.assertEqual(2, excess["actual_occurrences"])

    def test_unapproved_bounded_marker_pair_is_inventory_invalid(self) -> None:
        marker = "review-worker-" + "protocol/v1"
        start, end = "<!-- START OTHER -->", "<!-- END OTHER -->"
        (self.roots["worker"] / "AGENTS.md").write_text(
            f"{start}\n{marker}\n{end}\n", encoding="utf-8"
        )
        payload = self._inventory()
        payload["evidence_exclusions"].insert(
            0,
            {
                "id": "agents-arbitrary-section",
                "repo": "worker",
                "path": "AGENTS.md",
                "reason": "d27_evidence",
                "start_marker": start,
                "end_marker": end,
            },
        )
        self._write_inventory(payload)

        exit_code, report = self._invoke()

        self.assertEqual(2, exit_code)
        self.assertEqual("inventory_invalid", report["error_kind"])

    def test_actual_d27_register_supersession_tamper_is_rejected(self) -> None:
        register_path = (
            self.roots["worker"]
            / "contracts"
            / "agent-first"
            / "spec-decision-register.json"
        )
        register = json.loads(register_path.read_text(encoding="utf-8"))
        decision = next(item for item in register["decisions"] if item["id"] == "D27")
        decision["supersedes"] = []
        decision["resolution"]["resolution_sha256"] = canonical_resolution_sha256(
            decision["id"], decision["resolution"], decision["supersedes"]
        )
        register_path.write_text(json.dumps(register), encoding="utf-8")
        self._write_inventory(self._inventory())

        exit_code, report = self._invoke()

        self.assertEqual(2, exit_code)
        self.assertEqual("inventory_invalid", report["error_kind"])

    def test_registered_path_is_read_once_for_report_and_ratchet(self) -> None:
        legacy = self.roots["worker"] / "legacy.py"
        legacy.write_text("review-worker-" + "protocol/v1", encoding="utf-8")
        self._write_inventory(self._inventory())
        original = absence.read_surface
        observed = 0

        def counting_read(path: Path) -> bytes:
            nonlocal observed
            if path == legacy:
                observed += 1
            return original(path)

        with patch.object(absence, "read_surface", side_effect=counting_read):
            exit_code, _ = self._invoke()

        self.assertEqual(0, exit_code)
        self.assertEqual(1, observed)

    def test_strict_mode_includes_every_frozen_baseline_surface(self) -> None:
        baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
        surface = baseline["surfaces"][0]
        target = self.roots[surface["repo"]] / surface["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(surface["anchors"][0], encoding="utf-8")
        self._write_inventory(self._inventory())

        exit_code, report = self._invoke("--require-absent")

        self.assertEqual(1, exit_code)
        failures = {
            item["surface_id"]
            for item in report["failures"]
            if item["code"] == "legacy_surface_present"
        }
        self.assertIn(surface["id"], failures)

    def test_frozen_baseline_digest_is_verified_from_the_safe_snapshot(self) -> None:
        baseline_path = (
            self.roots["worker"]
            / "contracts"
            / "agent-first"
            / "legacy-v1-contract-baseline.json"
        )
        baseline_path.write_text(
            baseline_path.read_text(encoding="utf-8") + "\n", encoding="utf-8"
        )
        self._write_inventory(self._inventory())

        exit_code, report = self._invoke()

        self.assertEqual(2, exit_code)
        self.assertEqual("inventory_invalid", report["error_kind"])

    def test_occurrence_ceiling_rejects_a_boolean(self) -> None:
        payload = self._inventory()
        payload["surfaces"][0]["signature_occurrence_ceilings"] = {
            "legacy-protocol": True
        }
        self._write_inventory(payload)

        exit_code, report = self._invoke()

        self.assertEqual(2, exit_code)
        self.assertEqual("inventory_invalid", report["error_kind"])

    def test_overlapping_explicit_and_frozen_surface_path_is_read_once(self) -> None:
        baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
        frozen = baseline["surfaces"][0]
        marker = "review-worker-" + "protocol/v1"
        target = self.roots[frozen["repo"]] / frozen["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"{frozen['anchors'][0]}\n{marker}\n", encoding="utf-8")
        payload = self._inventory()
        payload["surfaces"][0]["repo"] = frozen["repo"]
        payload["surfaces"][0]["path"] = frozen["path"]
        self._write_inventory(payload)
        original = absence.read_surface
        observed = 0

        def counting_read(path: Path) -> bytes:
            nonlocal observed
            if path == target:
                observed += 1
            return original(path)

        with patch.object(absence, "read_surface", side_effect=counting_read):
            exit_code, report = self._invoke()

        self.assertEqual(0, exit_code)
        self.assertTrue(report["ratchet_clean"])
        self.assertEqual(1, observed)

    def test_binary_suffix_does_not_hide_an_unregistered_literal(self) -> None:
        marker = "review-worker-" + "protocol/v1"
        (self.roots["worker"] / "new_legacy.png").write_bytes(marker.encode("utf-8"))
        self._write_inventory(self._inventory())

        exit_code, report = self._invoke()

        self.assertEqual(1, exit_code)
        self.assertEqual("new_legacy.png", report["unexpected_surfaces"][0]["path"])

    def test_d27_control_bytes_must_remain_stable_through_observation(self) -> None:
        register = (
            self.roots["worker"]
            / "contracts"
            / "agent-first"
            / "spec-decision-register.json"
        ).resolve()
        self._write_inventory(self._inventory())
        original = absence.read_surface
        observed = 0

        def swapping_read(path: Path) -> bytes:
            nonlocal observed
            if path == register:
                observed += 1
                if observed == 2:
                    return b"tampered after validation"
            return original(path)

        with patch.object(absence, "read_surface", side_effect=swapping_read):
            exit_code, report = self._invoke()

        self.assertEqual(2, exit_code)
        self.assertEqual("environment_invalid", report["error_kind"])
        self.assertEqual(2, observed)

    def test_inventory_control_bytes_must_remain_stable_through_observation(self) -> None:
        inventory_path = self.inventory_path.resolve()
        self._write_inventory(self._inventory())
        original = absence.read_surface
        observed = 0

        def swapping_read(path: Path) -> bytes:
            nonlocal observed
            if path == inventory_path:
                observed += 1
                if observed == 2:
                    return b"tampered after validation"
            return original(path)

        with patch.object(absence, "read_surface", side_effect=swapping_read):
            exit_code, report = self._invoke()

        self.assertEqual(2, exit_code)
        self.assertEqual("environment_invalid", report["error_kind"])
        self.assertEqual(2, observed)

    def test_verifier_rejects_inventory_detached_from_control_bytes(self) -> None:
        marker = "review-worker-" + "protocol/v1"
        (self.roots["worker"] / "legacy.py").write_text(
            f"{marker}\n{marker}\n", encoding="utf-8"
        )
        disk_inventory = self._inventory()
        self._write_inventory(disk_inventory)
        inventory_raw = self.inventory_path.read_bytes()
        detached_inventory = deepcopy(disk_inventory)
        detached_inventory["surfaces"][0][
            "signature_occurrence_ceilings"
        ]["legacy-protocol"] = 3
        detached_inventory["catalog_sha256"] = catalog_sha256(detached_inventory)

        with self.assertRaises(InventoryError):
            absence.verify_legacy_absence(
                detached_inventory,
                self.workspace,
                inventory_raw=inventory_raw,
            )

    def test_production_catalog_ceiling_applies_in_a_copied_workspace(self) -> None:
        payload = deepcopy(load_inventory(absence.DEFAULT_INVENTORY))
        added = deepcopy(payload["surfaces"][-1])
        added["id"] = "worker.999-added-legacy"
        added["path"] = "copied-workspace-added-legacy.py"
        payload["surfaces"].append(added)
        self._write_inventory(payload)

        exit_code, report = self._invoke(production_catalog=True)

        self.assertEqual(2, exit_code)
        self.assertEqual("inventory_invalid", report["error_kind"])

    def test_production_inventory_id_applies_in_a_copied_workspace(self) -> None:
        payload = deepcopy(load_inventory(absence.DEFAULT_INVENTORY))
        payload["inventory_id"] = "forged-production-inventory"
        self._write_inventory(payload)

        exit_code, report = self._invoke(production_catalog=True)

        self.assertEqual(2, exit_code)
        self.assertEqual("inventory_invalid", report["error_kind"])

    def test_production_default_mode_keeps_reporting_the_frozen_baseline(self) -> None:
        payload = deepcopy(load_inventory(absence.DEFAULT_INVENTORY))
        self._write_inventory(payload)

        exit_code, report = self._invoke(production_catalog=True)

        self.assertEqual(0, exit_code)
        self.assertEqual("legacy_present", report["status"])
        self.assertTrue(report["ratchet_clean"])
        self.assertEqual([], report["indeterminate_reasons"])

    def test_production_strict_mode_rejects_frozen_baseline_self_reference(
        self,
    ) -> None:
        payload = deepcopy(load_inventory(absence.DEFAULT_INVENTORY))
        self._write_inventory(payload)

        exit_code, report = self._invoke(
            "--require-absent", production_catalog=True
        )

        self.assertEqual(2, exit_code)
        self.assertEqual("indeterminate", report["status"])
        self.assertFalse(report["legacy_absent"])
        self.assertTrue(report["require_absent"])
        self.assertTrue(report["ratchet_clean"])
        self.assertEqual([], report["failures"])
        self.assertEqual(
            [
                {
                    "code": "strict_catalog_self_reference",
                    "surface_id": "worker.004-frozen-contract-baseline",
                    "repo": "worker",
                    "path": (
                        "contracts/agent-first/"
                        "legacy-v1-contract-baseline.json"
                    ),
                }
            ],
            report["indeterminate_reasons"],
        )
        self.assertEqual(
            "present",
            next(
                item for item in report["surfaces"]
                if item["id"] == "worker.004-frozen-contract-baseline"
            )["status"],
        )

    def test_strict_indeterminate_preserves_independent_failures(self) -> None:
        payload = deepcopy(load_inventory(absence.DEFAULT_INVENTORY))
        surface = next(
            item
            for item in payload["surfaces"]
            if item["id"] != "worker.004-frozen-contract-baseline"
        )
        signature_id = next(iter(surface["signature_occurrence_ceilings"]))
        literal = next(
            item["literal"]
            for item in payload["signatures"]
            if item["id"] == signature_id
        )
        target = self.roots[surface["repo"]] / surface["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(literal, encoding="utf-8")
        (self.roots["worker"] / "strict-new-legacy.py").write_text(
            literal, encoding="utf-8"
        )
        self._write_inventory(payload)

        exit_code, report = self._invoke(
            "--require-absent", production_catalog=True
        )

        self.assertEqual(2, exit_code)
        self.assertEqual("indeterminate", report["status"])
        self.assertEqual(
            "strict_catalog_self_reference",
            report["indeterminate_reasons"][0]["code"],
        )
        self.assertIn(
            {
                "code": "legacy_surface_present",
                "surface_id": surface["id"],
            },
            report["failures"],
        )
        self.assertIn(
            {
                "code": "unexpected_legacy_surface",
                "repo": "worker",
                "path": "strict-new-legacy.py",
                "signature_id": signature_id,
            },
            report["failures"],
        )
        self.assertNotIn(
            {
                "code": "legacy_surface_present",
                "surface_id": "worker.004-frozen-contract-baseline",
            },
            report["failures"],
        )

    def test_default_catalog_ceiling_rejects_a_recomputed_expansion(self) -> None:
        payload = deepcopy(load_inventory(absence.DEFAULT_INVENTORY))
        payload["surfaces"].append(deepcopy(payload["surfaces"][-1]))
        payload["surfaces"][-1]["id"] = "worker.999-added-legacy"
        payload["catalog_sha256"] = catalog_sha256(payload)

        with self.assertRaises(InventoryError):
            absence._require_catalog_ceiling(payload, absence.DEFAULT_INVENTORY)

    def test_descriptor_target_must_equal_the_validated_lexical_path(self) -> None:
        target = self.roots["worker"] / "safe.txt"
        outside = self.workspace / "outside.txt"
        target.write_text("safe", encoding="utf-8")
        outside.write_text("outside", encoding="utf-8")

        with patch.object(
            contract_files, "_descriptor_final_path", return_value=outside
        ):
            with self.assertRaises(contract_files.BaselineEnvironmentError):
                contract_files.read_surface(target)
