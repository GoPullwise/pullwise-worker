"""Small fail-closed validator for the checked-in Agent Task schema subset."""

from __future__ import annotations

from datetime import date
import re
from typing import Callable

from .agent_kernel_canonical import CanonicalizationError, canonical_bytes


SUPPORTED_KEYWORDS = {
    "$schema",
    "$id",
    "$ref",
    "type",
    "const",
    "enum",
    "required",
    "properties",
    "additionalProperties",
    "oneOf",
    "items",
    "minItems",
    "maxItems",
    "uniqueItems",
    "minLength",
    "maxLength",
    "pattern",
    "minimum",
    "maximum",
    "format",
    "x-pullwise-ascii",
    "x-pullwise-max-utf8-bytes",
    "x-pullwise-sorted-unique",
}
RFC3339_MILLISECONDS = re.compile(
    r"^[0-9]{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12][0-9]|3[01])"
    r"T(?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]\.[0-9]{3}Z$"
)
SUPPORTED_TYPES = {"null", "boolean", "integer", "string", "array", "object"}
SUPPORTED_FORMATS = {"utc-rfc3339-ms"}


class SchemaValidationError(ValueError):
    def __init__(self, code: str, path: str = "$", detail: str = "") -> None:
        self.code = code
        self.path = path
        self.detail = detail
        message = f"{code} at {path}"
        if detail:
            message += f": {detail}"
        super().__init__(message)


def validate_schema_definition(schema: object, path: str = "$schema") -> None:
    if not isinstance(schema, dict):
        raise SchemaValidationError("schema_definition_not_object", path)
    unknown = sorted(set(schema) - SUPPORTED_KEYWORDS)
    if unknown:
        raise SchemaValidationError("schema_keyword_unsupported", path, str(unknown))
    for keyword in ("$schema", "$id", "$ref"):
        value = schema.get(keyword)
        if value is not None and (not isinstance(value, str) or not value):
            raise SchemaValidationError("schema_text_keyword_invalid", path, keyword)
    declared_type = schema.get("type")
    if declared_type is not None:
        names = declared_type if isinstance(declared_type, list) else [declared_type]
        if (
            not names
            or any(not isinstance(name, str) for name in names)
            or len(set(names)) != len(names)
            or not set(names) <= SUPPORTED_TYPES
        ):
            raise SchemaValidationError("schema_type_invalid", path)
    if "const" in schema:
        _schema_value_bytes(schema["const"], f"{path}.const")
    enum = schema.get("enum")
    if enum is not None:
        if not isinstance(enum, list) or not enum:
            raise SchemaValidationError("schema_enum_invalid", path)
        encoded = [_schema_value_bytes(value, f"{path}.enum") for value in enum]
        if len(encoded) != len(set(encoded)):
            raise SchemaValidationError("schema_enum_duplicate", path)
    properties = schema.get("properties")
    if properties is not None:
        if not isinstance(properties, dict):
            raise SchemaValidationError("schema_properties_invalid", path)
        for name, child in properties.items():
            if not isinstance(name, str):
                raise SchemaValidationError("schema_property_name_invalid", path)
            validate_schema_definition(child, f"{path}.properties.{name}")
    required = schema.get("required")
    if required is not None:
        if (
            not isinstance(required, list)
            or any(not isinstance(name, str) for name in required)
            or len(required) != len(set(required))
        ):
            raise SchemaValidationError("schema_required_invalid", path)
        if isinstance(properties, dict) and not set(required) <= set(properties):
            raise SchemaValidationError("schema_required_unknown", path)
    for keyword in ("oneOf",):
        variants = schema.get(keyword)
        if variants is not None:
            if not isinstance(variants, list) or not variants:
                raise SchemaValidationError("schema_union_invalid", path)
            for index, child in enumerate(variants):
                validate_schema_definition(child, f"{path}.{keyword}[{index}]")
    items = schema.get("items")
    if items is not None:
        if not isinstance(items, dict):
            raise SchemaValidationError("schema_items_invalid", path)
        validate_schema_definition(items, f"{path}.items")
    additional = schema.get("additionalProperties")
    if isinstance(additional, dict):
        validate_schema_definition(additional, f"{path}.additionalProperties")
    elif additional is not None and not isinstance(additional, bool):
        raise SchemaValidationError("schema_additional_properties_invalid", path)
    for keyword in (
        "minItems",
        "maxItems",
        "minLength",
        "maxLength",
        "x-pullwise-max-utf8-bytes",
    ):
        value = schema.get(keyword)
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int) or value < 0
        ):
            raise SchemaValidationError("schema_integer_keyword_invalid", path, keyword)
    for minimum, maximum in (
        ("minItems", "maxItems"),
        ("minLength", "maxLength"),
        ("minimum", "maximum"),
    ):
        lower = schema.get(minimum)
        upper = schema.get(maximum)
        for keyword, value in ((minimum, lower), (maximum, upper)):
            if keyword in {"minimum", "maximum"} and value is not None and (
                isinstance(value, bool) or not isinstance(value, int)
            ):
                raise SchemaValidationError(
                    "schema_integer_keyword_invalid", path, keyword
                )
        if lower is not None and upper is not None and lower > upper:
            raise SchemaValidationError("schema_range_invalid", path)
    for keyword in (
        "uniqueItems",
        "x-pullwise-ascii",
        "x-pullwise-sorted-unique",
    ):
        value = schema.get(keyword)
        if value is not None and not isinstance(value, bool):
            raise SchemaValidationError("schema_boolean_keyword_invalid", path, keyword)
    pattern = schema.get("pattern")
    if pattern is not None:
        if not isinstance(pattern, str):
            raise SchemaValidationError("schema_pattern_invalid", path)
        try:
            re.compile(pattern)
        except re.error as exc:
            raise SchemaValidationError("schema_pattern_invalid", path) from exc
    format_name = schema.get("format")
    if format_name is not None and format_name not in SUPPORTED_FORMATS:
        raise SchemaValidationError("schema_format_unsupported", path)


