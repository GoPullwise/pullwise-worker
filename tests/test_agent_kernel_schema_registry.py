from __future__ import annotations

import copy
import json
from pathlib import Path
import shutil
import tempfile
import unittest

import pullwise_worker.agent_kernel_schema_registry as schema_registry_module
from pullwise_worker.agent_kernel_canonical import canonical_sha256
from pullwise_worker.agent_kernel_schema_registry import (
    SchemaRegistry,
    SchemaRegistryError,
    SchemaValidationError,
)
from pullwise_worker.agent_kernel_schema_validation import (
    validate_instance,
    validate_schema_definition,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
CONTRACT_ROOT = REPO_ROOT / "contracts" / "agent-task" / "v1"


def _mutated(instance: object, mutation: dict[str, object]) -> object:
    result = copy.deepcopy(instance)
    assert isinstance(result, dict)
    segments = [
        part.replace("~1", "/").replace("~0", "~")
        for part in str(mutation["path"]).split("/")[1:]
    ]
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
    def test_schema_definition_subset_rejects_malformed_keyword_values(self) -> None:
        cases = (
            ({"type": "number"}, "schema_type_invalid"),
            ({"type": "string", "format": "uri"}, "schema_format_unsupported"),
            ({"type": "string", "pattern": "["}, "schema_pattern_invalid"),
            (
                {"type": "array", "minItems": 2, "maxItems": 1},
                "schema_range_invalid",
            ),
            (
                {
                    "type": "object",
                    "properties": {},
                    "required": ["missing"],
                    "additionalProperties": False,
                },
                "schema_required_unknown",
            ),
            (
                {"type": "object", "additionalProperties": "false"},
                "schema_additional_properties_invalid",
            ),
        )
        for schema, code in cases:
            with self.subTest(code=code):
                with self.assertRaisesRegex(SchemaValidationError, code):
                    validate_schema_definition(schema)

    def test_const_and_enum_use_json_identity_not_python_bool_integer_aliases(self) -> None:
        for schema in ({"const": 1}, {"enum": [1]}):
            with self.subTest(schema=schema):
                with self.assertRaisesRegex(SchemaValidationError, "mismatch"):
                    validate_instance(True, schema, resolve=lambda _: {})

    def test_utc_rfc3339_milliseconds_rejects_impossible_calendar_dates(self) -> None:
        schema = {"type": "string", "format": "utc-rfc3339-ms"}
        validate_instance(
            "2024-02-29T23:59:59.999Z", schema, resolve=lambda _: {}
        )
        for timestamp in (
            "2023-02-29T00:00:00.000Z",
            "2026-02-30T00:00:00.000Z",
            "2026-04-31T00:00:00.000Z",
        ):
            with self.subTest(timestamp=timestamp), self.assertRaisesRegex(
                SchemaValidationError, "timestamp_not_canonical"
            ):
                validate_instance(timestamp, schema, resolve=lambda _: {})

    def test_registry_verifies_schema_identity_digest_and_golden_fixtures(self) -> None:
        registry = SchemaRegistry(CONTRACT_ROOT)
        cases = []
        for path in sorted((CONTRACT_ROOT / "fixtures").glob("schema-golden*.json")):
            fixture = json.loads(path.read_text(encoding="utf-8"))
            cases.extend(fixture["cases"])

        self.assertEqual(
            {
                "actor/v1",
                "availability-ref/v1",
                "budget-entry/v1",
                "content-ref/v1",
                "effective-execution-policy/v1",
                "interaction-request/v1",
                "interaction-response/v1",
                "legacy-v1-task-mapping/v1",
                "requirement-entry/v1",
                "task-charter/v1",
                "task-record/v1",
                "task-request/v1",
                "waiver-event/v1",
            },
            set(registry.schema_ids),
        )
        self.assertEqual(set(registry.schema_ids), {
            case["schema_id"] for case in cases
        })
        for case in cases:
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

    def test_registry_rejects_symlinked_contract_artifacts(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-schema-link-") as tmp_dir:
            copied = Path(tmp_dir) / "v1-real"
            shutil.copytree(CONTRACT_ROOT, copied)
            linked_root = Path(tmp_dir) / "v1"
            linked_root.symlink_to(copied.name, target_is_directory=True)

            with self.assertRaisesRegex(SchemaRegistryError, "schema_root_invalid"):
                SchemaRegistry(linked_root)

        for label, relative in (
            ("registry", Path("schema-registry.json")),
            ("schema", Path("content-ref.schema.json")),
        ):
            with self.subTest(label=label), tempfile.TemporaryDirectory(
                prefix=f"agent-{label}-link-"
            ) as tmp_dir:
                copied = Path(tmp_dir) / "v1"
                shutil.copytree(CONTRACT_ROOT, copied)
                linked = copied / relative
                target = linked.with_name(f"{linked.name}.target")
                linked.rename(target)
                linked.symlink_to(target.name)

                with self.assertRaisesRegex(
                    SchemaRegistryError, f"schema_file_not_regular: {linked.name}"
                ):
                    SchemaRegistry(copied)

        with tempfile.TemporaryDirectory(prefix="agent-fixture-link-") as tmp_dir:
            copied = Path(tmp_dir) / "v1"
            shutil.copytree(CONTRACT_ROOT, copied)
            linked = copied / "fixtures" / "schema-golden.json"
            target = linked.with_name(f"{linked.name}.target")
            linked.rename(target)
            linked.symlink_to(target.name)

            with self.assertRaisesRegex(
                SchemaRegistryError, f"schema_file_not_regular: {linked.name}"
            ):
                schema_registry_module._read_regular(linked)

    def test_registry_rejects_unknown_schema_reference(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-schema-ref-") as tmp_dir:
            copied = Path(tmp_dir) / "v1"
            shutil.copytree(CONTRACT_ROOT, copied)
            schema_path = copied / "availability-ref.schema.json"
            schema = json.loads(schema_path.read_text(encoding="utf-8"))
            schema["oneOf"][0]["properties"]["ref"]["$ref"] = "future/v9"
            schema_path.write_text(
                json.dumps(schema, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            manifest_path = copied / "schema-registry.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            for entry in manifest["schemas"]:
                if entry["schema_id"] == "availability-ref/v1":
                    entry["sha256"] = canonical_sha256(schema)
                    break
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                SchemaRegistryError,
                "schema_reference_unknown: availability-ref/v1: future/v9",
            ):
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
