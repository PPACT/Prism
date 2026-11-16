# 异构 Agent 嗅探层设计草案

> 状态：**草案，未实施**。依赖 `psutil` 尚未获批准。
> 日期：2026-11-10
> 范围：C1/C2 Console 的 Agent 发现能力扩展；不改变现有 MCP 注册与任务路由协议。
> 关联代码：`tools/orchestrator/service.py`、`tools/orchestrator/registry.py`、`tools/orchestrator/mcp_server.py`、`experiments/frontend-dashboard-tauri/src/{App.tsx,lib/backend.ts}`。

## 1. 背景与问题

当前「检测 Agent」只能识别两类来源：

1. MCP `orchestrator_register` 自报身份的 Claude Code 会话；
2. 这些会话写入 `.claude/orchestrator/agents/<session>.json` 的角色缓存。

这带来明确局限：

- 用户使用 **codex、pi 或手写 Python Agent** 时，中控台完全看不到它们；
- 没有任何注册协议的进程无法被现有逻辑发现；
- 「检测 Agent」实际上只做了「重新探测已登记 pid」，不是真正的发现。

目标是让 AgentSentinel 成为**异构 Agent 的中控平台**：既能识别自注册 Agent，也能发现项目内运行的野生活跃进程，并由用户手动绑定角色。

## 2. 目标与非目标

### 目标

- 基于 **进程 cwd + 进程树** 发现工作目录位于当前项目根目录内的活跃进程；
- 不依赖进程名（node/claude/python）匹配；
- 提供 `AgentDriver` 抽象：MCP 注册是一个 driver，未来 Codex 是另一个 driver；
- `/discover` 聚合返回已注册 Agent 与「未注册野生进程」；
- 用户在中控台手动把野生 PID 绑定为 `coder` / `tester` 等角色；
- 扫描只读取进程的 `pid/ppid/cwd/cmdline/create_time`，不读取环境变量、内存、文件或网络。

### 非目标

- 不自动把野生进程注册为 Agent（必须人工确认）；
- 不控制、不注入、不终止被发现的外部进程；
- 不替代 MCP 心跳/角色缓存；两者共存；
- 本轮不实现 CodexDriver 的具体逻辑，只预留接口。

## 3. 产品边界：中控只观察与调度，不终止外部进程

> 2026-11-10 用户拍板；与实现基线 `ffed730` 一致。本草案的嗅探、绑定只增加可观察性，不改变进程控制权。

- **停止范围**：`POST /stop` 仅对 `job_id` 非空的 daemon 托管 worker 生效；外部 Agent（MCP 会话、野生/手动绑定进程等 `job_id` 为空）返回 **409**，`reason="external_agent"`，不改状态、不杀进程（`tools/orchestrator/service.py:718-741`）。
- **前端呈现**：托管 worker 显示「停止」按钮；外部 Agent 操作列显示「外部进程」，不提供停止按钮（`experiments/frontend-dashboard-tauri/src/components/AgentTable.tsx:43,71-86`）。
- **禁止项**：不杀 MCP server、不注入进程、不新增「退出协议」；外部窗口由用户自行 Ctrl+C / 关闭。
- **UI 入口**：暂不提供 UI 启动托管 worker 的入口；托管 worker 仍由 daemon/CLI 创建。
- **与嗅探的关系**：发现、扫描、手动绑定只做观察与标注；绑定后仍不可停止（见 §11 安全边界、§8.2「不终止被绑定进程」）。

## 4. 依赖申请（待批准）

```
[依赖申请]：psutil，最新稳定版（建议 >=6.0）
- 原因：跨平台读取进程 pid/ppid/cwd/cmdline/create_time。
  Windows 标准库与 WMI Win32_Process 都不提供其他进程的 cwd。
- 兼容性：纯 Python/C 扩展，独立于 AgentScope，不影响 agentscope[full]==2.0.8；
  Python 3.12 有官方 wheel。
- 安装位置：仅项目 .venv（uv pip install psutil）。
```

