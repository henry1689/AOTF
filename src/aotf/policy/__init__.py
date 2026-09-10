"""AOTF policy 子包（M0-C4）：机械裁决与发布语义边界。

rules.py —— PolicyError + 门配置校验：required（闭环必需）/release_gates
（发布标准门）/deferred（deferred_out_of_scope，spec §11.2/#23）三集合
令牌与一致性命中。
engine.py —— evaluate：evidence + 门 + 控制面事实 → MechanicalPolicyResult
（PASS/FAIL/INCOMPLETE/ERROR）+ next-state 建议（决策表 R1–R9；#22/#23/
#31/#32 边界；无 RELEASED）。

顶层 aotf/__init__ 不引用本子包。
"""

__all__ = ["rules", "engine"]
