"""Pullwise JCS Profile 1 canonical JSON primitives."""

from __future__ import annotations

import hashlib
import json
import unicodedata
from typing import Any


MAX_SAFE_INTEGER = 2**53 - 1


class CanonicalizationError(ValueError):
    """A value cannot be represented by Pullwise JCS Profile 1."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


def _validated_string(value: str, *, is_key: bool = False) -> None:
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise CanonicalizationError("string_not_utf8") from exc
    if unicodedata.normalize("NFC", value) != value:
        raise CanonicalizationError("string_not_nfc")
    if is_key:
        try:
            value.encode("ascii", errors="strict")
        except UnicodeEncodeError as exc:
            raise CanonicalizationError("object_key_not_ascii") from exc


def _validate_profile(value: object) -> None:
    if value is None or isinstance(value, bool):
        return
    if isinstance(value, int):
        if not -MAX_SAFE_INTEGER <= value <= MAX_SAFE_INTEGER:
            raise CanonicalizationError("integer_out_of_range")
        return
    if isinstance(value, float):
        raise CanonicalizationError("float_not_supported")
    if isinstance(value, str):
        _validated_string(value)
        return
    if isinstance(value, list):
        for item in value:
            _validate_profile(item)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise CanonicalizationError("object_key_not_string")
            _validated_string(key, is_key=True)
            _validate_profile(item)
        return
    raise CanonicalizationError("json_type_not_supported", type(value).__name__)


def canonical_bytes(value: object, *, digest_field: str | None = None) -> bytes:
    """Return deterministic UTF-8 bytes for a validated profile value."""

    material = value
    if digest_field is not None:
        if not isinstance(value, dict):
            raise CanonicalizationError("digest_container_not_object")
        material = {key: item for key, item in value.items() if key != digest_field}
    _validate_profile(material)
    try:
        text = json.dumps(
            material,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return text.encode("utf-8", errors="strict")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise CanonicalizationError("canonical_json_encoding_failed") from exc


def canonical_sha256(value: object, *, digest_field: str | None = None) -> str:
    return hashlib.sha256(canonical_bytes(value, digest_field=digest_field)).hexdigest()


def _reject_float(value: str) -> float:
    raise CanonicalizationError("float_not_supported", value)


def _reject_constant(value: str) -> None:
    raise CanonicalizationError("non_finite_number", value)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CanonicalizationError("duplicate_object_key", key)
        result[key] = value
    return result


def load_strict_json(payload: bytes | str) -> object:
    """Parse JSON without accepting duplicate keys, floats, or invalid UTF-8."""

    if isinstance(payload, bytes):
        try:
            text = payload.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise CanonicalizationError("json_not_utf8") from exc
    elif isinstance(payload, str):
        text = payload
    else:
        raise CanonicalizationError("json_input_type_invalid")
    try:
        value = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_float=_reject_float,
            parse_constant=_reject_constant,
        )
    except CanonicalizationError:
        raise
    except json.JSONDecodeError as exc:
        raise CanonicalizationError("json_invalid", str(exc)) from exc
    _validate_profile(value)
    return value
