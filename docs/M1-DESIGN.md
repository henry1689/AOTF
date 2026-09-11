# AOTF M1 设计文档 — 轻度人工干预自动化

> 版本：2026-09-11 v0.1（草稿）
> 状态：待 owner 评审
> 前置：M0-A～E 已闭合（Pre-MVP），Wenstar-cc 真实验证通过

---

## 1. 目标与边界

### 1.1 M1 目标

实现**轻度人工干预自动化**：Agent 自动执行调优任务，但关键环节需人类确认。

**核心能力**：
- ✅ 真实模型工具调用（DeepSeek via CC-Switch）
- ✅ Execution Snapshot（中间阶段崩溃可恢复）
- ✅ 重试策略（临时失败可自动重试）
- ✅ 真实中型仓试点（≥1k LOC）
- ⚠️ Approval 流程简化（CLI 签名审批，非完整 operator 编排）
- ❌ 并发控制（Lease/Fencing，M2）
- ❌ OS 级沙箱（M2）

### 1.2 不做什么

- 不实现完整 operator 审批编排（M2）
- 不实现跨控制器并发（M2）
- 不实现自动发布（M3）
- 不替换 Wenstar-cc 的 Harness（AOTF 是独立系统）

### 1.3 成功标准

| 指标 | M1 目标 |
|---|---|
| Live 落盘成功率 | ≥80%（10 次真实任务）|
| 平均 token/任务 | <50k input + <10k output |
| 平均成本/任务 | <$2 |
| 平均耗时/任务 | <5 分钟 |
| 中型仓试点 | 至少 1 个 ≥1k LOC 仓通过闭环 |
| 人工介入次数 | 每任务 ≤2 次（approval + review）|

---

## 2. 架构变更

### 2.1 整体变更图

```
M0（当前）                          M1（目标）
─────────────────────────────────────────────────────
┌──────────────┐                  ┌──────────────┐
│  Pilot Layer │                  │  Pilot Layer │
│  (fake/live) │                  │  (fake/live) │
└──────┬───────┘                  └──────┬───────┘
       │                                 │
┌──────▼───────┐                  ┌──────▼───────┐
│  Engine      │                  │  Engine      │
│  (orchestrate)│                  │  (orchestrate)│
│  - 单 turn   │                  │  - 单 turn   │
│  - 不可重入  │          →       │  - snapshot  │
│              │                  │  - retry     │
└──────┬───────┘                  └──────┬───────┘
       │                                 │
┌──────▼───────┐                  ┌──────▼───────┐
│  Agent SDK   │                  │  Agent SDK   │
│  (claude-sdk)│                  │  (claude-sdk)│
│  - 零工具    │                  │  - 零工具    │
│  - 无状态    │                  │  + snapshot  │
└──────────────┘                  └──────────────┘
```

### 2.2 新增模块

| 模块 | 路径 | 职责 |
|---|---|---|
| `snapshot.py` | `src/aotf/snapshot.py` | Execution Snapshot 持久化/恢复 |
| `retry.py` | `src/aotf/retry.py` | 重试策略（指数退避 + 错误分类）|
| `approval_cli.py` | `src/aotf/approval_cli.py` | CLI 审批工具（签名 + 验证）|
| `middleware_ccswitch.py` | `src/aotf/agents/middleware_ccswitch.py` | CC-Switch 路径适配 |

### 2.3 修改模块

| 模块 | 变更 |
|---|---|
| `orchestrate.py` | 接入 snapshot/retry |
| `pilot/adapter.py` | 接入 CC-Switch 路径 |
| `pilot/run.py` | 新增 live 中型仓测试 |
| `models.py` | 新增 `SnapshotRecord` typed contract |

---

## 3. 详细设计

### 3.1 Execution Snapshot

**问题**：当前中间阶段（IMPLEMENTING→POLICY_EVALUATING）崩溃后不可恢复，必须 SAFE_HALT。

**方案**：每次状态转换后持久化执行上下文。

#### 3.1.1 Data Model

```python
@dataclass(frozen=True, slots=True)
class SnapshotRecord:
    """单次 agent run 的完整执行上下文（可重入恢复）。"""
    snapshot_id: str
    task_id: str
    role: str  # "implementer" | "reviewer"
    phase: str  # 进入该 phase 前的状态
    worktree_path: str
    baseline_tree: str
    actual_tree: str | None  # implementer 完成后
    mutations_applied: tuple[str, ...] | None  # 已应用的文件路径
    delta_sha256: str | None
    evidence: EvidenceBundleResult | None
    review_verdict: str | None  # "PASS" | "CONCERNS" | "BLOCK"
    run_inputs: dict  # 原始 inputs（用于重放）
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        _token("snapshot_id", self.snapshot_id)
        _token("task_id", self.task_id)
        _token("role", self.role)
        _token("phase", self.phase)
        _abs_no_parent(self.worktree_path, "worktree_path")
        # ... 其他校验
```

