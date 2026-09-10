# AOTF M0-D 终验收对照（真实 Claude SDK 三角色与工具权限）

> 历史验收快照（2026-09-06）。2026-09-09 已修正空工具集、结果通道、
> 工具审计及 Claude Code 接管风险；当前生产 adapter 已改为模型单 turn、
> `tools=[]`、controller-owned mutation，所以下文“Implementer 直接
> Edit/Write”只保留为历史记录，不再描述当前运行路径。当前实现与验证以
> `HARDENING-2026-09-09.md` 为准。

- 日期：2026-09-06
- 状态：M0-D 全部包闭合（D1 SDK 执行面 → D5 文档收口）
- 权威架构：`D:\tools\AOTF-Pre-MVP-architecture-and-implementation-spec-2026-08-31.md`
- 权威架构 SHA-256：`86fb98ed094fe750c8b4d7c2e54112d99370a1049e8e8f783deaeb03b4bca2f3`
- 基准：`620 passed + 1 skipped` / 32 测试文件 / 77 文件（含本 doc）/ 仓库零 DB 生成物 / Git `main`·unborn·remote 空

## 1. M0-D 包完成清单

| 包 | 内容 | 证据（src / tests） |
|---|---|---|
| D1 | SDK 执行面 typed 契约（SdkRunContext/SDKRunOutcome）+ FakeClaudeSDK（SDK 门） | `agents/schema.py` `agents/fake.py` / `test_agents_schema.py` `test_agents_fake.py` |
| D2 | 真实 claude-agent-sdk adapter（ClaudeSdkRunner + A4a mapper + 别名解析） | `agents/claude_sdk.py` / `test_agents_claude_sdk.py` |
| D3 | 三角色 adapter（Planner/Implementer/Reviewer role spec + typed IO，#28/#26） | `agents/roles.py` `agents/reports.py` / `test_agents_roles.py` `test_agents_reports.py` |
| D4 | 工具权限矩阵（visible_tools 过滤 + can_use_tool canonical 判定 + Bash 白名单） | `agents/tools.py` / `test_agents_tools.py` |
| D5 | 本 doc + README 收口 | `docs/M0-D-ACCEPTANCE.md` |

## 2. spec §19 M0-D 内容对照

| spec 内容 | 覆盖 | 证据 |
|---|---|---|
| Planner/Implementer/Reviewer adapter | 已覆盖 | `role_system_prompt`/`ROLE_SYSTEM_PROMPTS`（§4.1 职责编码）+ 输入清单 `render_role_inputs`；`test_agents_roles.py` |
| typed input/output schema | 已覆盖 | `PlannerInputs/ImplementerInputs/ReviewerInputs` + `PlannerReport/ImplementerReport/ReviewerReport` + `parse_*` fail-closed（#26）；`test_agents_reports.py` |
| tool visibility / dontAsk / can_use_tool dynamic path callback | 已覆盖 | `filter_visible_tools`/`decide_tool` + `ClaudeSdkRunner` can_use_tool 闭包接线（PermissionResultAllow/Deny）；`test_agents_tools.py::test_can_use_tool_wiring_allow/deny` |
| max turns / budget / deadline | 已覆盖（分层） | SDK 门：`test_agents_fake.py::test_fake_turns_exceed_timeout`/`test_fake_turns_checked_before_budget`（turns→TIMEOUT 先）/`test_fake_budget_exceeded`/`test_fake_zero_budget_no_limit`（budget→BUDGET_EXCEEDED 后、0=未设）/`test_fake_non_completed_reason_kept`；真实 budget 兜底 `test_agents_claude_sdk.py::test_runner_cost_over_budget_mapped`；deadline 归 A4b `normalize_status`（M0-A） |
| fake SDK 与真实受控样例 | 已覆盖 | `FakeClaudeSDK`（SdkRunner 面，可替换）；`test_live_controlled_example`（ANTHROPIC_API_KEY 门控，默认 skip——零真实 token） |

## 3. spec §7 对照

