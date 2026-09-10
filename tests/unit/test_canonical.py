"""M0-A1b1 canonical 确定性编码测试。

所有 golden 期望值均写死，不得由被测函数或同构 helper 现场生成。
不依赖 cwd、网络、locale、当前时区或文件写入。
"""

from __future__ import annotations

import hashlib
import pathlib
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum

import pytest

from aotf.canonical import canonical_json_bytes, canonical_json_text, canonical_sha256
from aotf.errors import AotfError, ErrorCode

# ---- golden vector（写死期望，字节级） -------------------------------------

GOLDEN_TEXT = (
    '{"a":[null,true,false,0,-7,'
    '{"$aotf":"decimal","value":"1.23"},'
    '{"$aotf":"datetime","value":"2026-08-31T16:00:00.123456Z"}],'
    '"z":"é"}'
)
GOLDEN_HEX = (
    "7b2261223a5b6e756c6c2c747275652c66616c73652c302c2d372c"
    "7b2224616f7466223a22646563696d616c222c2276616c7565223a22312e3233227d2c"
    "7b2224616f7466223a226461746574696d65222c2276616c7565223a"
    "22323032362d30382d33315431363a30303a30302e3132333435365a227d5d2c"
    "227a223a22c3a9227d"
)
GOLDEN_SHA256 = "46b947bf919c1c8ec481db30762cd4296cef0fa714d6baafe17a71ee3b321b7e"


class TaskPhase(Enum):
    CREATED = "CREATED"


class Status(str, Enum):
    OK = "ok"


class MyList(list):
    pass


class MyDict(dict):
    pass


def _golden_input() -> dict:
    return {
        "z": "é",
        "a": [
            None,
            True,
            False,
            0,
            -7,
            Decimal("1.2300"),
            datetime(2026, 9, 1, 0, 0, 0, 123456, tzinfo=timezone(timedelta(hours=8))),
        ],
    }


def test_golden_vector_bytes_hex_and_digest_are_exact() -> None:
    value = _golden_input()
    assert canonical_json_bytes(value) == GOLDEN_TEXT.encode("utf-8")
    assert canonical_json_bytes(value).hex() == GOLDEN_HEX
    assert canonical_sha256(value) == GOLDEN_SHA256


def test_mapping_key_order_does_not_change_canonical_result() -> None:
    a = {"x": 1, "y": 2}
    b = {"y": 2, "x": 1}
    assert canonical_json_bytes(a) == canonical_json_bytes(b)
    assert canonical_sha256(a) == canonical_sha256(b)

    # 无效 dict 的错误 path 不依赖插入顺序（完整高辨识度 key）
    left = {"ORDER-KEY-A-7F31": float("nan"), "ORDER-KEY-B-9C42": 1}
    right = {"ORDER-KEY-B-9C42": 1, "ORDER-KEY-A-7F31": float("nan")}
    errs = []
    for v in (left, right):
        with pytest.raises(AotfError) as exc:
            canonical_json_bytes(v)
        assert exc.value.code is ErrorCode.SCHEMA_VALIDATION_FAILED
        errs.append((exc.value.message, dict(exc.value.details)))
    assert errs[0] == errs[1]
    assert errs[0][1]["path"] == "$[0]"
    assert "ORDER-KEY-A-7F31" not in str(errs[0])
    assert "ORDER-KEY-B-9C42" not in str(errs[0])


def test_unicode_is_nfc_utf8_without_bom_or_trailing_newline() -> None:
    raw = "é"
    b = canonical_json_bytes({"k": raw})
    assert not b.startswith(b"\xef\xbb\xbf")
    assert not b.endswith(b"\n")
    text = canonical_json_text({"k": raw})
    assert "é" in text
    assert "é" not in text  # NFC 归一化，无 combining 序列残留


def test_quotes_backslashes_newlines_and_controls_use_json_escaping() -> None:
    v = {"s": 'quote" back\\slash\nnewline\t\b\f\r'}
    text = canonical_json_text(v)
    assert "\n" not in text  # 换行被转义为 \n 文本，而非真实换行
    assert "\\n" in text
    assert "\\t" in text
    assert '\\"' in text
    assert "\\\\" in text
    assert canonical_json_text(v) == canonical_json_text(v)


