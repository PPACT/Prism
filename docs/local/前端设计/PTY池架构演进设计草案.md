# PTY 池架构演进设计草案

> 状态：**已拍板（2026-11-14），P0 双终端 Spike 已派发**（`T-20261114154618-ab3bea`）；P1 需 P0 验证通过后开始。
> 基线：`654e472`（P0 切换丢上下文已修复；dev 已确认以此为准）。`STATUS` 快照滞后，本文以 `654e472` 源码为准。
> **裁决更新（2026-11-14）**：用户正式推翻此前「战略收缩：放弃 PTY 池」裁决，重启 PTY 池演进；上限 6、TTL 60min、任务红线、Shell↔Agent 转换见 §3.2/§3.3。

## 1. 目标与非目标

**目标**
- 把当前单终端 MVP 演进为**每 Agent 常驻独立 PTY/claude 进程**的终端池。
- 切换 Agent 只切视图，不杀进程、不重启，上下文与会话保持。
- 为多 Agent 并行协作（如 dev / doc / reviewer 同时工作）铺路，同时保留单终端模式的兼容路径。

**非目标（v1）**
- 不改 registry 的 `role TEXT PRIMARY KEY` 身份模型；同一 role 仍只允许一个 Agent。
- 不做跨 Workspace 的终端迁移；池属于**单个 workspace daemon**。
- 不做终端录制/回放、远程终端、终端共享。
- 不改变产品侧 `agentsentinel/` 治理与 MCP 任务协议。

## 2. 现状基线（当前源码）

| 现状 | 证据 |
|---|---|
| `TerminalManager` 是代理层，内部 `dict[id, PtyManager]`，v1 仅 `id="1"` | `tools/orchestrator/service.py` `TerminalManager`（`DEFAULT_TERMINAL_ID = "1"`） |
| `/pty/<terminal_id>/<action>` 已存在，未知 id 404；`/pty/<action>` 仍兼容默认终端 | `service.py` `_normalize_terminal_path` |
| 角色 session 已持久化到 `pty_sessions.json`（role → session） | `service.py` `TerminalManager._load_sessions/_save_sessions_locked`、初始化 `sessions_path=self.home / "pty_sessions.json"` |
| 切回角色用 `--resume`，仅当 JSONL 真实存在；否则换新 id | `service.py` `_new_pty(resume=...)`、`pty_for`；`engines.py` `build_resume_command` |
| 切角色仍会 `current.stop()`，单活动 PTY | `service.py` `pty_for` |
| PTY 状态文件仍是单份 `pty.json` | `service.py` `_new_pty(state_path=self.home / "pty.json")`；`pty_manager.py` `_write_state_locked` |
| `clean_all` 只读每个 home 的 `pty.json` | `scripts/clean_all.py` `read_state_at(home, "pty.json")` |
| Hook 通知只路由到 `terminals.active()`，并用 active session 校验 | `service.py` `notify_claude_hook`、`/hooks/claude/notification` |
| 前端只有一个 `ACTIVE_TERMINAL_ID = "1"` | `src/lib/backend.ts` |
| 前端选中 role 驱动 `usePtyChat(role, agentId)` 与单个 `TerminalPanel` | `src/App.tsx`、`src/lib/usePtyChat.ts`、`src/components/TerminalPanel.tsx` |
| 工作区切换 `activate_workspace` → `reset_pty`，当前会杀 PTY | `service.py` `activate_workspace`/`reset_pty` |
| `registry.agents` 以 role 为主键 | `tools/orchestrator/registry.py` |

**结论**：API 形态已为池留了口子，但运行时仍是“单活动 PTY + 切走即停”；池化需要改生命周期、状态持久化、SSE/chat/hook 路由、前端选中模型与清理链路。

## 3. 核心设计

### 3.1 终端身份与数据模型

`terminal_id` 用**不可变不透明 ID**（如 `t-<8位随机>`），不用数组下标；兼容旧行为：`"1"` 作为默认终端别名保留一个发布周期。

建议 `TerminalRecord`：

