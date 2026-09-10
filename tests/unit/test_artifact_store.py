"""M0-B6 artifact store（filesystem append-only）测试。

纯 filesystem（tmp_path），无 git/DB；ingest 产物全在 tmp；verify 字节级
完整性（§20.4 #24）；令牌校验防路径穿越。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from aotf.artifacts.store import (
    ArtifactError,
    ArtifactInfo,
    ingest,
    list_artifacts,
    verify,
)

T0 = datetime(2026, 9, 1, tzinfo=timezone.utc)
TASK = "task-1"
KIND = "evidence"
PRODUCER = "aotf-evidence-runner"


def _root(tmp_path) -> str:
    return str(tmp_path / "art")


def _src(tmp_path, name: str = "out.txt",
         content: bytes = b"hello artifact\n") -> Path:
    p = tmp_path / name
    p.write_bytes(content)
    return p


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def test_ingest_text_artifact(tmp_path) -> None:
    src = _src(tmp_path)
    info = ingest(src, root=_root(tmp_path), task_id=TASK, kind=KIND,
                  producer=PRODUCER, at=T0)
    assert isinstance(info, ArtifactInfo)
    assert info.task_id == TASK and info.kind == KIND
    assert info.producer == PRODUCER
    assert info.relative_path == "out.txt"
    assert info.sha256 == _sha(src.read_bytes())
    assert info.size_bytes == len(src.read_bytes())
    assert info.created_at == "2026-09-01T00:00:00Z"
    assert Path(info.data_path).read_bytes() == src.read_bytes()
    meta = json.loads(open(info.metadata_path, encoding="utf-8").read())
    assert meta["schema_version"] == 1
    assert meta["artifact_id"] == info.artifact_id


def test_ingest_binary_bytes_preserved(tmp_path) -> None:
    content = b"\x00\x01\xff\xfe\x00"
    src = _src(tmp_path, "bin.dat", content)
    info = ingest(src, root=_root(tmp_path), task_id=TASK, kind=KIND,
                  producer=PRODUCER)
    assert Path(info.data_path).read_bytes() == content
    assert info.sha256 == _sha(content)
    assert info.size_bytes == len(content)


def test_default_artifact_id_content_derived(tmp_path) -> None:
    content = b"same bytes\n"
    src = _src(tmp_path, content=content)
    info = ingest(src, root=_root(tmp_path), task_id=TASK, kind=KIND,
                  producer=PRODUCER)
    assert info.artifact_id == f"{TASK}--{KIND}--{_sha(content)[:12]}"
    # append-only：同 task/kind/内容再次 ingest → 同 id → 拒绝（不覆盖）
    with pytest.raises(ArtifactError, match="already exists"):
        ingest(_src(tmp_path, "out2.txt", content),
               root=_root(tmp_path), task_id=TASK, kind=KIND,
               producer=PRODUCER)


def test_explicit_artifact_id_used(tmp_path) -> None:
    src = _src(tmp_path)
    info = ingest(src, root=_root(tmp_path), task_id=TASK, kind=KIND,
                  producer=PRODUCER, artifact_id="ev-abc", at=T0)
    assert info.artifact_id == "ev-abc"
    assert Path(info.data_path).name == "ev-abc.data"


def test_duplicate_id_refused(tmp_path) -> None:
    src = _src(tmp_path)
    root = _root(tmp_path)
    ingest(src, root=root, task_id=TASK, kind=KIND, producer=PRODUCER,
           artifact_id="dup", at=T0)
    with pytest.raises(ArtifactError, match="already exists"):
        ingest(_src(tmp_path, "other.txt", b"other\n"),
               root=root, task_id=TASK, kind=KIND, producer=PRODUCER,
               artifact_id="dup", at=T0)


def test_verify_ok(tmp_path) -> None:
    src = _src(tmp_path)
    root = _root(tmp_path)
    info = ingest(src, root=root, task_id=TASK, kind=KIND, producer=PRODUCER,
                  at=T0)
    checked = verify(root, TASK, KIND, info.artifact_id)
    assert checked == info


def test_verify_detects_byte_tamper(tmp_path) -> None:
    src = _src(tmp_path)
    root = _root(tmp_path)
    info = ingest(src, root=root, task_id=TASK, kind=KIND, producer=PRODUCER)
    data = Path(info.data_path)
    raw = bytearray(data.read_bytes())
    raw[0] ^= 0xFF
    data.write_bytes(bytes(raw))
    with pytest.raises(ArtifactError, match="hash/size mismatch"):
        verify(root, TASK, KIND, info.artifact_id)


def test_verify_detects_size_change(tmp_path) -> None:
    src = _src(tmp_path)
    root = _root(tmp_path)
    info = ingest(src, root=root, task_id=TASK, kind=KIND, producer=PRODUCER)
    data = Path(info.data_path)
    data.write_bytes(data.read_bytes() + b"x")
    with pytest.raises(ArtifactError, match="hash/size mismatch"):
        verify(root, TASK, KIND, info.artifact_id)


def test_verify_missing_data_raises(tmp_path) -> None:
    src = _src(tmp_path)
    root = _root(tmp_path)
    info = ingest(src, root=root, task_id=TASK, kind=KIND, producer=PRODUCER)
    Path(info.data_path).unlink()
    with pytest.raises(ArtifactError, match="data not found"):
        verify(root, TASK, KIND, info.artifact_id)


def test_verify_metadata_mismatch(tmp_path) -> None:
    src = _src(tmp_path)
    root = _root(tmp_path)
    info = ingest(src, root=root, task_id=TASK, kind=KIND, producer=PRODUCER)
    meta_path = Path(info.metadata_path)
    payload = json.loads(meta_path.read_text(encoding="utf-8"))
    payload["kind"] = "tampered"
    meta_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ArtifactError, match="metadata mismatch"):
        verify(root, TASK, KIND, info.artifact_id)


def test_invalid_tokens_rejected(tmp_path) -> None:
    src = _src(tmp_path)
    root = _root(tmp_path)
    with pytest.raises(ArtifactError, match="token"):
        ingest(src, root=root, task_id="a/../b", kind=KIND, producer=PRODUCER)
    with pytest.raises(ArtifactError, match="token"):
        ingest(src, root=root, task_id=TASK, kind="../../x", producer=PRODUCER)
    with pytest.raises(ArtifactError, match="token"):
        ingest(src, root=root, task_id=TASK, kind=KIND, producer=PRODUCER,
               artifact_id="ev/x")
    with pytest.raises(ArtifactError, match="token"):
        ingest(src, root=root, task_id="t" * 65, kind=KIND, producer=PRODUCER)
    # 目录穿越守卫：整段 "." / ".." 也拒绝（正则放行但越 root）
    for bad in (".", ".."):
        with pytest.raises(ArtifactError, match="token"):
            ingest(src, root=root, task_id=bad, kind=KIND, producer=PRODUCER)
        with pytest.raises(ArtifactError, match="token"):
            ingest(src, root=root, task_id=TASK, kind=bad, producer=PRODUCER)


def test_source_missing_raises_valueerror(tmp_path) -> None:
    with pytest.raises(ValueError):
        ingest(tmp_path / "nope", root=_root(tmp_path), task_id=TASK,
               kind=KIND, producer=PRODUCER)


def test_invalid_producer_rejected(tmp_path) -> None:
    src = _src(tmp_path)
    with pytest.raises(ArtifactError, match="token"):
        ingest(src, root=_root(tmp_path), task_id=TASK, kind=KIND,
               producer="evil/producer")


def test_list_artifacts_empty(tmp_path) -> None:
    assert list_artifacts(_root(tmp_path), TASK) == ()


def test_list_artifacts_grouped_sorted(tmp_path) -> None:
    root = _root(tmp_path)
    for i, content in enumerate((b"a\n", b"b\n")):
        ingest(_src(tmp_path, f"s{i}.txt", content), root=root, task_id=TASK,
               kind="evidence", producer=PRODUCER)
    ingest(_src(tmp_path, "plan.json", b'{"x":1}'), root=root, task_id=TASK,
           kind="plan", producer=PRODUCER)
    got = list_artifacts(root, TASK)
    assert len(got) == 3
    kinds = [a.kind for a in got]
    assert kinds == sorted(kinds)
    only_evidence = list_artifacts(root, TASK, kind="evidence")
    assert {a.kind for a in only_evidence} == {"evidence"}
    assert len(only_evidence) == 2


def test_list_skips_corrupt_sidecar(tmp_path) -> None:
    root = Path(_root(tmp_path))
    src = _src(tmp_path)
    good = ingest(src, root=str(root), task_id=TASK, kind=KIND,
                  producer=PRODUCER)
    bad_dir = root / TASK / "badkind"
    bad_dir.mkdir(parents=True)
    (bad_dir / "x.json").write_text("not json", encoding="utf-8")
    (bad_dir / "x.data").write_bytes(b"data")
    listed = list_artifacts(str(root), TASK)
    assert [a.artifact_id for a in listed] == [good.artifact_id]


def test_unicode_source_filename_in_sidecar(tmp_path) -> None:
    src = _src(tmp_path, "报告.txt", b"report\n")
    info = ingest(src, root=_root(tmp_path), task_id=TASK, kind=KIND,
                  producer=PRODUCER)
    assert info.relative_path == "报告.txt"
    checked = verify(_root(tmp_path), TASK, KIND, info.artifact_id)
    assert checked.relative_path == "报告.txt"