| spec | 落点 |
|---|---|
| §7.1 选型 | 真实 Python Claude Agent SDK 包 `claude-agent-sdk`（0.2.152，唯一运行时依赖，owner 授权）；非 subprocess。**探针定案**：本环境 WebFetch 域不可达 → API 形状以安装后反射实测为准（query async 流 / ResultMessage{num_turns,is_error,session_id,errors,total_cost_usd,model_usage} / ClaudeAgentOptions 全字段 / ModelUsage TypedDict camelCase 含 canonicalModel / TERMINAL_TASK_STATUSES={completed,stopped,killed,failed}） |
| §7.2 抽象接口 | **两域分离**：A4a（`runner.py` AgentRunRequest/Result + AgentRunner Protocol + MockAgentRunner，M0-A 冻结）= 编排面；agents `SdkRunContext/SDKRunOutcome/SdkRunner` = SDK 执行面；`to_agent_run_result` SDKReason→A4a status 五映射桥接；模型别名不写死（`ALIAS_MODELS`/`resolve_model`），resolved 经 `resolved_model_from_result`（model_usage keys/canonicalModel）；M0-E 组装 |
| §7.3 权限原则 | `tools` 可见=ctx.visible_tools→options.tools（空=`[]`，明确禁用全部）；`allowed_tools` 预批准=allowed_tools 透传（≠限制）；dontAsk 忠实透传；Reviewer/Planner 显式仅见 Read/Glob/Grep；Implementer 显式增加 Edit/Write，写工具再经 can_use_tool canonical path 判定（resolve+normcase 前缀+allowed_files）；不用 bypassPermissions/acceptEdits；通用 Bash 与网络工具不在默认可见集 |

## 4. §20.5 Agent 与预算对照

| 项 | 状态 | 证据 |
|---|---|---|
| #25 max turns/budget/deadline 分别可触发 | 已覆盖（分层） | turns/budget：`test_agents_fake.py::test_fake_turns_exceed_timeout`/`test_fake_turns_checked_before_budget`/`test_fake_budget_exceeded`/`test_fake_zero_budget_no_limit`；真实 budget 兜底 `test_agents_claude_sdk.py::test_runner_cost_over_budget_mapped`；deadline：A4b `normalize_status`（M0-A 已覆） |
| #26 Agent schema 输出非法不推进 | 已覆盖 | D3 `parse_planner/implementer/reviewer_report` fail-closed；`test_agents_reports.py::test_*_rejected`（bad outcome/verdict/findings null/非 dict → ValueError） |
| #27 Agent 运行失败产生唯一失败事件 | 已覆盖（M0-A 基元） | A4b `advance_target`（非 completed 不推进，M0-A ACCEPTANCE） |
| #28 Reviewer 不读取 Implementer subjective summary | 已覆盖（双保险） | 结构：`ReviewerInputs` 无 summary 字段（`test_agents_roles.py::test_reviewer_inputs_no_summary_field`）；prompt 文本：「不得读取 Implementer 的主观总结」（`test_reviewer_prompt_no_subjective_summary`）；`ImplementerReport.summary` 标注仅供 controller/human |
| #29 模型别名解析结果写入 run record | 已覆盖（分层） | A4b `record_run` 落 resolved_model（M0-A）；D2 `resolve_model`/`resolved_model_from_result` 提供真实解析 |
| #30 网络工具默认不可见 | 已覆盖 | `filter_visible_tools` 默认去 NETWORK_TOOLS；`test_agents_tools.py::test_filter_network_removed_by_default`/`test_decide_network_denied` |

## 5. §20.2 授权与边界（子集）

| 项 | 状态 | 证据 |
|---|---|---|
| #10 Edit/Bash/symlink/rename 越界均检测 | 机制覆盖（工具面） | `decide_tool`：Bash 默认禁 `bash-disabled`；写工具 canonical（resolve(strict=False) 展开 symlink → worktree 前缀 + allowed_files 集内）→ 越界 `traversal`/`out-of-scope`；`test_agents_tools.py::test_decide_edit_traversal_denied`/`test_decide_bash_denied_by_default`/`test_decide_edit_out_of_scope`。真实 symlink 建链端到端与 rename 工具级拦截留 M0-E（OS sandbox/SDK hook 层） |
| #12 Reviewer 和 Planner 无源码写工具 | 已覆盖 | `decide_tool` role-read-only + `filter_visible_tools` role 约束；`test_agents_tools.py::test_decide_reviewer_write_denied`/`test_decide_planner_write_denied`/`test_filter_reviewer_no_write` |