#### 3.1.2 持久化策略

```python
# snapshot.py
def save_snapshot(conn, record: SnapshotRecord) -> None:
    """原子写入 snapshot 表（INSERT 或 UPDATE）。"""
    conn.execute("""
        INSERT OR REPLACE INTO snapshots 
        (snapshot_id, task_id, role, phase, worktree_path, 
         baseline_tree, actual_tree, mutations_applied, delta_sha256,
         evidence_json, review_verdict, run_inputs, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, to_row(record))
    conn.commit()

def load_snapshot(conn, task_id: str, role: str) -> SnapshotRecord | None:
    """加载最近的 snapshot（用于重入恢复）。"""
    row = conn.execute(
        "SELECT * FROM snapshots WHERE task_id=? AND role=? ORDER BY updated_at DESC LIMIT 1",
        (task_id, role)
    ).fetchone()
    return from_row(row) if row else None
```

#### 3.1.3 Engine 集成

```python
# orchestrate.py 修改
async def _run_agent(self, role: str, inputs: dict) -> RoleOutcome:
    # 检查是否有未完成的 snapshot
    snapshot = load_snapshot(self.conn, self.plan.task_id, role)
    if snapshot and snapshot.phase == self.task.phase.value:
        # 恢复上下文（跳过已完成的步骤）
        return await self._resume_from_snapshot(snapshot)
    
    # 正常执行
    outcome = await self._execute_agent(role, inputs)
    
    # 持久化 snapshot
    save_snapshot(self.conn, SnapshotRecord(
        snapshot_id=_new_id(),
        task_id=self.plan.task_id,
        role=role,
        phase=self.task.phase.value,
        # ... 填充字段
    ))
    
    return outcome
```

### 3.2 重试策略

**问题**：当前任何失败 → SAFE_HALT，无法处理临时错误（网络抖动、API 限流）。

**方案**：错误分类 + 指数退避重试。

#### 3.2.1 错误分类

```python
# retry.py
class RetryableError(Exception):
    """可重试错误基类。"""
    pass

class RateLimitError(RetryableError):
    """API 限流。"""
    pass

class TemporaryNetworkError(RetryableError):
    """网络临时故障。"""
    pass

class BudgetExceededError(Exception):
    """预算超限（不可重试）。"""
    pass

class ScopeViolationError(Exception):
    """越界修改（不可重试）。"""
    pass
```

#### 3.2.2 重试逻辑

```python
MAX_RETRIES = 3
BASE_DELAY_S = 2

async def run_with_retry(
    coro_func, 
    role: str, 
    inputs: dict,
    max_retries: int = MAX_RETRIES,
) -> RoleOutcome:
    """带重试的 agent 执行。"""
    last_exc = None
    
    for attempt in range(max_retries):
        try:
            return await coro_func(role, inputs=inputs)
        except (RateLimitError, TemporaryNetworkError) as exc:
            last_exc = exc
            if attempt < max_retries - 1:
                delay = BASE_DELAY_S * (2 ** attempt)
                await asyncio.sleep(delay)
                continue
            raise
        except (BudgetExceededError, ScopeViolationError):
            # 不可重试
            raise
        except Exception as exc:
            # 未知错误，记录但不重试
            log.warning(f"Unexpected error in {role}: {exc}")
            raise
    
    # 所有重试耗尽
    raise last_exc
```

#### 3.2.3 Engine 集成

```python
# orchestrate.py 修改
async def _run_agent(self, role: str, inputs: dict) -> RoleOutcome:
    try:
        return await run_with_retry(
            self._execute_agent,
            role=role,
            inputs=inputs,
            max_retries=self.cfg.agent_max_retries,
        )
    except (BudgetExceededError, ScopeViolationError) as exc:
        self._stop(TaskPhase.FAILED, f"{role}: {exc}")
    except RetryableError as exc:
        self._stop(TaskPhase.SAFE_HALT, f"{role} retried exhausted: {exc}")
```

### 3.3 CC-Switch 路径适配

**背景**：Wenstar-cc 已通过 CC-Switch 验证 DeepSeek-V4-flash 工具调用正常。需复用该路径。

#### 3.3.1 环境探测