def test_decimal_equivalent_forms_and_negative_zero_are_canonical() -> None:
    # 去零只作用于小数部分：整数不误伤
    assert canonical_json_text({"v": Decimal("100")}) == '{"v":{"$aotf":"decimal","value":"100"}}'
    assert canonical_json_text({"v": Decimal("100.0")}) == '{"v":{"$aotf":"decimal","value":"100"}}'
    assert canonical_json_text({"v": Decimal("1E+2")}) == '{"v":{"$aotf":"decimal","value":"100"}}'
    assert canonical_json_text({"v": Decimal("0.00100")}) == '{"v":{"$aotf":"decimal","value":"0.001"}}'
    assert canonical_json_text({"v": Decimal("-0")}) == '{"v":{"$aotf":"decimal","value":"0"}}'
    assert canonical_json_text({"v": Decimal("0.000")}) == '{"v":{"$aotf":"decimal","value":"0"}}'
    assert canonical_json_text({"v": Decimal("1.2300")}) == '{"v":{"$aotf":"decimal","value":"1.23"}}'
    assert canonical_sha256({"v": Decimal("1.2300")}) == canonical_sha256({"v": Decimal("1.23")})


def test_non_finite_decimal_values_are_rejected() -> None:
    for d in (Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity")):
        with pytest.raises(AotfError) as exc:
            canonical_json_bytes({"v": d})
        assert exc.value.code is ErrorCode.SCHEMA_VALIDATION_FAILED


def test_aware_datetimes_normalize_same_instant_to_fixed_utc() -> None:
    d1 = datetime(2026, 9, 1, 0, 0, 0, 123456, tzinfo=timezone(timedelta(hours=8)))
    d2 = datetime(2026, 8, 31, 16, 0, 0, 123456, tzinfo=timezone.utc)
    assert canonical_json_text({"t": d1}) == (
        '{"t":{"$aotf":"datetime","value":"2026-08-31T16:00:00.123456Z"}}'
    )
    assert canonical_sha256({"t": d1}) == canonical_sha256({"t": d2})

    # 低年份固定四位，不依赖平台 strftime
    assert canonical_json_text({"t": datetime(1, 1, 1, tzinfo=timezone.utc)}) == (
        '{"t":{"$aotf":"datetime","value":"0001-01-01T00:00:00.000000Z"}}'
    )
    assert canonical_json_text(
        {"t": datetime(999, 12, 31, 23, 59, 59, 1, tzinfo=timezone.utc)}
    ) == '{"t":{"$aotf":"datetime","value":"0999-12-31T23:59:59.000001Z"}}'


def test_naive_datetime_is_rejected() -> None:
    with pytest.raises(AotfError) as exc:
        canonical_json_bytes({"t": datetime(2026, 9, 1)})
    assert exc.value.code is ErrorCode.SCHEMA_VALIDATION_FAILED


def test_enum_identity_is_stable_and_checked_before_scalar_types() -> None:
    expected_type = f"{TaskPhase.__module__}.TaskPhase"
    assert canonical_json_text({"s": TaskPhase.CREATED}) == (
        f'{{"s":{{"$aotf":"enum","name":"CREATED","type":"{expected_type}"}}}}'
    )
    # str,Enum 必须先走 Enum，不得降级为普通 str
    assert canonical_sha256({"s": Status.OK}) != canonical_sha256({"s": "ok"})
    assert canonical_sha256({"s": TaskPhase.CREATED}) != canonical_sha256(
        {"s": "CREATED"}
    )
    # 局部类 Enum 身份不稳定，拒绝
    class LocalPhase(Enum):
        X = "x"

    with pytest.raises(AotfError) as exc:
        canonical_json_bytes({"s": LocalPhase.X})
    assert exc.value.code is ErrorCode.SCHEMA_VALIDATION_FAILED


def test_float_and_other_unsupported_types_are_rejected() -> None:
    unsupported = [
        1.5,
        float("nan"),
        float("inf"),
        float("-inf"),
        b"bytes",
        (1, 2),
        {1, 2},
        pathlib.Path("x"),
        object(),
        MyList([1]),
        MyDict({"a": 1}),
    ]
    for u in unsupported:
        with pytest.raises(AotfError) as exc:
            canonical_json_bytes(u)
        assert exc.value.code is ErrorCode.SCHEMA_VALIDATION_FAILED


def test_non_string_mapping_keys_are_rejected() -> None:
    for key in (1, 1.5, None, True, (1, 2), b"k"):
        with pytest.raises(AotfError) as exc:
            canonical_json_bytes({key: "v"})
        assert exc.value.code is ErrorCode.SCHEMA_VALIDATION_FAILED


def test_reserved_marker_and_nfc_key_collisions_are_rejected() -> None:
    with pytest.raises(AotfError) as exc:
        canonical_json_bytes({"$aotf": "x"})
    assert exc.value.code is ErrorCode.SCHEMA_VALIDATION_FAILED
    with pytest.raises(AotfError) as exc:
        canonical_json_bytes({"é": 1, "é": 2})
    assert exc.value.code is ErrorCode.SCHEMA_VALIDATION_FAILED


def test_exact_sensitive_keys_are_rejected_without_substring_false_positive() -> None:
    sensitive = [
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
    ]
    marker = "SENSITIVE-VALUE-9F3A-77"
    for key in sensitive:
        with pytest.raises(AotfError) as exc:
            canonical_json_bytes({key: marker})
        assert exc.value.code is ErrorCode.SCHEMA_VALIDATION_FAILED
        assert marker not in str(exc.value)
        assert marker not in exc.value.message
        assert marker not in str(exc.value.details)
    # 仅精确 key 匹配，子串不误伤
    assert canonical_json_text({"input_tokens": 5, "authorization_id": "abc"}) == (
        '{"authorization_id":"abc","input_tokens":5}'
    )


def test_recursive_containers_are_rejected_but_shared_values_are_allowed() -> None:
    lst = [1, 2]
    lst.append(lst)
    with pytest.raises(AotfError) as exc:
        canonical_json_bytes(lst)
    assert exc.value.code is ErrorCode.SCHEMA_VALIDATION_FAILED

    d = {}
    d["self"] = d
    with pytest.raises(AotfError) as exc:
        canonical_json_bytes(d)
    assert exc.value.code is ErrorCode.SCHEMA_VALIDATION_FAILED

    # 共享但非递归引用允许，且输出与等价展开一致
    shared = [1, 2]
    v_shared = {"a": shared, "b": shared}
    v_expanded = {"a": [1, 2], "b": [1, 2]}
    assert canonical_sha256(v_shared) == canonical_sha256(v_expanded)


def test_text_bytes_digest_and_safe_error_contract_are_consistent() -> None:
    v = {"k": [1, Decimal("2.50")]}
    assert canonical_json_text(v) == canonical_json_bytes(v).decode("utf-8")
    assert canonical_sha256(v) == hashlib.sha256(canonical_json_bytes(v)).hexdigest()

    # 高辨识度 key/value 均真实进入输入，不得泄漏到 str/message/details/path
    high_key = "SENSITIVE-KEY-77XYZ"
    high_val = "SENSITIVE-VALUE-88ABC"
    with pytest.raises(AotfError) as exc:
        canonical_json_bytes({high_key: [high_val, float("nan")]})
    assert exc.value.code is ErrorCode.SCHEMA_VALIDATION_FAILED
    assert high_key not in str(exc.value)
    assert high_key not in exc.value.message
    assert high_key not in str(exc.value.details)
    assert high_val not in str(exc.value)
    assert high_val not in exc.value.message
    assert high_val not in str(exc.value.details)
    assert "path" in exc.value.details
    assert high_key not in str(exc.value.details.get("path", ""))
    assert high_val not in str(exc.value.details.get("path", ""))