## 6. 诚实边界声明

下列 **不属于 M0-D**，不得据此声称已覆盖：

- orchestrator 全流程接线（AgentRunRequest→SdkRunContext→SdkRunner→SDKRunOutcome→`to_agent_run_result`→A4b record_run/推进的组装、POLICY_EVALUATING 调度、DB run 事件）——M0-E；
- 真实 Claude SDK 端到端运行与受控试点（需 ANTHROPIC_API_KEY；live 测试默认 skip）——M0-E；
- 真实 symlink 建链端到端、自定义 command-id 诊断工具实现、rename 工具级越界拦截（OS sandbox/SDK hook 层）——M0-E；
- approval 门全流程编排（进入 IMPLEMENTING 人工批准与 EDIT_AUTHORIZED 接线）、materialization、recover CLI 自动推进、真实 webhook——M0-E；
- Gate Explainer（M2，LLM 解释层）；prompts/*.md 文件拆分（§15 生产参考，M0-D 用 Python 常量）；
- A4a/A4b（M0-A 交付）复用非 M0-D 重写。

注记（环境事项，非 M0-D 机器缺陷）：`.gitignore` 的 `artifacts/` 无前导 `/` 误忽略 `src/aotf/artifacts/*` 代码包 2 文件（git untracked=75 vs 文件系统 77，M0-D 态含本 doc）——owner 待决（建议 `/artifacts/`）；历史 `__pycache__` 残留（非 M0-D 制造，agents 触碰模块零 pyc）。均不影响本验收声明。

## 7. 事实语义与不变量

- **两域分离**：A4a 编排面（M0-A 冻结，`runner.py`/`orchestrator.py` hash 全程未变）↔ agents SDK 执行面（D1 新增）；`to_agent_run_result` 为唯一编排桥（单向 → runner 只读值对象）；
- **探针定案**：环境 WebFetch 域不可达 → claude-agent-sdk 0.2.152 API 形状以安装后反射实测为准（本 doc §3 记录）；SDK 无「成功但超 turns」态 → 超 turns 由 Claude Code 以 error 呈现、deadline 超时归 A4b（reason 校订记录于 claude_sdk docstring）；
- **依赖**：唯一运行时依赖 `claude-agent-sdk`（pyproject 声明；test_package 白名单化例外，owner 知情）；agents 子包内 schema/fake/claude_sdk/roles/reports/tools 分层，claude_sdk 顶层零 import SDK（延迟）、`_query_fn`/`tool_decider` 注入零真实请求；
- **默认零 token**：live 受控样例 ANTHROPIC_API_KEY 门控默认 skip（CI 620 passed+1 skipped 无真实 API 调用）；
- **失败不冒泡语义**：SDK 异常 → ERROR outcome（error_code `sdk_error:<type>`）；handler 异常 → ERROR（`handler_error`）；
- **确定性门**：FakeClaudeSDK SDK 门（turns→TIMEOUT 先、budget→BUDGET 后、仅 completed 可覆写、0 budget=未设）；decide_tool 判定序（invalid-tool→role-read-only→network→bash→canonical→ok）；PolicyError/ValueError 域清晰；
- **#28/#26 结构保证**：ReviewerInputs 无 summary 字段；parse_* 对缺字段/非法枚举/空消息/null findings fail-closed；
- **工具判定 deny 方向安全**：canonical 判定 spurious-deny 可能、无越权 allow 路径；
- **测试隔离**：GitSandbox tmp 一次性仓或纯构造；零真实仓触碰；`run_git` GIT_OPTIONAL_LOCKS=0 只读保证沿用；
- **编码惯例**：sorted-json/UTC/CRLF 归一沿用（M0-A/C 定案）。
