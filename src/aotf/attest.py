"""AOTF 审批签名契约（P0-2a-1）。

把「人工批准」从**声明式事实**升级为**密码学事实**：operator 侧持 Ed25519
私钥签名，AOTF 侧只持公钥验签。因此 AgentRunner 子进程即便与 operator 同
OS 用户，也无法伪造有效批准（spec §16）。

设计裁决：
- 纯函数层：零 I/O（不读文件、不读环境变量）、零全局状态、无副作用；
  密钥一律以原始 bytes 传入，加载/配置归调用方。
- 确定性编码**复用** ``aotf.canonical.canonical_json_bytes``——本仓经
  ``test_canonical.py`` 验证的唯一确定性编码器；不自造第二套编码。
  该函数键序无关（sort_keys）、datetime 归一 UTC、None → JSON null，
  与 ``models._datetime``（强制 tz-aware 且 UTC 偏移为 0）契约相容。
- 签名编码：Ed25519 原始 64 字节 → base64（标准字母表，带 padding）。
- **验签 fail-closed**：``verify`` 对任何异常（畸形 key/签名、非法 base64、
  长度不符、非 bytes）一律返回 ``False``——不抛、不泄露细节、不做长度探测
  区分。此处的「不吞错误」由**返回 False 本身**承担：调用方必须把 False
  当作拒绝信号（不变量 #6 的显式例外，理由见上）。
- 私钥**永不**进日志、**永不**进异常 message；``AttestError`` 只描述入参形状。

边界：本模块不做公钥加载/路径解析、不做批准有效性判断（decision/expiry/
scope 归 store 与 orchestrate）。P0-2a-1 只建立契约；``_authorize`` 的验签
接线属 P0-2a-2。
"""

from __future__ import annotations

import base64
from datetime import datetime
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from aotf.canonical import canonical_json_bytes

__all__ = [
    "APPROVAL_ATTEST_FIELDS",
    "AttestError",
    "PRIVATE_KEY_BYTES",
    "PUBLIC_KEY_BYTES",
    "SIGNATURE_BYTES",
    "canonical_approval_payload",
    "sign",
    "verify",
]

PRIVATE_KEY_BYTES = 32
PUBLIC_KEY_BYTES = 32
SIGNATURE_BYTES = 64

#: 进入签名的审批事实字段（固定集合；顺序无关——canonical 按键排序）。
APPROVAL_ATTEST_FIELDS = (
    "approval_id",
    "task_id",
    "decision",
    "scope_sha256",
    "proposal_sha256",
    "baseline_tree",
    "created_at",
    "expires_at",
)


class AttestError(Exception):
    """审批签名契约的畸形入参拒绝（调用方 bug，非运行事实异常）。"""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def canonical_approval_payload(
    *,
    approval_id: str,
    task_id: str,
    decision: str,
    scope_sha256: str,
    proposal_sha256: str,
    baseline_tree: str,
    created_at: datetime,
    expires_at: datetime | None,
) -> bytes:
    """把审批事实编码为确定性字节——签名与验签的唯一输入。

    字段集合固定为 :data:`APPROVAL_ATTEST_FIELDS`；``expires_at=None``
    编码为 JSON ``null``（显式占位，不省略字段）。
    """
    facts: dict[str, Any] = {
        "approval_id": approval_id,
        "task_id": task_id,
        "decision": decision,
        "scope_sha256": scope_sha256,
        "proposal_sha256": proposal_sha256,
        "baseline_tree": baseline_tree,
        "created_at": created_at,
        "expires_at": expires_at,
    }
    if tuple(sorted(facts)) != tuple(sorted(APPROVAL_ATTEST_FIELDS)):
        raise AttestError("payload field set drifted from APPROVAL_ATTEST_FIELDS")
    try:
        return canonical_json_bytes(facts)
    except Exception as exc:  # naive datetime / 不支持类型 → 契约错误
        raise AttestError(
            f"payload not canonically encodable: {type(exc).__name__}"
        ) from exc


def sign(payload: bytes, private_key: bytes) -> str:
    """用 32 字节 Raw Ed25519 私钥签名，返回 base64 文本。

    畸形入参抛 :class:`AttestError`；**异常 message 绝不包含密钥材料**。
    """
    if type(payload) is not bytes:
        raise AttestError("payload must be bytes")
    if type(private_key) is not bytes or len(private_key) != PRIVATE_KEY_BYTES:
        raise AttestError("private key must be 32 raw bytes")
    try:
        key = Ed25519PrivateKey.from_private_bytes(private_key)
        signature = key.sign(payload)
    except Exception as exc:
        raise AttestError(f"signing failed: {type(exc).__name__}") from exc
    return base64.b64encode(signature).decode("ascii")


def verify(payload: bytes, signature: str, public_key: bytes) -> bool:
    """验签。任何异常一律返回 ``False``（fail-closed，见模块 docstring）。"""
    try:
        if type(payload) is not bytes:
            return False
        if type(signature) is not str:
            return False
        if type(public_key) is not bytes or len(public_key) != PUBLIC_KEY_BYTES:
            return False
        raw = base64.b64decode(signature, validate=True)
        if len(raw) != SIGNATURE_BYTES:
            return False
        Ed25519PublicKey.from_public_bytes(public_key).verify(raw, payload)
        return True
    except Exception:  # noqa: BLE001 —— fail-closed 是本函数的契约（见 docstring）
        return False
