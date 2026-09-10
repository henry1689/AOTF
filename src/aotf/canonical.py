"""AOTF canonical 确定性编码基座。

把受支持的 Python 值转换为跨 Python 3.11/3.13 稳定、无 BOM、无空白歧义的
UTF-8 JSON bytes，并以这些 bytes 计算 SHA-256。不基于 repr()、默认 JSON
参数、locale、当前时区或对象内存地址。

只支持本模块显式列出的类型；dataclass/Path/bytes/tuple/set 与任意对象的
``__dict__`` 一律拒绝。调用方必须显式选择进入 hash 的字段。
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any

from aotf.errors import AotfError, ErrorCode

__all__ = ["canonical_json_bytes", "canonical_json_text", "canonical_sha256"]

_RESERVED_MARKER = "$aotf"

_SENSITIVE_KEYS = frozenset(
    {
        "password",
        "passwd",
        "secret",
        "token",
        "api_key",
        "apikey",
        "cookie",
        "authorization",
        "private_key",
        "access_token",
        "refresh_token",
    }
)

_JSON_OPTS = {
    "ensure_ascii": False,
    "sort_keys": True,
    "separators": (",", ":"),
    "allow_nan": False,
}


def _safe_path(segments: tuple[int, ...]) -> str:
    """把安全索引段渲染为路径；不含任何原始 key 或 value。"""
    return "$" + "".join(f"[{seg}]" for seg in segments)


def _reject(segments: tuple[int, ...], type_name: str, message: str) -> None:
    raise AotfError(
        ErrorCode.SCHEMA_VALIDATION_FAILED,
        message,
        details={"path": _safe_path(segments), "type": type_name},
    )


def _decimal_fixed(value: Decimal) -> str:
    """Decimal 规范化：fixed-point、去尾零仅作用小数部分、负零统一为 "0"。"""
    s = f"{value:f}"
    if "." in s:
        int_part, frac_part = s.split(".", 1)
        frac_part = frac_part.rstrip("0")
        s = int_part + ("." + frac_part if frac_part else "")
    if s == "-0":
        s = "0"
    return s


def _datetime_utc(value: datetime) -> str:
    utc = value.astimezone(timezone.utc)
    return (
        f"{utc.year:04d}-{utc.month:02d}-{utc.day:02d}T"
        f"{utc.hour:02d}:{utc.minute:02d}:{utc.second:02d}."
        f"{utc.microsecond:06d}Z"
    )


def _canonicalize(
    value: Any, segments: tuple[int, ...], seen: frozenset[int]
) -> object:
    if isinstance(value, Enum):
        cls = value.__class__
        qualname = cls.__qualname__
        if "<locals>" in qualname:
            _reject(segments, "Enum", "enum with local class identity is rejected")
        return {
            "$aotf": "enum",
            "name": value.name,
            "type": f"{cls.__module__}.{qualname}",
        }
    if value is None:
        return None
    if type(value) is bool:
        return value
    if type(value) is int:
        return value
    if type(value) is str:
        return unicodedata.normalize("NFC", value)
    if isinstance(value, Decimal):
        if not value.is_finite():
            _reject(segments, "Decimal", "non-finite decimal is rejected")
        return {"$aotf": "decimal", "value": _decimal_fixed(value)}
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            _reject(segments, "datetime", "naive datetime is rejected")
        return {"$aotf": "datetime", "value": _datetime_utc(value)}
    if type(value) is list:
        return _canonicalize_list(value, segments, seen)
    if type(value) is dict:
        return _canonicalize_dict(value, segments, seen)
    _reject(segments, type(value).__name__, "unsupported type")
    return None


def _canonicalize_list(
    value: list, segments: tuple[int, ...], seen: frozenset[int]
) -> list:
    marker = id(value)
    if marker in seen:
        _reject(segments, "list", "recursive container is rejected")
    new_seen = seen | {marker}
    return [
        _canonicalize(item, segments + (i,), new_seen)
        for i, item in enumerate(value)
    ]


def _canonicalize_dict(
    value: dict, segments: tuple[int, ...], seen: frozenset[int]
) -> dict:
    marker = id(value)
    if marker in seen:
        _reject(segments, "dict", "recursive container is rejected")
    new_seen = seen | {marker}

    items: list[tuple[str, object]] = []
    for raw_key, raw_value in value.items():
        if type(raw_key) is not str:
            _reject(
                segments,
                type(raw_key).__name__,
                "non-string mapping key is rejected",
            )
        nfc_key = unicodedata.normalize("NFC", raw_key)
        if nfc_key == _RESERVED_MARKER:
            _reject(segments, "str", "reserved marker key is rejected")
        if nfc_key.casefold() in _SENSITIVE_KEYS:
            _reject(segments, "str", "sensitive key is rejected")
        items.append((nfc_key, raw_value))

    if len({k for k, _ in items}) != len(items):
        _reject(segments, "dict", "nfc key collision is rejected")

    ordered = sorted(items, key=lambda it: it[0])
    result: dict[str, object] = {}
    for seq, (nfc_key, raw_value) in enumerate(ordered):
        result[nfc_key] = _canonicalize(raw_value, segments + (seq,), new_seen)
    return result


def canonical_json_bytes(value: object) -> bytes:
    """返回受支持值的确定性 UTF-8 JSON bytes。"""
    canon = _canonicalize(value, (), frozenset())
    return json.dumps(canon, **_JSON_OPTS).encode("utf-8")


def canonical_json_text(value: object) -> str:
    """返回 canonical bytes 的 UTF-8 解码文本。"""
    return canonical_json_bytes(value).decode("utf-8")


def canonical_sha256(value: object) -> str:
    """返回 canonical bytes 的 SHA-256 hex。"""
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()
