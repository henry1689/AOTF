"""AOTF artifacts 子包（M0-B6）：filesystem append-only artifact store。

store.py —— ingest/verify/list_artifacts（typed kind + producer 身份 +
task 绑定 + sha/size/created_at + 确定性 sidecar）。

顶层 aotf/__init__ 不引用本子包。
"""

__all__ = ["store"]