| 字段 | 说明 |
|---|---|
| `terminal_id` | 主键，创建后不变 |
| `workspace_id` | 所属 Workspace（池不跨 workspace） |
| `kind` | `agent` / `shell`（v1 可只做 `agent`） |
| `role` / `agent_id` | Agent 终端的身份；shell 为空 |
| `engine` / `command` | 引擎与最终启动命令（含 per-role command 解析结果） |
| `session_id` | Claude/Codex 会话；`--resume` 依据 |
| `cwd` | Workspace 项目路径 |
| `status` | `starting` / `running` / `idle` / `exited` / `failed` / `restoring` |
| `pid` / `job_id` | 进程与 Job Object |
| `created_at` / `last_active_at` | LRU/TTL |
| `notice_seq` | 每个终端独立 notice 序号 |
| `restored` | daemon 重启后是否已懒恢复 |

持久化建议：
- `home/terminals.json`：池元数据（不含密钥），原子写。
- `home/pty-<terminal_id>.json`：每终端 pid/job/session，供 `clean_all` 与兜底脚本；替换单 `pty.json`。
- `home/pty_sessions.json`：role → session 兼容保留；新增 `terminal_id → session` 映射，迁移时一次性写入。

### 3.2 生命周期与上限

状态机：`created → starting → running ↔ idle → exited/failed → stopped`。

- **创建**：按需创建（选中 Agent / 显式新建），不预启动全部。
- **软上限**：默认 **6**（5 个核心 Agent + 1 个容错/Shell 位）；允许在「设置 / 工作区」调高（建议范围 1–12，如 8 / 10 / 12）。6 是默认总数，Shell 与 Agent 都占位；超过软上限返回 429/409。
- **物理硬兜底**：daemon 资源监控，系统内存占用 **>80%**（可配）时强制 LRU 回收未 pin 的空闲终端；仍超阈值则拒绝新建（429/503），不依赖前端自觉。
- **LRU + 空闲 TTL**：默认 **60 分钟**；仅回收 `idle`、非当前活动、无未完成任务且未 pin 的终端。Shell 空闲可回收（关闭即销毁）；**Agent 终端默认 pin，不参与自动 TTL**。
- **绝对红线**：Agent 有未完成长任务（构建/测试等）绝对禁止被 LRU/TTL 回收。实现以 `pinned`/`busy` 标记为准；本地 Agent 没有 registry 任务记录时默认视为 pinned，只有用户显式解除或确认后才可回收。
- **任务保护缺口（P0/P1 必须补）**：当前 registry 只跟踪 MCP worker 任务，本地配置 Agent 没有任务记录；需补 `busy`/`pinned` 标记，否则“未完成任务不回收”无法可靠保证。
- **退出**：`exit` 后保留记录（可查看最后输出/状态），不自动重启；显式重启才 `--resume`。
- **会话冲突重试**：沿用现有 `session_conflict_detected` + `MAX_SESSION_RETRIES`，但重试产生的**新 session 必须写回该 terminal 的 record**，避免上下文丢失（P0 回归点）。
- **停止/删除**：先停 Job，再删元数据与 `pty-<id>.json`；运行中删除需二次确认。

### 3.3 role ↔ terminal ↔ session 绑定

- v1：`role` 唯一对应一个 `terminal_id`（同 role 多实例不做）；`agent_id` 变化视为换人，旧 terminal 标记 `exited`。
- `terminal_id` 唯一对应一个 `session_id`；session 可随 `/clear` 变化，由 terminal record 更新。
- `pty_sessions.json` 从 role→session 升级为 terminal→session；role 映射仅作兼容查询。
- Chat / handoff / Hook 通知都必须带 `terminal_id`，不再默认 `terminals.active()`。
- 外部 MCP Agent：一个 `role` 只绑定一个终端；本地配置角色与外部 Agent 同名时仍按现有“外部存活优先”规则。
- **Shell 终端**：`kind=shell`（如 PowerShell），不注册 Agent、不消耗 Token、不进入任务路由；关闭即销毁（DELETE 直接 stop + 删记录）。
- **Shell → Agent 转换**：用户通过 UI/API 将某个 Shell 指派为 Agent（分配 role、启动命令/唤醒词）：
  - 转换 = **停止 Shell 进程 → 以同一 terminal_id 启动该角色的 CLI**（不是把 PowerShell 原地变成 claude）；cwd 用 Workspace 路径，Shell 历史/前台进程不保留。
  - 转换前校验：role 未被占用；Shell 若疑似有前台长任务，要求用户确认。
  - **最终口径 A（用户 2026-11-14 拍板）**：先预检命令/环境；预检通过后停 Shell，原地以同一 `terminal_id` 启动 CLI；若 spawn 仍失败，标记 `failed`，由用户重新点 `+` 创建——**不自动重开 Shell，不引入临时第 7 进程**。
