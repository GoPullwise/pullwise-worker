from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from pathlib import Path, PurePosixPath
from typing import Any

from reviewer_spec_model import SAFE_INT_MAX


class SpecError(Exception):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def fail(code: str, detail: str) -> None:
    raise SpecError(code, detail)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _no_float(value: str) -> Any:
    fail("json.float_forbidden", value)


def _no_constant(value: str) -> Any:
    fail("json.constant_forbidden", value)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            fail("json.duplicate_key", key)
        result[key] = value
    return result


def load_json(path: Path) -> Any:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        fail("json.read_failed", f"{path}: {exc}")
    try:
        return json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_float=_no_float,
            parse_constant=_no_constant,
        )
    except SpecError:
        raise
    except json.JSONDecodeError as exc:
        fail("json.invalid", f"{path}:{exc.lineno}:{exc.colno}")


def validate_profile(value: Any, location: str = "$") -> None:
    if value is None or isinstance(value, bool):
        return
    if isinstance(value, int):
        if abs(value) > SAFE_INT_MAX:
            fail("json.unsafe_integer", location)
        return
    if isinstance(value, str):
        if unicodedata.normalize("NFC", value) != value:
            fail("json.non_nfc_string", location)
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            validate_profile(item, f"{location}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not key.isascii():
                fail("json.non_ascii_key", f"{location}.{key}")
            validate_profile(item, f"{location}.{key}")
        return
    fail("json.unsupported_type", location)


def canonical_rel(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or chr(92) in value
        or value.startswith("/")
    ):
        fail("path.noncanonical", repr(value))
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in ("", ".", "..") for part in path.parts):
        fail("path.noncanonical", value)
    if path.as_posix() != value or unicodedata.normalize("NFC", value) != value:
        fail("path.noncanonical", value)
    return value


def exact_keys(value: dict[str, Any], expected: set[str], location: str) -> None:
    actual = set(value)
    if actual != expected:
        fail(
            "schema.keys",
            f"{location}: missing={sorted(expected-actual)} extra={sorted(actual-expected)}",
        )


def _resolve_ref(root: dict[str, Any], reference: str) -> dict[str, Any]:
    if not reference.startswith("#/"):
        fail("schema.ref", reference)
    value: Any = root
    for raw in reference[2:].split("/"):
        key = raw.replace("~1", "/").replace("~0", "~")
        if not isinstance(value, dict) or key not in value:
            fail("schema.ref", reference)
        value = value[key]
    if not isinstance(value, dict):
        fail("schema.ref", reference)
    return value


def _matches_type(value: Any, type_name: str) -> bool:
    if type_name == "null":
        return value is None
    if type_name == "object":
        return isinstance(value, dict)
    if type_name == "array":
        return isinstance(value, list)
    if type_name == "string":
        return isinstance(value, str)
    if type_name == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if type_name == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if type_name == "boolean":
        return isinstance(value, bool)
    fail("schema.unsupported_type", type_name)


def _unique_marker(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def validate_schema(
    value: Any,
    schema: dict[str, Any],
    root_schema: dict[str, Any] | None = None,
    location: str = "$",
) -> None:
    root = schema if root_schema is None else root_schema
    if "$ref" in schema:
        validate_schema(value, _resolve_ref(root, schema["$ref"]), root, location)
        return
    if "const" in schema and value != schema["const"]:
        fail("schema.const", location)
    if "enum" in schema and value not in schema["enum"]:
        fail("schema.enum", location)
    declared = schema.get("type")
    if declared is not None:
        names = [declared] if isinstance(declared, str) else declared
        if not isinstance(names, list) or not any(
            _matches_type(value, name) for name in names
        ):
            fail("schema.type", location)
    if isinstance(value, dict):
        required = schema.get("required", [])
        missing = [key for key in required if key not in value]
        if missing:
            fail("schema.required", f"{location}:{missing}")
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            extra = sorted(set(value) - set(properties))
            if extra:
                fail("schema.additionalProperties", f"{location}:{extra}")
        for key, item in value.items():
            child_schema = properties.get(key)
            if child_schema is not None:
                validate_schema(item, child_schema, root, f"{location}.{key}")
    if isinstance(value, list):
        minimum = schema.get("minItems")
        maximum = schema.get("maxItems")
        if minimum is not None and len(value) < minimum:
            fail("schema.minItems", location)
        if maximum is not None and len(value) > maximum:
            fail("schema.maxItems", location)
        if schema.get("uniqueItems"):
            markers = [_unique_marker(item) for item in value]
            if len(markers) != len(set(markers)):
                fail("schema.uniqueItems", location)
        item_schema = schema.get("items")
        if item_schema is not None:
            for index, item in enumerate(value):
                validate_schema(item, item_schema, root, f"{location}[{index}]")
    if isinstance(value, str):
        minimum = schema.get("minLength")
        maximum = schema.get("maxLength")
        if minimum is not None and len(value) < minimum:
            fail("schema.minLength", location)
        if maximum is not None and len(value) > maximum:
            fail("schema.maxLength", location)
        pattern = schema.get("pattern")
        if pattern is not None and re.search(pattern, value) is None:
            fail("schema.pattern", location)
    if isinstance(value, int) and not isinstance(value, bool):
        minimum = schema.get("minimum")
        maximum = schema.get("maximum")
        if minimum is not None and value < minimum:
            fail("schema.minimum", location)
        if maximum is not None and value > maximum:
            fail("schema.maximum", location)