**在用户回复“同意”之前，不安装、不写实现代码。**

## 5. 总体架构

```
/discover
   │
   ├── ClaudeDriver        # 现有 MCP 注册/心跳/角色缓存
   ├── CwdProcessDriver    # psutil CWD + 进程树扫描（本草案新增）
   └── CodexDriver         # 预留，后续实现
   │
   ▼
DriverRegistry → 聚合候选 → daemon 校验/绑定 → Registry(SQLite) → /status / SSE
```

### 5.1 AgentDriver 接口

```python
@dataclass(frozen=True)
class AgentCandidate:
    pid: int
    ppid: int
    cwd: str
    cmdline: str
    create_time: float
    source: str                 # "claude" / "cwd" / "codex"
    role: str = ""              # 已注册或已绑定的角色；野生候选为空
    registered: bool = False    # True = 已登记/已绑定；False = 野生候选
    root: bool = True           # 进程树根
    child_pids: tuple[int, ...] = ()


class AgentDriver(Protocol):
    name: str

    def discover(self, project: str) -> list[AgentCandidate]:
        """返回本 driver 能发现的 Agent 或候选；只读，不修改被扫描进程。"""
```

### 5.2 ClaudeDriver

- 包装现有 `_reconcile_agent_cache()` 与 registry 读取；
- `discover()` 返回已注册/缓存中可恢复的 Claude Agent（`registered=True`）；
- 现有 MCP 15s 心跳与缓存写入逻辑**保持不变**，仍属于注册侧；
- 本重构只把「发现/恢复」部分抽到 driver，避免 `/discover` 直接写死 Claude 逻辑。

### 5.3 CwdProcessDriver

- 使用 `psutil` 扫描全系统进程；
- 返回 `registered=False` 的野生候选；
- 默认只返回进程树根，子进程折叠为 `child_pids`；
- 不按进程名过滤。

### 5.4 CodexDriver（预留）

- 未来读取 Codex 的会话/进程/配置文件；
- 与 `CwdProcessDriver` 返回相同的 `AgentCandidate`，daemon 侧无需改动；
- 具体实现另开设计文档。

## 6. CWD + 进程树扫描算法

### 6.1 扫描步骤

1. `psutil.pids()` 获取全部 pid；
2. 对每个 pid 仅调用：
   - `Process(pid).cwd()`
   - `Process(pid).cmdline()`
   - `Process(pid).ppid()`
   - `Process(pid).create_time()`
3. 过滤：`cwd` 必须位于当前项目根目录下（`Path(cwd).resolve()` 前缀匹配，Windows 用 `normcase`）；
4. 构建项目内进程森林；
5. 排除基础设施；
6. 返回 root 候选。

### 6.2 进程树分组

- `root = cwd 在项目内 且（父进程不存在 或 父进程 cwd 不在项目内）`；
- 子进程不单独列为候选，记录到 root 的 `child_pids`；
- 若 root 的后代包含**已注册 pid**，视为已注册会话，不进入野生列表；
- 若一个野生进程有多个项目内子进程，只在 root 行展示子进程数量。

示例：

```
claude.exe (cwd=项目根, 子进程含 mcp_server.py 已注册 pid)
  └── python.exe mcp_server.py        ← 已注册，整棵树隐藏

bash.exe (cwd=C:\Users\...)
  └── python.exe my_agent.py (cwd=项目根)  ← 野生 root，展示
```

### 6.3 排除规则

以下进程及其子树不进入野生候选：

- daemon 自身 pid；
- 已注册的 MCP pid 及其子树；
- Console 自身（`agentsentinel-console.exe`）及 Vite/cargo 子树；
- `cmdline` 包含 `tools/orchestrator/service.py` 或 `tools/orchestrator/mcp_server.py` 的基础设施进程。

> 这是“基础设施排除”，不是“按进程名发现”。发现仍完全基于 cwd。

### 6.4 性能与缓存

