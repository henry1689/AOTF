"""M0-C1 TestEvidence typed contract + 确定性序列化测试。

纯数据契约（无文件/DB/git）；校验全 fail-closed；序列化确定性 +
bundle round-trip 自洽。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

import aotf.evidence.schema as evschema
from aotf.evidence.schema import (
    EvidenceCheck,
    EvidenceError,
    EvidenceRunnerInfo,
    compute_bundle_sha256,
    evidence_json_bytes,
    record_with_bundle,
)

T0 = datetime(2026, 9, 1, tzinfo=timezone.utc)
T1 = T0 + timedelta(seconds=5)
H40 = "a" * 40
H64 = "b" * 64


def _runner(start=T0, end=T1) -> EvidenceRunnerInfo:
    return EvidenceRunnerInfo(
        identity="aotf-evidence-runner@v1", host_fingerprint=None,
        started_at=start, ended_at=end,
    )


def _check(check_id="unit", command_id="project.unit", status="passed",
           **kw) -> EvidenceCheck:
    base = dict(check_id=check_id, command_id=command_id,
                stdout_sha256=H64, stderr_sha256=H64, duration_ms=12,
                status=status)
    base.update(kw)
    return EvidenceCheck(**base)


def _record(checks=(), bundle=H64, **kw) -> "evschema.TestEvidenceRecord":
    base = dict(schema_version=1, evidence_id="ev-1", task_id="task-1",
                worktree_tree=H40, delta_sha256=H64, runner=_runner(),
                checks=checks, bundle_sha256=bundle)
    base.update(kw)
    return evschema.TestEvidenceRecord(**base)


def test_valid_record_ok() -> None:
    rec = _record(checks=(_check(),))
    assert rec.evidence_id == "ev-1"
    assert rec.runner.identity == "aotf-evidence-runner@v1"
    assert rec.checks[0].status == "passed"


def test_check_status_must_be_allowed() -> None:
    with pytest.raises(EvidenceError, match="status"):
        _check(status="bogus")


@pytest.mark.parametrize("field,value,hex64", [
    ("worktree_tree", "z" * 40, False),
    ("delta_sha256", "z" * 64, True),
    ("bundle_sha256", "z" * 64, True),
])
def test_bad_sha_hex_rejected(field, value, hex64) -> None:
    with pytest.raises(EvidenceError):
        _record(**{field: value})


def test_bad_sha_in_check_rejected() -> None:
    with pytest.raises(EvidenceError, match="sha256"):
        _check(stdout_sha256="xyz")


def test_bad_token_rejected() -> None:
    for kwargs in (
        {"evidence_id": "a/../b"},
        {"task_id": "x" * 65},
    ):
        with pytest.raises(EvidenceError, match="token"):
            _record(**kwargs)
    with pytest.raises(EvidenceError, match="token"):
        _check(check_id="..")
    with pytest.raises(EvidenceError, match="token"):
        _check(command_id="c/x")
    with pytest.raises(EvidenceError, match="token"):
        _runner_ident("bad/ident")


def _runner_ident(ident: str) -> EvidenceRunnerInfo:
    return EvidenceRunnerInfo(identity=ident, host_fingerprint=None,
                              started_at=T0, ended_at=T1)


def test_cwd_rejects_absolute_and_parent() -> None:
    with pytest.raises(EvidenceError, match="cwd"):
        _check(cwd="/abs")
    with pytest.raises(EvidenceError, match="cwd"):
        _check(cwd="..")
    with pytest.raises(EvidenceError, match="cwd"):
        _check(cwd="a/../b")
    assert _check(cwd=".").cwd == "."


def test_negative_exit_or_duration_rejected() -> None:
    with pytest.raises(EvidenceError, match="duration_ms"):
        _check(duration_ms=-1)
    with pytest.raises(EvidenceError):
        _check(exit_code="1")


def test_runner_time_order_enforced() -> None:
    with pytest.raises(EvidenceError, match="ended_at"):
        _runner(start=T1, end=T0)
    with pytest.raises(EvidenceError, match="timezone-aware"):
        EvidenceRunnerInfo(
            identity="aotf-evidence-runner@v1", host_fingerprint=None,
            started_at=datetime(2026, 9, 1), ended_at=datetime(2026, 9, 1),
        )


def test_schema_version_must_be_one() -> None:
    with pytest.raises(EvidenceError, match="schema_version"):
        _record(schema_version=2)


def test_empty_checks_allowed() -> None:
    rec = _record()
    assert rec.checks == ()


def test_serialization_deterministic() -> None:
    a = _record(checks=(_check(check_id="b"), _check(check_id="a")))
    b = _record(checks=(_check(check_id="a"), _check(check_id="b")))
    raw = evidence_json_bytes(a, include_bundle=True)
    assert raw == evidence_json_bytes(b, include_bundle=True)
    parsed = json.loads(raw)
    assert [c["check_id"] for c in parsed["checks"]] == ["a", "b"]


def test_serialization_datetime_utc_z() -> None:
    rec = _record()
    parsed = json.loads(evidence_json_bytes(rec, include_bundle=True))
    assert parsed["runner"]["started_at"] == "2026-09-01T00:00:00.000000Z"
    assert parsed["runner"]["ended_at"] == "2026-09-01T00:00:05.000000Z"


def test_compute_bundle_roundtrip() -> None:
    rec = record_with_bundle(_record(checks=(_check(),)))
    assert rec.bundle_sha256 == compute_bundle_sha256(rec)
    assert rec.bundle_sha256 == compute_bundle_sha256(rec)
    other = _record(checks=(_check(),))
    assert record_with_bundle(other).bundle_sha256 == rec.bundle_sha256
    changed = _record(checks=(_check(),), task_id="task-2")
    assert record_with_bundle(changed).bundle_sha256 != rec.bundle_sha256


def test_include_bundle_flag_stable_layout() -> None:
    rec = _record(checks=(_check(),))
    raw = evidence_json_bytes(rec, include_bundle=False)
    parsed = json.loads(raw)
    assert parsed["bundle_sha256"] == ""
    assert list(parsed) == sorted(parsed)


def test_host_fingerprint_optional() -> None:
    rec = _record()
    assert rec.runner.host_fingerprint is None
    ok = _record()
    assert ok is not None


def test_import_has_no_side_effect() -> None:
    import aotf.evidence.schema  # noqa: F401
    assert aotf.evidence.schema.__name__ == "aotf.evidence.schema"
