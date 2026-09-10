"""AOTF agents 子包（M0-D）：真实 Claude SDK 三角色的执行与角色层。

schema.py —— SdkRunContext/SDKRunOutcome typed 契约 + SdkRunner 面（两域
分离：A4a 编排面 ↔ agents SDK 执行面）。
fake.py —— FakeClaudeSDK：确定性 SDK 门（turns/budget enforce）。
claude_sdk.py —— 真实 claude-agent-sdk adapter（ClaudeSdkRunner + A4a
mapper + 别名解析；延迟 import SDK）。
roles.py —— 三角色 adapter（Planner/Implementer/Reviewer role spec +
输入清单，§4.1/#28）。
reports.py —— 三角色输出 typed + parse fail-closed（#26，§11.1 verdict）。
tools.py —— 工具权限矩阵（visible_tools 过滤 + can_use_tool canonical 判定
+ Bash 白名单，§7.3/#30/#12/#10）。

顶层 aotf/__init__ 不引用本子包。
"""

__all__ = ["schema", "fake", "claude_sdk", "roles", "reports", "tools"]
