"""AOTF 基础错误契约。

本模块只定义 M0 共用的错误码枚举与异常基类。不涉及 SQLite、状态机、
日志或 canonical 序列化（后者由 M0-A1b 负责）。import 无副作用。
"""

from __future__ import annotations

from copy import deepcopy
from enum import StrEnum
from types import MappingProxyType
from typing import Mapping

__all__ = ["AotfError", "ErrorCode"]


class ErrorCode(StrEnum):
    """AOTF 机器可用的错误码枚举（值 = 名称，一一对应）。"""

    INVALID_STATE_TRANSITION = "INVALID_STATE_TRANSITION"
    VERSION_CONFLICT = "VERSION_CONFLICT"
    TERMINAL_STATE = "TERMINAL_STATE"
    DUPLICATE_EVENT = "DUPLICATE_EVENT"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    INTEGRITY_FAILURE = "INTEGRITY_FAILURE"
    LEASE_CONFLICT = "LEASE_CONFLICT"
    STALE_FENCING_TOKEN = "STALE_FENCING_TOKEN"
    NOT_AUTHORIZED = "NOT_AUTHORIZED"
    INVALID_INPUT = "INVALID_INPUT"
    MIGRATION_FAILED = "MIGRATION_FAILED"
    BUSY_TIMEOUT = "BUSY_TIMEOUT"
    OUTBOX_DELIVERY_FAILED = "OUTBOX_DELIVERY_FAILED"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    DEADLINE_EXCEEDED = "DEADLINE_EXCEEDED"
    SCHEMA_VALIDATION_FAILED = "SCHEMA_VALIDATION_FAILED"


class AotfError(Exception):
    """AOTF 基础异常。

    - ``code``：机器可用的 ErrorCode；不得靠解析 message 判断错误类型。
    - ``message``：非空白人类可读文本。
    - ``details``：防御性深拷贝 + 只读 mapping 暴露；``str()`` 不展开 details。
    """

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        details: Mapping[str, object] | None = None,
    ) -> None:
        if not message or not message.strip():
            raise ValueError("message must be non-blank")
        self.code = code
        self.message = message
        self._details: dict[str, object] = (
            deepcopy(dict(details)) if details is not None else {}
        )
        super().__init__(message)

    @property
    def details(self) -> Mapping[str, object]:
        """只读 details 视图。"""
        return MappingProxyType(self._details)

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"