- **Agent → Shell 不提供**（v1）；删除 Agent 终端需任务完结确认。

### 3.4 API 复数化

保留旧接口一个发布周期，新增：

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/pty/terminals` | 池列表：id/role/agent/status/pid/session/created/last_active |
| POST | `/pty/terminals` | 创建终端（role/agent_id/kind/command 可选）；支持 `Idempotency-Key` |
| POST | `/pty/<id>/activate` | 设为活动终端（仅前端视图切换，不杀其他） |
| POST | `/pty/<id>/start` | 启动/重启（`resume` 由服务端判定） |
| POST | `/pty/<id>/stop` | 停止该终端 |
| DELETE | `/pty/<id>` | 删除终端与状态文件 |
| GET | `/pty/<id>/status` | 单终端状态 |
| GET | `/pty/<id>/stream` | 原始输出 SSE |
| GET | `/pty/<id>/chat/stream` | 该终端 JSONL 旁路 SSE |
| POST | `/pty/<id>/input` / `resize` | 输入/尺寸，必须校验 role/agent 绑定 |

错误码：`404` 未知 id；`409` role 已绑定/状态冲突；`423` 终端锁定；`429` 达到上限；`503` 引擎启动失败。旧 `/pty/*` 映射到默认终端 `"1"`。

### 3.5 SSE 与 JSONL 隔离

- 每个终端独立订阅队列与 backlog（沿用 512KB/终端），`seq` 按终端独立；禁止跨终端广播。
- `/pty/<id>/stream` 只回放该终端 backlog；重连可选支持 `Last-Event-ID`。
- `/pty/<id>/chat/stream` 的 offset、session、context/usage 都按终端保存；切换视图不重建流（前端保持订阅或按需重连）。
- Hook notice 按 `session_id → terminal_id` 路由，只写入该终端 `notice_seq`；不再写入 active 终端。
- `/events` 总览只增加终端摘要，不承载原始输出。

### 3.6 前端多终端视图

- 左侧 Agent 列表选择 = 选中 `terminal_id`；终端 Tab 显示该终端；对话/进度页绑定同一 terminal。
- 非活动终端**必须严格卸载视图并销毁 DOM + dispose WebGL**（不是仅隐藏）；仅活动终端加载 WebGL 渲染。
- 非活动终端不保留渲染上下文与后台绘制；切回时重新挂载并回放截断 backlog，UI 提示“只恢复最近输出”。
- 活动终端 24ms 合批；非活动终端不渲染，避免多开 CPU/GPU 飙升。状态按 `terminal_id` 存储；切换 Agent 不再调用 `stopPty`。

### 3.7 daemon 重启与懒恢复

- 启动时只加载 `terminals.json`，将记录标为 `restored=false`，**不批量拉起进程**。
- 用户选中终端时懒恢复：JSONL 存在 → `--resume <session>`；不存在 → 新 session，并在 UI 提示“原会话不可恢复，已新建”。
- ConPTY 进程无法重连，只能重启进程 + 续接 session；不要承诺“原进程恢复”。
- `pty_sessions.json` / `terminals.json` 损坏时降级为空池，不阻塞 daemon 启动。
- **TUI 重连边界（用户已接受）**：只要 `--resume` 能保住 JSONL 历史即可；重连只回放截断 backlog，UI 提示“只恢复最近输出”，不承诺完整屏幕。

### 3.8 Job / 内存 / 孤儿清理

- 每 PTY 一个 Job Object；daemon 退出/workspace 删除时 `stop_all`。
- `clean_all` 从“每 home 一个 `pty.json`”改为遍历 `pty-*.json` + `terminals.json`；仍只匹配项目路径/daemon+pty pid/白名单，禁止盲杀。
- 内存：每终端 backlog 512KB + xterm scrollback 5000 行；4–6 终端预计可控，需实机压测。
- 空闲 TTL 回收必须走正常 `stop`，不可直接杀 pid；删除前写审计日志（terminal_id/role/reason）。
- **工作区切换 = 挂起保留（用户拍板）**：切走不 stop、不 reset；切回继续原 PTY 与上下文。**保留的是进程本身，不是只留记录 + 懒 resume**。只有删除 Workspace、daemon 退出或软件退出才 `stop_all`。当前 `activate_workspace` 的 `reset_pty` 必须移除/改造。

### 3.9 权限与 Caller Identity

- 每个终端记录 `role` / `agent_id`；`/pty/<id>/*` 必须校验请求的 role/agent 与终端绑定一致，不能只信客户端传的 id。
- 本地配置角色不是托管 worker（`job_id=""`）；不得自动接任务/预算/路由。
- 外部 MCP Agent 的 `agent_id` 仍由注册期决定；同 role 多实例不在 v1 范围。
- git push / reset 等身份校验继续走现有 Caller Identity；终端池不新增绕过路径。
- Hook 回调仍用 daemon Bearer token；`session_id → terminal_id` 映射失败时丢弃并记 warning，不广播到所有终端。

### 3.10 分阶段落地

| 阶段 | 内容 | 出口条件 |
|---|---|---|
| P0 | 双终端 Spike：纯 Shell + 可转换 Agent；软上限/内存兜底/转换 A/pin/非活动销毁（feature flag） | Spike 证明 2 PTY 隔离、转换 A、资源阈值生效、非活动 DOM/WebGL 释放 |
| P1 | 池数据模型、持久化、API、生命周期/上限/TTL；旧 `/pty/*` 映射默认终端 | 后端测试 + 兼容旧前端 |
| P2 | 前端终端 tabs/选中模型、chat/handoff 按 terminal、WebGL 策略 | 真机 E2E：A→B→A 不丢上下文、无重复流 |
| P3 | 懒恢复、clean_all、孤儿回收、LRU、压测与灰度 | 崩溃/重启/清理验收矩阵全绿 |

**P0 Spike 范围（当前阶段，用户已拍板，立即执行）**
- 一个纯 Shell（PowerShell）+ 一个由 Shell 转换/新建的 Agent（如 reviewer）；feature flag，不改现有单终端默认行为。
- 容量：软上限默认 6（Spike 用 2 验证）；「设置/工作区」可调；内存 >80% 阈值可注入测试，超限先 LRU 再拒绝新建。
- 转换 A：预检 → 停 Shell → 同 `terminal_id` 起 CLI；失败标 `failed`，不重开 Shell、不引入临时第 7 进程。
- 保护：pin/忙标记免 TTL/LRU；工作区切换保留**进程**；Shell 关闭即销毁；Agent 关闭需确认。
- 前端：最小双视图验证非活动终端卸载 DOM + dispose WebGL；仅活动 WebGL；切回回放 + 提示。
- 不做：P1 数据模型重构、完整多终端 UI、跨 Workspace 的 UI 全量验收（但后端“保留进程”必须测）。

**验收矩阵（至少）**
- 创建/切换/停止/删除；软上限默认 6、可调、内存 >80% 兜底（注入测试）。
- Shell→Agent 转换 A：预检、同槽起 CLI、失败标 failed；转换后注册/计 Token。
- pin/忙标记：长任务不被 TTL/LRU；Shell 关闭即销毁。
- A→B→A 后 session/context 保持；`--resume` 命中；工作区切换保留进程。
- SSE 不串流；Hook 只通知对应终端；chat offset 独立。
- 前端非活动终端卸载 DOM + dispose WebGL；仅活动 WebGL；切回回放 + 提示。
- daemon 重启懒恢复；`clean_all` 清理全部终端且不误杀；Job 无残留。
- 权限：跨 role 调用被拒；本地角色无 worker 权限。

### 3.11 回滚方案

- Feature flag `PRISM_TERMINAL_POOL`（旧名 `SENTINEL_TERMINAL_POOL` 读取兜底，默认 0）；关闭时 `TerminalManager` 退回单终端 `id="1"`，旧 `/pty/*` 行为不变。
- `terminals.json` / `pty-*.json` 为新增文件；回滚时忽略，`pty.json` 继续由默认终端写入。
- 不删除旧文件、不做破坏性 schema 迁移；前端可通过 flag 隐藏终端 tabs。
- 回滚验收：单终端 A→B→A 与现有 P0 修复行为一致。

## 4. 与现有代码的差异点

1. 单 `pty.json` → 每终端状态文件 + `terminals.json`；`clean_all` 必须同步。
2. `TerminalManager` 从“一个活动 id”变成真正的池（当前 `install` 覆盖 active id）。
3. `pty_for` 的“先 stop 再起”改为“查找/创建目标终端，不碰其他终端”。
4. Hook notice 从 active 终端改为按 session 路由。
5. chat/stream 从 role 参数改为 terminal_id 参数；前端 `ACTIVE_TERMINAL_ID` 常量移除。
6. `activate_workspace` 的 `reset_pty` 语义需定稿：池内终端是否跨 Workspace 视图保留。
7. 前端从单 `activeRole` 派生，改为 `activeTerminalId` + 每终端状态。

## 5. 风险与开放问题

**风险**
- 多 ConPTY 进程的内存/句柄/Job 数增长；空闲 TTL 与 Agent 长任务冲突。
- WebGL 上下文数量与丢失；非活动终端 CPU 解析成本。
- TUI 重连只能回放截断 backlog，无法保证屏幕完整。
- `--resume` 依赖 JSONL 存在与 session 未被外部占用；冲突重试可能再次换 session。
- registry role 唯一：同 role 多终端需 agent_id 主键改造，不能顺手做。
- 工作区切换/daemon 退出时的孤儿与误杀风险。

**已拍板（2026-11-14，用户）**
1. 重启 PTY 池：**是**；正式推翻此前“战略收缩”。
2. **软上限默认 6，可调 8/10/12**；空闲 TTL **60 分钟**；LRU 保留；**未完成长任务绝对禁止回收**；内存 >80% 物理硬兜底。
3. Shell 与 Agent 混用：初始为纯 Shell（不注册、不计 Token、关闭即销毁）；支持 UI/配置**转换为 Agent**（分配 role、拉起 CLI），转换后注册入网并受池保护；转换采用 **A 事务式**。
4. 工作区切换：**挂起保留进程**；只在删除 Workspace 或退出软件时全部销毁。
5. 非活动终端：**严格销毁 DOM + WebGL 上下文**，仅活动终端 WebGL。
6. TUI 重连边界：接受；`--resume` 保住 JSONL 即可，UI 提示只回放最近输出。

**最终裁决（2026-11-14，用户；取代上节待确认项）**
1. **容量**：软上限默认 6，可在「设置/工作区」调高（8/10/12）；daemon 资源监控在系统内存 >80% 时强制 LRU 或拒绝新建。
2. **Shell→Agent**：采用 **A 事务式**；预检通过后停 Shell、原地起 CLI；极端失败不重开 Shell，用户重新点 `+`；不引入临时第 7 进程。
3. **生命周期**：长任务发 pin，免 TTL/LRU；工作区切换**保留进程**，不是只留记录。
4. **渲染**：非活动终端严格销毁 DOM + WebGL 上下文，仅活动终端 WebGL。

## 6. 结论

技术上可行，且现有 `TerminalManager` + `/pty/<id>` + `pty_sessions.json` 已提供演进支点；用户已拍板重启池化。执行前确认项见 §5；P0 双终端 Spike 通过后再进入 P1，当前保持单终端与 `654e472` 不变。