- 全量扫描预计 100–500ms；
- 只由手动 `/discover` 触发，不进入 30s 缓存整理循环；
- driver 内缓存结果 2s，避免连续点击重复扫描；
- 单个进程 `AccessDenied`/`NoSuchProcess`/`ZombieProcess` 静默跳过。

## 7. 数据模型与持久化

`AgentRecord` / `agents` 表新增字段（SQLite migration，沿用现有 `_init` 模式）：

| 字段 | 说明 |
|---|---|
| `driver` | `claude` / `wild` / `codex`，默认空 |
| `pid_create_time` | 进程创建时间，防 PID 复用 |
| `cwd` | 绑定时的 cwd，仅展示/校验 |

绑定记录写入现有 registry，因此：

- daemon 重启后仍保留；
- 由 `process_alive(pid)` + `pid_create_time` 探活；
- 进程退出后标记 `stopped`，不自动删除。

## 8. 手动绑定

### 8.1 `POST /agents/bind`

请求：

```json
{
  "pid": 12345,
  "create_time": 1699999999.12,
  "role": "coder"
}
```

校验：

1. pid 存活；
2. `create_time` 与当前进程一致（防 PID 复用）；
3. `cwd` 位于 daemon 项目根内；
4. role 满足 `^[a-z0-9_-]+$` 且未被占用；
5. pid 不是已注册的 MCP/daemon 进程。

成功返回：

```json
{
  "agent": {
    "role": "coder",
    "pid": 12345,
    "driver": "wild",
    "status": "registered",
    "project": "d:\\code\\...\\agentsentinel",
    "cwd": "d:\\code\\...\\agentsentinel",
    "started_at": "2026-11-10T...Z"
  }
}
```

失败：400（参数/role）、404（pid 不存在）、409（role/pid 已被占用或项目不匹配）。

### 8.2 `POST /agents/unbind`

```json
{ "role": "coder" }
```

- 仅允许解除 `driver=wild` 的绑定；
- 标记 `stopped` 并从野生列表移除；
- 不终止被绑定进程。

## 9. `/discover` 契约扩展

保持向后兼容，仅新增字段：

```json
{
  "discovered": true,
  "recovered": 2,
  "pruned": 1,
  "agents": [ ... ],
  "wild": [
    {
      "pid": 12345,
      "ppid": 12000,
      "cwd": "D:\\Code\\Work_Code\\projects\\AgentSentinel",
      "cmdline": "python my_agent.py --mode run",
      "create_time": 1699999999.12,
      "root": true,
      "child_pids": [12346, 12347],
      "source": "cwd",
      "bound_role": null
    }
  ]
}
```

- `agents`：已注册/已绑定，行为与现在一致；
- `wild`：新的未注册候选，只读展示；
- 已绑定的野生进程从 `wild` 移到 `agents`，带 `driver: "wild"`；
- 旧前端忽略 `wild` 不会崩溃。

## 10. 前端交互

现有「检测 Agent」按钮保留，交互扩展为：

1. 点击后调用 `/discover`；
2. Console 继续显示「缓存恢复 N / 清理 M」，新增「发现 X 个未注册进程」；
3. Agents 面板下方新增可折叠区域「未注册进程」：
   - 列：pid / cwd / cmdline 摘要 / 子进程数 / 操作；
   - 每行：角色输入 + 「绑定」按钮；
   - 绑定成功后刷新 Agents 表，Console 记录 `绑定 pid=12345 → coder`；
4. 已绑定的野生 Agent 在 Agents 表显示 `driver=wild` 或「野生」标签；
5. 进程已退出时显示「已停止」，提供「解除绑定」。

## 11. 安全边界

严格按照用户要求：

- **只读**：`pid / ppid / cwd / cmdline / create_time`；
- 不读取环境变量、打开文件、内存内容、网络连接、注册表；
- 不注入、不修改、不终止被扫描进程；
- `AccessDenied` / `NoSuchProcess` / `ZombieProcess` 静默跳过；
- `cmdline` 截断（建议 300 字符），对 `--token=...`、`API_KEY=...`、`SECRET=...` 做掩码后再展示/持久化；
- 扫描结果只存内存；绑定后的元数据存项目内 SQLite（`.claude/orchestrator/`，gitignored）；
- 跨项目进程不会出现在当前项目的野生列表（cwd 过滤 + daemon project 校验）。