def _schema_value_bytes(value: object, path: str) -> bytes:
    try:
        return canonical_bytes(value)
    except CanonicalizationError as exc:
        raise SchemaValidationError("schema_value_not_canonical", path, exc.code) from exc


def validate_instance(
    instance: object,
    schema: dict[str, object],
    *,
    resolve: Callable[[str], dict[str, object]],
    path: str = "$",
) -> None:
    reference = schema.get("$ref")
    if reference is not None:
        if not isinstance(reference, str):
            raise SchemaValidationError("schema_ref_invalid", path)
        validate_instance(instance, resolve(reference), resolve=resolve, path=path)

    if "const" in schema and canonical_bytes(instance) != canonical_bytes(schema["const"]):
        raise SchemaValidationError("const_mismatch", path)
    enum = schema.get("enum")
    if enum is not None:
        encoded_instance = canonical_bytes(instance)
        if not isinstance(enum, list) or encoded_instance not in {
            canonical_bytes(value) for value in enum
        }:
            raise SchemaValidationError("enum_mismatch", path)

    declared_type = schema.get("type")
    if declared_type is not None and not _matches_type(instance, declared_type):
        raise SchemaValidationError(
            "type_mismatch", path, f"expected {declared_type!r}"
        )

    one_of = schema.get("oneOf")
    if one_of is not None:
        matches = 0
        for variant in one_of:
            try:
                validate_instance(instance, variant, resolve=resolve, path=path)
            except SchemaValidationError:
                continue
            matches += 1
        if matches != 1:
            raise SchemaValidationError("one_of_mismatch", path, f"matches={matches}")

    if isinstance(instance, dict):
        _validate_object(instance, schema, resolve=resolve, path=path)
    elif isinstance(instance, list):
        _validate_array(instance, schema, resolve=resolve, path=path)
    elif isinstance(instance, str):
        _validate_string(instance, schema, path=path)
    elif isinstance(instance, int) and not isinstance(instance, bool):
        _validate_integer(instance, schema, path=path)


def _matches_type(instance: object, declared: object) -> bool:
    names = declared if isinstance(declared, list) else [declared]
    if not names or any(not isinstance(name, str) for name in names):
        return False
    return any(
        {
            "null": instance is None,
            "boolean": isinstance(instance, bool),
            "integer": isinstance(instance, int) and not isinstance(instance, bool),
            "string": isinstance(instance, str),
            "array": isinstance(instance, list),
            "object": isinstance(instance, dict),
        }.get(name, False)
        for name in names
    )