```python
# middleware_ccswitch.py
import os
from pathlib import Path

def detect_ccswitch_path() -> Path | None:
    """探测 CC-Switch 安装路径。"""
    candidates = [
        Path(os.environ.get("CC_SWITCH_PATH", "")),
        Path.home() / ".cc-switch" / "bin" / "claude",
        Path("/usr/local/bin/claude"),
    ]
    for path in candidates:
        if path.exists() and path.is_file():
            return path
    return None

def get_ccswitch_env() -> dict[str, str]:
    """获取 CC-Switch 所需的 env 变量。"""
    env = os.environ.copy()
    env.update({
        "ANTHROPIC_MODEL": "DeepSeek-V4-flash",
        "ANTHROPIC_BASE_URL": "https://api.deepseek.com/anthropic",
        "CLAUDE_CODE_ENTRYPOINT": "aotf",  # 标识来源
    })
    return env
```

#### 3.3.2 SDK Runner 修改

```python
# agents/claude_sdk.py 修改
class ClaudeSdkRunner:
    def __init__(self, ...):
        # ...
        self._ccswitch_path = detect_ccswitch_path()
        self._ccswitch_env = get_ccswitch_env()
    
    async def run(self, ctx: SdkRunContext) -> SDKRunOutcome:
        if self._ccswitch_path:
            # 通过 CC-Switch 启动
            return await self._run_via_ccswitch(ctx)
        else:
            # 直连 Anthropic API
            return await self._run_direct(ctx)
    
    async def _run_via_ccswitch(self, ctx: SdkRunContext) -> SDKRunOutcome:
        """通过 CC-Switch 子进程启动 Claude Code。"""
        proc = await asyncio.create_subprocess_exec(
            str(self._ccswitch_path),
            "--output", "json",
            "--no-sandbox",
            "--model", ctx.requested_model,
            "--max-turns", str(ctx.max_turns),
            "--budget", str(ctx.max_budget_usd),
            input=self._build_prompt(ctx),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=ctx.cwd,
            env=self._ccswitch_env,
        )
        # ... 解析输出
```

### 3.4 Approval CLI

**目标**：operator 通过 CLI 提交签名审批，无需完整 operator 编排。

#### 3.4.1 CLI 接口

```python
# approval_cli.py
import argparse
from cryptography.hazmat.primitives.serialization import load_pem_private_key

def cmd_approve(args):
    """提交审批。"""
    # 1. 加载私钥
    private_key = load_pem_private_key(args.key_file, password=None)
    
    # 2. 构建审批事实
    approval = ApprovalRecord(
        approval_id=f"ap-{uuid.uuid4().hex[:12]}",
        task_id=args.task_id,
        decision="approved",
        scope_sha256=calculate_scope_hash(args.allowed_files, args.proposal),
        proposal_sha256=hashlib.sha256(args.proposal.encode()).hexdigest(),
        baseline_tree=args.baseline_tree,
        source="human",
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=args.expiry_minutes),
        created_at=datetime.now(timezone.utc),
    )
    
    # 3. 签名（Ed25519）
    payload = canonical_approval_payload(**approval_to_dict(approval))
    signature = sign(payload, private_key.private_bytes())
    
    # 4. 存储
    store.insert_approval(conn, approval)
    store.insert_signature(conn, SignatureRecord(
        approval_id=approval.approval_id,
        signature=signature,
        public_key=private_key.public_key().public_bytes(
            Encoding.Raw, PublicFormat.Raw
        ),
    ))
    
    print(f"Approval created: {approval.approval_id}")
    print(f"Expires: {approval.expires_at.isoformat()}")

def cmd_verify(args):
    """验证审批签名。"""
    approval = store.get_approval(conn, args.approval_id)
    sig = store.get_signature(conn, args.approval_id)
    
    if verify(approval_to_bytes(approval), sig.signature, sig.public_key):
        print("✓ Signature valid")
    else:
        print("✗ Signature INVALID")
        sys.exit(1)
```

#### 3.4.2 Engine 集成

```python
# orchestrate.py 修改
def _authorize(self) -> None:
    now = _now()
    approval = get_approval(self.conn, self.task.authorization_id)
    if approval is None:
        self._stop(TaskPhase.SAFE_HALT, "approval missing")
    
    # 验证签名
    sig = get_signature(self.conn, approval.approval_id)
    if sig is None or not verify_approval(approval, sig):
        self._stop(TaskPhase.SAFE_HALT, "approval signature invalid")
    
    # 其他校验（过期、scope）...
    self._move(TaskPhase.IMPLEMENTING)
```

---

## 4. 中型仓试点

### 4.1 选择标准

- ≥1k LOC（排除 240 LOC demo 仓）
- 有真实测试套件
- 有明确可优化的模块
- 作者允许修改（开源或内部工具）