## 12. 风险与开放问题

| # | 风险 / 问题 | 建议 |
|---|---|---|
| 1 | 全系统扫描性能 | 手动触发 + 2s 缓存 |
| 2 | 其他用户/受保护进程无法读取 cwd | 静默跳过 |
| 3 | PID 复用 | 绑定必须带 `create_time` |
| 4 | cmdline 可能含密钥 | 截断 + 掩码 |
| 5 | 进程树粒度：只显示 root 还是展开 | 默认 root + 子进程数，后续可展开 |
| 6 | 角色清单固定还是自由 | 建议自由输入 + 正则校验，UI 给 coder/tester/manager 快捷项 |
| 7 | 野生绑定进程退出后保留还是删除 | 保留为 stopped，允许 unbind |
| 8 | Console 自身也被扫描到 | 基础设施排除规则 |
| 9 | 大量子进程导致 `wild` 列表噪声 | 折叠到 root；只展示 root |

## 13. 分阶段落地

### P1：只读嗅探（需要 psutil 批准）

- 新增 `CwdProcessDriver`；
- `/discover` 返回 `wild`；
- 前端展示未注册进程列表（不绑定）；
- 测试：用注入的假进程表测试 cwd 过滤/进程树/权限异常，daemon 集成测试返回 `wild`。

验收：

- 项目内启动一个 `python -c "import time; time.sleep(60)"`（cwd=项目根），`/discover` 能列出；
- 项目外进程不出现；
- daemon/MCP/Console 子树不出现；
- `pytest` / `ruff` / `mypy` exit 0。

### P2：手动绑定

- `driver/pid_create_time/cwd` 字段与 migration；
- `/agents/bind`、`/agents/unbind`；
- 前端绑定/解除 UI + Console 记录；
- 测试：绑定成功、PID 复用拒绝、role 冲突、项目不匹配、进程退出后 stopped。

### P3：ClaudeDriver 抽象与 CodexDriver

- 把现有缓存恢复/MCP 发现迁入 `ClaudeDriver`；
- 保留 MCP 心跳与缓存写入；
- 为 `CodexDriver` 预留接口与文档。

## 14. 代码落点（预估）

| 文件 | 变更 |
|---|---|
| `tools/orchestrator/drivers/base.py` | `AgentCandidate`、`AgentDriver` Protocol |
| `tools/orchestrator/drivers/claude.py` | `ClaudeDriver`（包装现有缓存/registry） |
| `tools/orchestrator/drivers/cwd_process.py` | `CwdProcessDriver`（psutil） |
| `tools/orchestrator/drivers/__init__.py` | `DriverRegistry` |
| `tools/orchestrator/service.py` | `/discover` 聚合、`/agents/bind`、`/agents/unbind`、`Daemon.bind_candidate` |
| `tools/orchestrator/registry.py` | `driver/pid_create_time/cwd` 列与 migration |
| `experiments/frontend-dashboard-tauri/src/lib/backend.ts` | `DiscoverResult.wild`、`bindAgent`、`unbindAgent` |
| `experiments/frontend-dashboard-tauri/src/App.tsx` | 野生列表/绑定 UI |
| `tests/orchestrator/test_discover_wild.py` | 扫描算法与 API 测试 |

## 15. 待用户确认

1. 是否批准 `psutil` 依赖；
2. 角色清单：自由输入还是固定 coder/tester/manager；
3. 野生进程列表：只显示进程树 root，还是展开全部子进程；
4. 绑定进程退出后：保留 stopped 还是自动删除；
5. 是否将 `cmdline` 只存截断/掩码后的版本（建议是）。

确认后再进入 P1，不先写实现代码。
