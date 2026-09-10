"""AOTF evidence 子包（M0-C）：证据与机械裁决。

schema.py —— TestEvidenceRecord / EvidenceCheck typed contract + 确定性
序列化（bundle_sha256 计算基座，spec §9.2）。
runner.py —— EvidenceRunner：命令注册表 + subprocess 隔离执行（产出
EvidenceCheck + stdout/stderr bytes）。
bundle.py —— produce_evidence：§9.3 顺序编排（tree/delta 绑定 + post-run
验证 #19/#21 + B6 ingest 锁定 stdout/stderr/test-evidence）。

顶层 aotf/__init__ 不引用本子包。
"""

__all__ = ["schema", "runner", "bundle"]
