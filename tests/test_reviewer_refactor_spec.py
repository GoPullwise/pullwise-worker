from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC_DIR = ROOT / "docs/reviewer-refactor"
MANIFEST_REL = Path("docs/reviewer-refactor/spec-manifest.json")
CARD_IDS = (
    "COL-0D",
    "COL-0F",
    "GOV-0A",
    "EVD-0",
    "GOV-0B",
    "EVD-1",
    "CON-0",
    "BEN-0",
    "SKILL-1",
    "RUN-1",
    "RUN-2",
    "RES-1",
    "PUB-1",
    "BEN-1",
    "SRV-1",
    "CON-1",
    "SRV-2",
    "WEB-1",
    "ADM-1",
    "CUT-1",
    "REL-1",
    "CAN-5",
    "CAN-25",
    "PROM-1",
)
COMMAND_FIELDS = (
    "red_commands",
    "green_commands",
    "focused_commands",
    "full_commands",
    "ci_commands",
)


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


class ReviewerRefactorSpecTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cards = load_json(SPEC_DIR / "execution-cards.json")
        self.by_id = {card["id"]: card for card in self.cards["cards"]}

    def run_verifier(self, root: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                "-I",
                "-B",
                str(root / "docs/reviewer-refactor/verify_spec.py"),
                "--repo-root",
                str(root),
            ],
            cwd=root,
            text=True,
            capture_output=True,
            check=False,
        )

    def copy_manifest_unit(self, target: Path) -> dict:
        manifest = load_json(ROOT / MANIFEST_REL)
        for entry in manifest["files"]:
            source = ROOT / entry["path"]
            destination = target / entry["path"]
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
        destination = target / MANIFEST_REL
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / MANIFEST_REL, destination)
        return manifest

    @staticmethod
    def refresh_manifest_entry(root: Path, relative_path: str) -> None:
        manifest_path = root / MANIFEST_REL
        manifest = load_json(manifest_path)
        data = (root / relative_path).read_bytes()
        entry = next(item for item in manifest["files"] if item["path"] == relative_path)
        entry["size_bytes"] = len(data)
        entry["sha256"] = hashlib.sha256(data).hexdigest()
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )

    def test_agent_entry_is_single_inert_start_point(self) -> None:
        entry = load_json(SPEC_DIR / "agent-entry.json")
        self.assertEqual("pullwise-reviewer-refactor/v1", entry["spec_id"])
        self.assertEqual("2026-08-06-r4", entry["spec_version"])
        self.assertEqual("PROPOSED_INERT", entry["activation_state"])
        self.assertEqual("NOT_AUTHORIZED", entry["authority_state"])
        self.assertEqual(1, entry["current_generation"])
        self.assertEqual("COL-0D", entry["next_card_id"])
        self.assertEqual(
            ["verify-spec", "inspect-current-gates"],
            entry["allowed_action_ids"],
        )

    def test_cards_cover_bootstrap_and_release_lifecycle(self) -> None:
        self.assertEqual("inert_catalog", self.cards["profile"])
        self.assertEqual(CARD_IDS, tuple(self.by_id))
        self.assertEqual("0A", self.by_id["COL-0D"]["stage"])
        self.assertEqual(["COL-0D"], self.by_id["COL-0F"]["dependencies"])
        self.assertEqual(["COL-0F"], self.by_id["GOV-0A"]["dependencies"])
        self.assertEqual("E", self.by_id["REL-1"]["stage"])
        self.assertEqual("E", self.by_id["CAN-5"]["stage"])
        self.assertEqual("E", self.by_id["CAN-25"]["stage"])
        self.assertEqual("F", self.by_id["PROM-1"]["stage"])

    def test_generation_one_is_non_executable(self) -> None:
        self.assertEqual(1, self.cards["generation"])
        self.assertIsNone(self.cards["transition"]["from_generation"])
        self.assertIsNone(self.cards["transition"]["from_manifest_sha256"])
        self.assertEqual("absent", self.cards["transition"]["command_binding_state"])
        for card in self.cards["cards"]:
            self.assertEqual("blocked", card["execution_state"], card["id"])
            self.assertEqual("NOT_AUTHORIZED", card["authority_state"], card["id"])
            self.assertTrue(card["blocking_predicates"], card["id"])
            for field in COMMAND_FIELDS:
                self.assertEqual([], card[field], f"{card['id']}:{field}")

    def test_cards_bind_real_cross_repo_surfaces(self) -> None:
        required = {
            "RUN-1": {
                ("worker", "pullwise_worker/reviewer_runtime/__init__.py"),
                ("worker", "pullwise_worker/reviewer_runtime/types.py"),
                ("worker", "pullwise_worker/reviewer_runtime/validation_service.py"),
                ("worker", "tests/test_reviewer_validation_service.py"),
            },
            "RUN-2": {
                ("worker", "scripts/run_reviewer_candidate.py"),
                ("worker", "tests/test_reviewer_model_fs_policy.py"),
                ("worker", "tests/test_reviewer_runtime_policy.py"),
            },
            "WEB-1": {
                ("web", "src/api/pullwise.js"),
                ("web", "src/lib/pullwise-data.js"),
                ("web", "src/screens/flow.jsx"),
                ("web", "src/screens/issues.jsx"),
            },
            "ADM-1": {
                ("admin", "src/api/pullwise.js"),
                ("admin", "src/screens/plans.jsx"),
                ("admin", "src/screens/settings.jsx"),
            },
            "SRV-2": {
                ("server", "pullwise_server/db.py"),
                ("server", "pullwise_server/_app_part_04_scan_audit_bundle.py"),
                ("server", "pullwise_server/_app_part_05_worker_results.py"),
                ("server", "pullwise_server/_app_part_10_handler_main.py"),
            },
            "CUT-1": {
                ("worker", "pullwise_worker/main.py"),
                ("worker", "pullwise_worker/review_worker_v1.py"),
                ("server", "tests/test_review_worker_protocol_v1.py"),
                ("web", "contract-package-pin.json"),
            },
        }
        for card_id, expected in required.items():
            paths = {
                (item["repo_id"], item["path"])
                for item in self.by_id[card_id]["write_set"]
            }
            self.assertTrue(expected <= paths, f"{card_id}: {sorted(expected - paths)}")
        repo_roots = {
            "worker": ROOT,
            "server": ROOT.parent / "pullwise-server",
            "web": ROOT.parent / "pullwise-web",
            "admin": ROOT.parent / "pullwise-admin",
        }
        for card_id in ("WEB-1", "ADM-1", "SRV-2", "CUT-1"):
            for repo_id, relative in required[card_id]:
                self.assertTrue(
                    (repo_roots[repo_id] / relative).exists(),
                    f"{card_id}:{repo_id}:{relative}",
                )
        all_paths = [
            item["path"]
            for card in self.cards["cards"]
            for item in card["write_set"]
        ]
        self.assertFalse(any("release-change-set" in path for path in all_paths))

    def test_deferred_outputs_are_typed_not_fake_paths(self) -> None:
        for card in self.cards["cards"]:
            for output in card["outputs"]:
                self.assertIn("path_binding_artifact", output)
                has_path = output["path"] is not None
                has_binding = output["path_binding_artifact"] is not None
                self.assertNotEqual(has_path, has_binding, f"{card['id']}:{output['artifact_id']}")
                if has_path:
                    self.assertNotIn("{", output["path"])
                    self.assertNotIn("release/", output["path"])

    def test_schema_is_executed_by_verifier(self) -> None:
        with tempfile.TemporaryDirectory(prefix="reviewer-r4-schema-") as directory:
            target = Path(directory)
            self.copy_manifest_unit(target)
            relative = "docs/reviewer-refactor/execution-cards.json"
            path = target / relative
            value = load_json(path)
            value["cards"][0]["owner_role"] = ""
            path.write_text(
                json.dumps(value, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
                newline="\n",
            )
            self.refresh_manifest_entry(target, relative)
            result = self.run_verifier(target)
            self.assertEqual(1, result.returncode, result.stdout + result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual("schema.minLength", payload["reason_code"])

    def test_verifier_sources_respect_worker_line_gate(self) -> None:
        sources = sorted(SPEC_DIR.glob("*verifier*.py")) + [SPEC_DIR / "verify_spec.py"]
        self.assertGreaterEqual(len(set(sources)), 2)
        for path in set(sources):
            line_count = len(path.read_text(encoding="utf-8").splitlines())
            self.assertLessEqual(line_count, 400, f"{path.name}: {line_count}")


if __name__ == "__main__":
    unittest.main()
