"""AOTF artifact store（filesystem append-only；M0-B6）。

spec §9.1：typed kind、producer identity、task 绑定、sha256/size/created_at、
append-only 存储路径。本模块为 filesystem 层：

- ingest：验证源文件 → 令牌校验（防路径穿越）→ 写
  `root/<task>/<kind>/<artifact_id>.data`（原始 bytes 原样）+ 同 .json
  sidecar（确定性 sorted-json ensure_ascii；源文件名等自由文本，不经
  canonical 守卫——B2 manifest 先例）。artifact_id 存在即拒（append-only
  不覆盖）；写失败清理半截 .data。
- verify：重算 .data bytes 的 sha256/size 与 sidecar 比对，任一不符 →
  ArtifactError（§20.4 #24 字节级完整性）。
- list_artifacts：扫 sidecar 返回 (kind, artifact_id) sorted；损坏项跳过。

DB 行索引边界：models.ArtifactRecord / aotf.store.insert_artifact（M0-A）
管 artifacts 表行；本模块不落 DB——行索引归 Orchestrator 编排接线。

异常域：完整性/已存在/非法令牌 → ArtifactError；源路径无效 → ValueError。
不扩冻结 16 码。仅 stdlib，无 aotf import。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

__all__ = ["ArtifactError", "ArtifactInfo", "ingest", "verify",
           "list_artifacts"]

_TOKEN_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_SCHEMA_VERSION = 1
_META_KEYS = ("artifact_id", "task_id", "kind", "producer", "relative_path",
              "sha256", "size_bytes", "created_at")


class ArtifactError(Exception):
    """artifact 的完整性/append-only/令牌拒绝（非状态机错误）。"""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True, slots=True)
class ArtifactInfo:
    """一件已 ingest artifact 的事实（值对象，含落盘路径）。"""

    artifact_id: str
    task_id: str
    kind: str
    relative_path: str
    producer: str
    sha256: str
    size_bytes: int
    created_at: str
    data_path: str
    metadata_path: str


def _check_token(name: str, value: str) -> None:
    if value in (".", ".."):  # 正则放行但会造成目录穿越（越出 root）
        raise ArtifactError(f"invalid {name} token")
    if not _TOKEN_RE.fullmatch(value):
        raise ArtifactError(f"invalid {name} token")


def _as_utc(at: datetime | None) -> str:
    if at is None:
        at = datetime.now(timezone.utc)
    if at.tzinfo is None or at.utcoffset() is None:
        raise ValueError("at must be timezone-aware")
    return at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _json_bytes(payload: dict) -> bytes:
    return json.dumps(payload, sort_keys=True, ensure_ascii=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _meta_path(root: Path, task_id: str, kind: str, artifact_id: str) -> Path:
    return root / task_id / kind / f"{artifact_id}.json"


def _info_from_payload(root: Path, task_id: str, kind: str,
                       payload: object) -> ArtifactInfo | None:
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        return None
    if any(k not in payload for k in _META_KEYS):
        return None
    aid = payload["artifact_id"]
    if payload["task_id"] != task_id or payload["kind"] != kind:
        return None
    for key in ("artifact_id", "task_id", "kind", "producer", "relative_path",
                "sha256", "created_at"):
        if not isinstance(payload[key], str):
            return None
    if not isinstance(payload["size_bytes"], int):
        return None
    return ArtifactInfo(
        artifact_id=aid,
        task_id=task_id,
        kind=kind,
        relative_path=payload["relative_path"],
        producer=payload["producer"],
        sha256=payload["sha256"],
        size_bytes=payload["size_bytes"],
        created_at=payload["created_at"],
        data_path=str(root / task_id / kind / f"{aid}.data"),
        metadata_path=str(_meta_path(root, task_id, kind, aid)),
    )


def ingest(
    source: str | Path,
    *,
    root: str | Path,
    task_id: str,
    kind: str,
    producer: str,
    artifact_id: str | None = None,
    at: datetime | None = None,
) -> ArtifactInfo:
    """把源文件验证后不可变写入 artifact store（append-only）。"""
    src = Path(source)
    if not src.exists() or not src.is_file():
        raise ValueError(f"source not found or not a file: {source}")
    _check_token("task_id", task_id)
    _check_token("kind", kind)
    _check_token("producer", producer)
    if artifact_id is not None:
        _check_token("artifact_id", artifact_id)
    created_at = _as_utc(at)

    try:
        data = src.read_bytes()
    except OSError as exc:
        raise ArtifactError(f"cannot read source: {exc}") from exc
    sha256 = hashlib.sha256(data).hexdigest()
    size_bytes = len(data)
    if artifact_id is None:
        artifact_id = f"{task_id}--{kind}--{sha256[:12]}"

    dirp = Path(root) / task_id / kind
    data_path = dirp / f"{artifact_id}.data"
    meta_path = dirp / f"{artifact_id}.json"
    if data_path.exists() or meta_path.exists():
        raise ArtifactError("artifact already exists")
    try:
        dirp.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ArtifactError(f"cannot create artifact dir: {exc}") from exc

    try:
        data_path.write_bytes(data)
    except OSError as exc:
        data_path.unlink(missing_ok=True)
        raise ArtifactError(f"cannot write artifact data: {exc}") from exc
    try:
        meta_path.write_bytes(_json_bytes({
            "schema_version": _SCHEMA_VERSION,
            "artifact_id": artifact_id,
            "task_id": task_id,
            "kind": kind,
            "producer": producer,
            "relative_path": src.name,
            "sha256": sha256,
            "size_bytes": size_bytes,
            "created_at": created_at,
        }))
    except OSError as exc:
        data_path.unlink(missing_ok=True)
        meta_path.unlink(missing_ok=True)
        raise ArtifactError(f"cannot write artifact metadata: {exc}") from exc

    return ArtifactInfo(
        artifact_id=artifact_id,
        task_id=task_id,
        kind=kind,
        relative_path=src.name,
        producer=producer,
        sha256=sha256,
        size_bytes=size_bytes,
        created_at=created_at,
        data_path=str(data_path),
        metadata_path=str(meta_path),
    )


def verify(
    root: str | Path,
    task_id: str,
    kind: str,
    artifact_id: str,
) -> ArtifactInfo:
    """校验 artifact 字节完整性（重算 sha/size vs sidecar；§20.4 #24）。"""
    _check_token("task_id", task_id)
    _check_token("kind", kind)
    _check_token("artifact_id", artifact_id)
    meta = _meta_path(Path(root), task_id, kind, artifact_id)
    if not meta.is_file():
        raise ArtifactError("artifact metadata not found")
    try:
        payload = json.loads(meta.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        raise ArtifactError(f"invalid artifact metadata: {exc}") from exc
    info = _info_from_payload(Path(root), task_id, kind, payload)
    if info is None or info.artifact_id != artifact_id:
        raise ArtifactError("artifact metadata mismatch")
    data = Path(info.data_path)
    if not data.is_file():
        raise ArtifactError("artifact data not found")
    try:
        raw = data.read_bytes()
    except OSError as exc:
        raise ArtifactError(f"cannot read artifact data: {exc}") from exc
    if (hashlib.sha256(raw).hexdigest() != info.sha256
            or len(raw) != info.size_bytes):
        raise ArtifactError("artifact hash/size mismatch")
    return info


def list_artifacts(
    root: str | Path,
    task_id: str,
    *,
    kind: str | None = None,
) -> tuple[ArtifactInfo, ...]:
    """列出 task 的已 ingest artifacts；(kind, artifact_id) sorted。"""
    _check_token("task_id", task_id)
    base = Path(root) / task_id
    if not base.is_dir():
        return ()
    if kind is not None:
        _check_token("kind", kind)
        kind_dirs = [base / kind] if (base / kind).is_dir() else []
    else:
        kind_dirs = sorted(
            d for d in base.iterdir() if d.is_dir() and _TOKEN_RE.fullmatch(d.name)
        )
    found: list[ArtifactInfo] = []
    for kdir in kind_dirs:
        for meta in sorted(kdir.glob("*.json")):
            try:
                payload = json.loads(meta.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                continue  # 损坏项跳过；verify 负责显式告警
            info = _info_from_payload(Path(root), task_id, kdir.name, payload)
            # 自洽加固：sidecar 文件名须与 payload artifact_id 一致且 .data 存在
            if info is not None and meta.stem == info.artifact_id \
                    and Path(info.data_path).is_file():
                found.append(info)
    return tuple(sorted(found, key=lambda a: (a.kind, a.artifact_id)))