### 4.2 候选项目

| 项目 | LOC | 语言 | 测试 | 备注 |
|---|---|---|---|---|
| `wenstar-cc` | ~15k | TS/Python | pytest/vitest | 主项目，需谨慎 |
| `aotf` 自身 | ~3k | Python | pytest | 自举测试 |
| 内部小工具 | 待选 | - | - | 需 owner 提供 |

### 4.3 试点任务设计

```python
# pilot/tasks.py 新增
MEDIUM_CARGO_TASKS = {
    "refactor-error-handling": {
        "id": "medium-refactor-errors",
        "proposal": """
在 src/aotf/errors.py 中统一错误码命名规范：
- ErrorCode.AB_C 改为 ErrorCode.AB_C（现有）
- 新增 ErrorCode.MIGRATION_FAILED, ErrorCode.SNAPSHOT_CORRUPTED
- 确保所有 raise AotfError(ErrorCode.XXX) 使用新命名
""",
        "allowed_files": (
            "src/aotf/errors.py",
            "src/aotf/models.py",
            "src/aotf/controller.py",
            "src/aotf/orchestrate.py",
        ),
        "test_plan": (("unit", "pytest tests/unit/test_errors.py"),),
        "release_gates": (),
        "deferred": (),
        "fault": None,
        "budget_usd": Decimal("5"),
    },
}
```

---

## 5. 测试策略

### 5.1 新增测试文件

| 文件 | 覆盖 |
|---|---|
| `test_snapshot.py` | Snapshot 持久化/恢复 |
| `test_retry.py` | 重试逻辑 + 退避 |
| `test_approval_cli.py` | CLI 命令 + 签名验证 |
| `test_ccswitch.py` | CC-Switch 路径探测 |
| `test_medium_cargo.py` | 中型仓试点 |

### 5.2 Live 测试矩阵

```python
# 测试配置
LIVE_TESTS = [
    {"name": "simple_edit", "cargo": "wenstar-cc", "task": "fix-typo"},
    {"name": "refactor", "cargo": "aotf", "task": "refactor-error-handling"},
    {"name": "bugfix", "cargo": "wenstar-cc", "task": "fix-null-check"},
]
```

### 5.3 验收标准

- 所有 unit test 通过（686+）
- Live 测试 10 次 ≥8 次成功
- 中型仓试点 ≥1 个通过闭环
- 人工介入 ≤2 次/任务

---

## 6. 实施计划

### 6.1 里程碑分解

| 周 | 任务 | 交付物 |
|---|---|---|
| W1 | Snapshot + Retry | `snapshot.py`, `retry.py`, 单元测试 |
| W1 | CC-Switch 适配 | `middleware_ccswitch.py`, SDK 修改 |
| W2 | Approval CLI | `approval_cli.py`, CLI 集成 |
| W2 | Engine 集成 | `orchestrate.py` 修改 |
| W3 | 中型仓试点 | `MEDIUM_CARGO_TASKS`, live 测试 |
| W3 | 文档 + 验收 | `M1-ACCEPTANCE.md` |

### 6.2 风险与缓解

| 风险 | 概率 | 影响 | 缓解 |
|---|---|---|---|
| CC-Switch 路径不稳定 | 中 | 高 | 回退到直连 API |
| DeepSeek 限流 | 高 | 中 | 重试 + 预算放宽 |
| 中型仓测试失败 | 中 | 低 | 多仓选择 |
| Snapshot 恢复不一致 | 低 | 高 | 严格测试重入场景 |

---

## 7. 后续演进（M2/M3）

### M2（自治）

- Lease/Fencing 接线
- Outbox/Recovery 完整实现
- 自动重试策略增强
- 监控告警（webhook）

### M3（生产）

- 并发控制器支持
- OS 级沙箱（seccomp/jail）
- Web UI（审批面板）
- 自动发布（merge PR）

---

## 8. 附录

### 8.1 相关文档

- `D:\tools\AOTF-Pre-MVP-architecture-and-implementation-spec-2026-08-31.md`
- `D:\tools\aotf\docs\M0-E-ACCEPTANCE.md`
- `D:\tools\aotf\docs\HARDENING-2026-09-09.md`

### 8.2 术语表

| 术语 | 含义 |
|---|---|
| CC-Switch | Claude Code Switch，DeepSeek 工具调用代理 |
| Snapshot | Execution Snapshot，中间状态持久化 |
| Worktree | Git worktree，任务隔离沙箱 |
| Delta | 基线到 checkpoint 的文件变更集合 |

---

**起草人**：Agnes（AI Assistant）
**评审**：待 owner 确认
**下一步**：评审后开始 W1 实施
