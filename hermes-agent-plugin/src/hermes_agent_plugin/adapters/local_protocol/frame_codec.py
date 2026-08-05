"""Bounded JSON frame codec shared by Local Gateway transports."""

from __future__ import annotations

import json
import math
from typing import Any

MAX_FRAME_BYTES = 262_144
MAX_STRING_BYTES = 131_072
MAX_NESTING_DEPTH = 32
MAX_OBJECT_FIELDS = 1_024
MAX_ARRAY_ITEMS = 1_024


_ERROR_MESSAGES = {
    "frame_too_large": "frame exceeds the protocol size limit",
    "invalid_frame_type": "frame must be text or bytes",
    "invalid_utf8": "frame is not valid UTF-8",
    "invalid_json": "frame is not valid JSON",
    "top_level_not_object": "frame must be a JSON object",
    "nul_not_allowed": "JSON strings must not contain NUL",
    "lone_surrogate": "JSON strings must not contain lone surrogates",
    "string_too_long": "JSON string exceeds the protocol size limit",
    "nesting_too_deep": "JSON nesting exceeds the protocol limit",
    "too_many_fields": "JSON object exceeds the protocol field limit",
    "too_many_array_items": "JSON array exceeds the protocol item limit",
    "invalid_json_value": "frame contains a value that JSON cannot encode",
}


class FrameCodecError(ValueError):
    """Stable, body-free protocol rejection from the shared frame codec."""

    def __init__(self, category: str) -> None:
        self.category = category
        super().__init__(_ERROR_MESSAGES[category])


def _raise(category: str) -> None:
    raise FrameCodecError(category)


def _validate_string(value: str) -> None:
    if "\x00" in value:
        _raise("nul_not_allowed")
    if any("\ud800" <= character <= "\udfff" for character in value):
        _raise("lone_surrogate")
    if len(value.encode("utf-8")) > MAX_STRING_BYTES:
        _raise("string_too_long")


def _validate_value(frame: Any) -> None:
    if not isinstance(frame, dict):
        _raise("top_level_not_object")

    pending: list[tuple[Any, int]] = [(frame, 1)]
    while pending:
        value, depth = pending.pop()
        if isinstance(value, dict):
            if depth > MAX_NESTING_DEPTH:
                _raise("nesting_too_deep")
            if len(value) > MAX_OBJECT_FIELDS:
                _raise("too_many_fields")
            for key, item in value.items():
                if not isinstance(key, str):
                    _raise("invalid_json_value")
                _validate_string(key)
                pending.append((item, depth + 1))
        elif isinstance(value, list):
            if depth > MAX_NESTING_DEPTH:
                _raise("nesting_too_deep")
            if len(value) > MAX_ARRAY_ITEMS:
                _raise("too_many_array_items")
            pending.extend((item, depth + 1) for item in value)
        elif isinstance(value, str):
            _validate_string(value)
        elif value is None or isinstance(value, (bool, int)):
            continue
        elif isinstance(value, float):
            if not math.isfinite(value):
                _raise("invalid_json_value")
        else:
            _raise("invalid_json_value")


def _reject_non_json_constant(_value: str) -> None:
    _raise("invalid_json")


def _decode_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            _raise("invalid_json")
        value[key] = item
    return value


def decode_frame(raw: Any) -> dict:
    if isinstance(raw, bytes):
        if len(raw) > MAX_FRAME_BYTES:
            _raise("frame_too_large")
        try:
            text = raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            _raise("invalid_utf8")
    elif isinstance(raw, str):
        if "\x00" in raw:
            _raise("nul_not_allowed")
        try:
            encoded = raw.encode("utf-8", errors="strict")
        except UnicodeEncodeError:
            _raise("lone_surrogate")
        if len(encoded) > MAX_FRAME_BYTES:
            _raise("frame_too_large")
        text = raw
    else:
        _raise("invalid_frame_type")

    if "\x00" in text:
        _raise("nul_not_allowed")
    try:
        frame = json.loads(
            text,
            object_pairs_hook=_decode_object,
            parse_constant=_reject_non_json_constant,
        )
    except FrameCodecError:
        raise
    except RecursionError:
        _raise("nesting_too_deep")
    except (TypeError, ValueError, json.JSONDecodeError):
        _raise("invalid_json")
    if not isinstance(frame, dict):
        _raise("top_level_not_object")
    _validate_value(frame)
    return frame


def try_decode_frame(raw: Any) -> dict | None:
    """Decode an untrusted frame without exposing its original body on failure."""
    try:
        return decode_frame(raw)
    except FrameCodecError:
        return None


def encode_frame(frame: dict) -> str:
    _validate_value(frame)
    try:
        encoded = json.dumps(
            frame,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
        encoded_bytes = encoded.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        _raise("lone_surrogate")
    except (TypeError, ValueError, OverflowError):
        _raise("invalid_json_value")
    if len(encoded_bytes) > MAX_FRAME_BYTES:
        _raise("frame_too_large")
    return encoded