def _validate_object(
    instance: dict[object, object],
    schema: dict[str, object],
    *,
    resolve: Callable[[str], dict[str, object]],
    path: str,
) -> None:
    properties = schema.get("properties", {})
    if not isinstance(properties, dict):
        raise SchemaValidationError("schema_properties_invalid", path)
    required = schema.get("required", [])
    if not isinstance(required, list) or any(
        not isinstance(name, str) for name in required
    ):
        raise SchemaValidationError("schema_required_invalid", path)
    missing = [name for name in required if name not in instance]
    if missing:
        raise SchemaValidationError("required_property_missing", path, str(missing))
    extra = [name for name in instance if name not in properties]
    additional = schema.get("additionalProperties", True)
    if extra and additional is False:
        raise SchemaValidationError("additional_property_forbidden", path, str(extra))
    for name, value in instance.items():
        child_path = f"{path}.{name}"
        if name in properties:
            validate_instance(value, properties[name], resolve=resolve, path=child_path)
        elif isinstance(additional, dict):
            validate_instance(value, additional, resolve=resolve, path=child_path)


def _validate_array(
    instance: list[object],
    schema: dict[str, object],
    *,
    resolve: Callable[[str], dict[str, object]],
    path: str,
) -> None:
    minimum = schema.get("minItems")
    maximum = schema.get("maxItems")
    if isinstance(minimum, int) and len(instance) < minimum:
        raise SchemaValidationError("array_too_short", path)
    if isinstance(maximum, int) and len(instance) > maximum:
        raise SchemaValidationError("array_too_long", path)
    encoded = [canonical_bytes(value) for value in instance]
    if schema.get("uniqueItems") is True and len(set(encoded)) != len(encoded):
        raise SchemaValidationError("array_items_not_unique", path)
    if schema.get("x-pullwise-sorted-unique") is True and encoded != sorted(set(encoded)):
        raise SchemaValidationError("array_items_not_sorted_unique", path)
    items = schema.get("items")
    if isinstance(items, dict):
        for index, value in enumerate(instance):
            validate_instance(value, items, resolve=resolve, path=f"{path}[{index}]")


def _validate_string(
    instance: str, schema: dict[str, object], *, path: str
) -> None:
    minimum = schema.get("minLength")
    maximum = schema.get("maxLength")
    if isinstance(minimum, int) and len(instance) < minimum:
        raise SchemaValidationError("string_too_short", path)
    if isinstance(maximum, int) and len(instance) > maximum:
        raise SchemaValidationError("string_too_long", path)
    byte_limit = schema.get("x-pullwise-max-utf8-bytes")
    if isinstance(byte_limit, int) and len(instance.encode("utf-8")) > byte_limit:
        raise SchemaValidationError("string_utf8_too_long", path)
    if schema.get("x-pullwise-ascii") is True:
        try:
            instance.encode("ascii")
        except UnicodeEncodeError as exc:
            raise SchemaValidationError("string_not_ascii", path) from exc
    pattern = schema.get("pattern")
    if isinstance(pattern, str) and re.search(pattern, instance) is None:
        raise SchemaValidationError("string_pattern_mismatch", path)
    if schema.get("format") == "utc-rfc3339-ms" and not RFC3339_MILLISECONDS.fullmatch(
        instance
    ):
        raise SchemaValidationError("timestamp_not_canonical", path)
    if schema.get("format") == "utc-rfc3339-ms":
        try:
            date.fromisoformat(instance[:10])
        except ValueError as exc:
            raise SchemaValidationError("timestamp_not_canonical", path) from exc


def _validate_integer(
    instance: int, schema: dict[str, object], *, path: str
) -> None:
    minimum = schema.get("minimum")
    maximum = schema.get("maximum")
    if isinstance(minimum, int) and instance < minimum:
        raise SchemaValidationError("integer_below_minimum", path)
    if isinstance(maximum, int) and instance > maximum:
        raise SchemaValidationError("integer_above_maximum", path)
