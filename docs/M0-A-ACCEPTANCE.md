# AOTF M0-A 终验收对照（Pre-MVP 控制面骨架）

- 日期：2026-09-05
- 状态：M0-A 全部包闭合（A1 基座 → A5c 文档收口）
- 权威架构：`D:\tools\AOTF-Pre-MVP-architecture-and-implementation-spec-2026-08-31.md`
- 权威架构 SHA-256：`86fb98ed094fe750c8b4d7c2e54112d99370a1049e8e8f783deaeb03b4bca2f3`
- 基准：`353 passed` / 15 测试文件 / 33 文件（含本 doc）/ 仓库零 DB 生成物 / Git main·unborn·remote 空

## 1. M0-A 包完成清单

| 包 | 内容 | 证据（src / tests） |
|---|---|---|
| A1 基座 | bootstrap / canonical 确定性编码 / typed records | `canonical.py` / `models.py` / `test_canonical.py` `test_models.py` |
| A1c | SQLite 七表→八表 schema + typed store + CAS | `db.py` / `store.py` / `test_db.py` `test_store.py` |
| A2a | 状态机合法转换 + 事务化 transition | `state.py` / `test_state.py` |
| A2b | EventLedger 账本流 / 纯函数重建 / verify 对账 | `ledger.py` / `test_ledger.py` |
| A3a1–a4 | controller_leases 表/record/store/CAS + lease-fencing 服务 | `lease.py` / `test_lease.py` |
| A3b1–b2 | outbox 状态原语 + delivery（at-least-once） | `outbox.py` / `test_outbox.py` |
| A3c | recovery 诊断 + 后继工厂 | `recovery.py` / `test_recovery.py` |
| A4a | AgentRunner 协议 + MockAgentRunner | `runner.py` / `test_runner.py` |
| A4b | orchestrator 编排基元（record_run/normalize/advance） | `orchestrator.py` / `test_orchestrator.py` |
| A4c | MockAgentRunner 驱动 fault-injection drill | `test_drill.py` |
| A5a–a5b | CLI doctor/health/task status/recover/cancel | `cli.py` / `test_cli.py` |
| A5c | 本 doc + README 收口 | `docs/M0-A-ACCEPTANCE.md` |

## 2. §19 / §20 对照

### §19 M0-A 验收（无需真实 Agent 模拟）

| 场景 | 证据 |
|---|---|
| 成功 | `test_drill.py::test_drill_success_flow_to_checkpoint` |
| 失败 | `test_drill.py::test_drill_failure_leads_successor` |
| 重复事件 | `test_drill.py::test_drill_duplicate_event_replay_no_double`、`test_outbox.py` 崩溃重投去重 |
| 并发 | `test_drill.py::test_drill_dual_controller_single_lease_fencing` |
| 崩溃 | `test_drill.py::test_drill_crash_mid_transaction_deterministic` |
| 恢复 | `recovery.py` / `test_recovery.py` + A2b 投影重建 |

### §20.1 状态与并发

| 项 | 状态 | 证据 |
|---|---|---|
| #1 双 controller 单租约 | 已覆盖 | `test_lease.py`、`test_drill.py` dual-controller |
| #2 相同 event 重放无重复副作用 | 已覆盖 | `test_ledger.py`、`test_drill.py`、`test_outbox.py` |
| #3 错 expected version 不推进 | 已覆盖 | `test_store.py` CAS mismatch |
| #5 commit 前后崩溃可确定恢复 | 已覆盖 | `test_drill.py` crash-mid-transaction |
| #6 projection 删除后可从 ledger 重建 | 已覆盖（投影尺度） | `test_ledger.py::test_projection_survives_row_deletion` |
| #4 terminal 后拒非 recovery 事件 | 已覆盖（transition 层） | `test_state.py` TERMINAL_STATE |

### §20.5 Agent 与预算（M0-A 子集）

| 项 | 状态 | 证据 |
|---|---|---|
| #25 budget/deadline 可触发 | 已覆盖（基元裁决） | `orchestrator.py::normalize_status` / `test_orchestrator.py` |
| #27 Agent 失败不推进 | 已覆盖 | `orchestrator.py::advance_target` / `test_orchestrator.py` |
| #29 模型别名解析写入 run record | 已覆盖 | `orchestrator.py::record_run` / `test_orchestrator.py` |

## 3. 冻结 16 错误码使用对照

实弹命中（均有测试断言）：`INVALID_STATE_TRANSITION`、`VERSION_CONFLICT`、`TERMINAL_STATE`、`IDEMPOTENCY_CONFLICT`、`INTEGRITY_FAILURE`、`INVALID_INPUT`、`LEASE_CONFLICT`、`STALE_FENCING_TOKEN`。

保留未用（如实，非缺陷）：`DUPLICATE_EVENT`、`NOT_AUTHORIZED`、`MIGRATION_FAILED`、`BUSY_TIMEOUT`、`OUTBOX_DELIVERY_FAILED`、`BUDGET_EXCEEDED`、`DEADLINE_EXCEEDED`、`SCHEMA_VALIDATION_FAILED`——留给 M0-B..E / 真实执行层。

## 4. 诚实边界声明

下列 **不属于 M0-A**，不得据此声称已覆盖：

- 真实 Agent / Claude SDK 三角色（M0-D）；MockAgentRunner 仅为 M0-A 模拟工具；
- Git worktree / delta / evidence artifact（M0-B）、EvidenceRunner 与 Policy Engine（M0-C）；
- approval 门全流程（进入 IMPLEMENTING 的人工批准编排，§20.2 #7–#12）；
- materialization（tasks 行自动复活）、recover CLI 自动推进（§16 只提方案）；
- 真实 webhook / 外部通知（at-least-once + receiver 去重仅 fake 验证）；
- M0-E 端到端试点与任何生产仓写入。

M0-A 交付的是「控制面骨架 + 机器层 + MockAgentRunner 模拟验收证据」，可审计、可暂停、可恢复的工程流水线机器语义。

## 5. 状态真相与一致性语义

- SQLite 为唯一状态真相：`events` 账本（append-only、`UNIQUE(aggregate_type, aggregate_id, aggregate_version)`）+ `tasks` 物化投影 + CAS（`version` 乐观并发）+ 同事务事件/outbox append；
- A2a transition：CAS 更新 + 同事务 `TASK_PHASE_CHANGED` 追加，失败回滚（幂等 `IDEMPOTENCY_CONFLICT`）；
- A2b verify_task 以账本为真对账物化行（DRIFT → `INTEGRITY_FAILURE`）；投影删除后可从账本重建；
- A3a lease/fencing 单调 token 防双 controller；A3b outbox 失败不静默（failed/退避可见）；A3c recovery 诊断 + FAILED/SAFE_HALT 只经 supersede 后继出口。
