from __future__ import annotations

import copy
import json
from pathlib import Path
import shutil
import tempfile
import unittest

from pullwise_worker.agent_kernel_canonical import canonical_sha256
from pullwise_worker.agent_kernel_schema_registry import (
    SchemaRegistry,
    SchemaRegistryError,
    SchemaValidationError,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
CONTRACT_ROOT = REPO_ROOT / "contracts" / "agent-task" / "v1"


def _mutated(instance: object, mutation: dict[str, object]) -> object:
    result = copy.deepcopy(instance)
    assert isinstance(result, dict)
    segments = [part.replace("~1", "/").replace("~0", "~") for part in str(mutation["path"]).split("/")[1:]]
    target: object = result
    for segment in segments[:-1]:
        assert isinstance(target, dict)
        target = target[segment]
    assert isinstance(target, dict)
    if isinstance(mutation.get("repeat"), dict):
        repeat = mutation["repeat"]
        value = str(repeat["text"]) * int(repeat["count"])
    elif "unsafe_integer" in mutation:
        value = int(str(mutation["unsafe_integer"]))
    else:
        value = mutation.get("value")
    target[segments[-1]] = value
    return result


class AgentKernelSchemaRegistryTest(unittest.TestCase):
    def test_registry_verifies_schema_identity_digest_and_golden_fixtures(self) -> None:
        registry = SchemaRegistry(CONTRACT_ROOT)
        fixture = json.loads(
            (CONTRACT_ROOT / "fixtures" / "schema-golden.json").read_text(
                encoding="utf-8"
            )
        )

        self.assertEqual(
            {
                "actor/v1",
                "availability-ref/v1",
                "budget-entry/v1",
                "content-ref/v1",
                "effective-execution-policy/v1",
                "legacy-v1-task-mapping/v1",
                "requirement-entry/v1",
                "task-record/v1",
                "task-request/v1",
            },
            set(registry.schema_ids),
        )
        self.assertEqual(set(registry.schema_ids), {
            case["schema_id"] for case in fixture["cases"]
        })
        for case in fixture["cases"]:
            with self.subTest(schema_id=case["schema_id"]):
                registry.validate(case["schema_id"], case["valid"])
                self.assertEqual(
                    case["canonical_sha256"], canonical_sha256(case["valid"])
                )
                self.assertEqual(
                    {"unknown_field", "enum", "size"},
                    {invalid["category"] for invalid in case["invalid_mutations"]},
                )
                for invalid in case["invalid_mutations"]:
                    with self.subTest(category=invalid["category"]), self.assertRaises(
                        SchemaValidationError
                    ):
                        registry.validate(
                            case["schema_id"], _mutated(case["valid"], invalid)
                        )

    def test_registry_rejects_schema_file_drift(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-schema-drift-") as tmp_dir:
            copied = Path(tmp_dir) / "v1"
            shutil.copytree(CONTRACT_ROOT, copied)
            schema_path = copied / "content-ref.schema.json"
            schema = json.loads(schema_path.read_text(encoding="utf-8"))
            schema["properties"]["size_bytes"]["maximum"] -= 1
            schema_path.write_text(
                json.dumps(schema, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(SchemaRegistryError, "schema_digest_mismatch"):
                SchemaRegistry(copied)

    def test_actor_session_kind_union_is_strict(self) -> None:
        registry = SchemaRegistry(CONTRACT_ROOT)
        owner = {
            "schema_id": "actor/v1",
            "kind": "task_owner",
            "id": "owner_" + "1" * 32,
            "session_id": "sess_" + "2" * 32,
        }
        registry.validate("actor/v1", owner)
        cases = (
            {**owner, "session_id": None},
            {
                "schema_id": "actor/v1",
                "kind": "worker_control",
                "id": "worker-1",
                "session_id": "sess_" + "2" * 32,
            },
        )
        for instance in cases:
            with self.subTest(instance=instance), self.assertRaises(
                SchemaValidationError
            ):
                registry.validate("actor/v1", instance)

    def test_availability_ref_resolves_content_ref_and_rejects_bare_null(self) -> None:
        registry = SchemaRegistry(CONTRACT_ROOT)
        ref = {
            "schema_id": "content-ref/v1",
            "artifact_id": "art_" + "3" * 32,
            "sha256": "4" * 64,
            "size_bytes": 0,
            "media_type": "application/octet-stream",
            "content_schema_id": "opaque-bytes/v1",
            "encoding": "binary",
        }
        registry.validate(
            "availability-ref/v1", {"availability": "available", "ref": ref}
        )
        for instance in (
            None,
            {"availability": "available", "ref": None},
            {"availability": "unavailable", "reason_code": ""},
        ):
            with self.subTest(instance=instance), self.assertRaises(
                SchemaValidationError
            ):
                registry.validate("availability-ref/v1", instance)

    def test_validate_rejects_unknown_schema_and_does_not_mutate_instance(self) -> None:
        registry = SchemaRegistry(CONTRACT_ROOT)
        instance = {
            "schema_id": "content-ref/v1",
            "artifact_id": "art_" + "5" * 32,
            "sha256": "6" * 64,
            "size_bytes": 1,
            "media_type": "text/plain",
            "content_schema_id": "plain-text/v1",
            "encoding": "utf-8",
        }
        original = copy.deepcopy(instance)
        registry.validate("content-ref/v1", instance)
        self.assertEqual(original, instance)
        with self.assertRaisesRegex(SchemaRegistryError, "schema_unknown"):
            registry.validate("future/v9", instance)


if __name__ == "__main__":
    unittest.main()
