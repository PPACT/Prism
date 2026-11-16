# PTY 终端与对话双模式方案（单 PTY + JSONL 旁路）

> 状态：**草案，未实施**（2026-11-12，按用户重新裁决改写）。
> 范围：前端中控台的「原始终端」与「对话气泡」双视图；后端为 `tools/orchestrator/`（开发期工具），不接产品、不改 `agentsentinel/**`。
> **裁决**：采用「**单 PTY 常驻 + 旁路读取本地 JSONL**」。此前「双引擎 + `--fork-session`/`--resume` 显式交接」方案**作废**；对话模式不再调用 `claude -p` 无头进程。
> 依据：源码 `tools/orchestrator/{transcript.py,chat.py,service.py}`（行号按 2026-11-12 工作区）；`27363f2` 的零 token transcript 读取由 dev 转达，未在本文重复核验提交对象。

## 1. 结论

1. **底层只有一个真实、交互式 PTY 进程**（Windows ConPTY，`pywinpty`），常驻运行；终端模式与对话模式共享同一个进程和同一个 stdin。
2. **终端模式**：PTY 原始 ANSI 字节经 SSE 推给前端 xterm.js，完整还原黑框（颜色、光标、全屏重绘）。
3. **对话模式**：**绝不解析 PTY 的 ANSI**；旁路读取 Claude Code 本地 `.jsonl` transcript，按文件增量（offset/size）把新的 `user/assistant` 消息推给前端渲染气泡。
4. **切换模式不重启进程**：两个 Tab 只是同一 PTY 的两种视图；输入统一写入 PTY stdin。
5. **零 token 读取**：JSONL 读取不调用模型、不计费；计费完全由原生 CLI 决定，不增加二次确认弹窗。
6. **传输**：`POST /pty/input` + `GET /pty/stream`（SSE、base64 字节），零新 Python 依赖；WebSocket 列为后续可选。

## 2. 背景与作废声明

此前方案试图用「终端 PTY」与「`claude -p` stream-json 对话引擎」双引擎并行，并通过 `--resume` 在同一 session 上显式交接。用户重新裁决后作废，原因与结论：

- 双引擎需要「停一个、起另一个」的交接窗口，且 `--resume` 的行为依赖 CLI 版本，复杂度高。
- 交互式 TUI 的 ANSI 输出无法稳定解析为结构化消息；但 Claude Code 已把结构化会话写入本地 JSONL。
- 因此保留**一个**交互式 PTY 作为唯一执行体，另用 JSONL 作为**只读、零 token** 的对话视图数据源。

**明确作废清单**：

- 双引擎（PTY 终端 + `claude -p` 对话）并行的架构；
- `--fork-session` / `--resume` 的显式模式交接；
- 对话模式解析 PTY ANSI 或从 PTY stdout 提取结构化事件；
- 为对话模式单独启动 `claude -p --output-format stream-json` 进程（P3 起由 JSONL 气泡替代）。

**保留资产**：

- `27363f2`：transcript 只读查看（`GET /sessions/history`）、零 token 读取；
- `tools/orchestrator/transcript.py`：JSONL 路径解析与消息抽取；本文的增量读取在其上扩展；
- 现有 Bearer、SSE、Job Object 回收、前端 `plugin-http` SSE 客户端。

## 3. 目标与非目标

### 目标

- 终端 Tab：可交互的原生 TUI（Phase 1 PowerShell/cmd；Phase 2 claude），完整 ANSI 还原；
- 对话 Tab：同一会话的消息气泡（user/assistant/text/thinking/tool_use），随 JSONL 增量更新；
- 输入统一：两个 Tab 的输入都写入同一 PTY stdin；
- 单 PTY 生命周期：启动、resize、退出、崩溃回收、断线重连；
- 安全：仅 `127.0.0.1` + Bearer，输入限流，cwd 限项目根。

### 非目标

- 不解析 ANSI TUI 为结构化消息；
- 不 attach 用户在外部手动打开的终端（Windows 控制台限制；只能只读 transcript）；
- 不做多 PTY 并发/多 role 同时终端（首期单 PTY、单 owner）；
- 不做远端/多机终端；
- 不接产品运行时。

> 与既有文档的关系：《AgentSentinel 工作台定位与前端中间层规划》提出「拒绝纯字符终端、前端不引入 xterm.js」，该结论继续适用于**对话面板**；本方案的**终端 Tab** 使用 xterm.js，对话 Tab 仍用 Markdown/块级渲染。两者按双 Tab 并存。

## 4. 架构总览

```text
                   ┌──────────────────────────────────────────┐
                   │ 前端（Tauri + React）                     │
                   │  终端 Tab：xterm.js ← SSE(base64)         │
                   │  对话 Tab：气泡渲染 ← SSE(JSON 消息)      │
                   │  输入：两个 Tab → POST /pty/input         │
                   └───────────────┬──────────────────────────┘
                                   │ 127.0.0.1 + Bearer
                   ┌───────────────▼──────────────────────────┐
                   │ daemon（stdlib HTTP）                     │
                   │  PtyManager：单 PTY（ConPTY/pywinpty）    │
                   │    ├─ 读线程：字节队列 → /pty/stream SSE  │
                   │    └─ 写入口：/pty/input → stdin          │
                   │  TranscriptTailer：offset 增量读 JSONL    │
                   │    └─ /sessions/stream SSE（气泡）        │
                   │  Job Object：daemon 退出回收进程树        │
                   └───────────────┬──────────────────────────┘
                                   │ ConPTY
                   ┌───────────────▼──────────────────────────┐
                   │ 交互式进程：PowerShell（P1）/ claude（P2）│
                   │  stdout/stderr → PTY → daemon 字节流      │
                   │  会话写入 Claude Code 本地 JSONL          │
                   └──────────────────────────────────────────┘
```

要点：

- **单一执行体**：只有一个 PTY 子进程；对话模式不是第二个引擎。
- **单一输入通道**：所有按键/文本都走 `/pty/input` 写入 PTY stdin；对话 Tab 的「发送」等价于向 TUI 键入文本 + 回车。
- **旁路只读**：JSONL 由 CLI 写入，daemon 只读；读取失败不影响终端。
- **零 token**：JSONL 读取不触发模型调用，不产生费用。

## 5. 两种视图与数据流

### 5.1 终端模式

1. `GET /pty/stream` 建立 SSE；daemon 把 PTY 读线程的字节块 base64 后推送。
2. 前端 xterm.js 直接 `write(Uint8Array)`；ANSI/颜色/光标由 xterm 解释。
3. 键盘输入（含粘贴）→ `POST /pty/input`；daemon 写入 PTY stdin。
4. 容器尺寸变化 → `FitAddon.fit()` → `POST /pty/resize` → ConPTY resize。

### 5.2 对话模式

1. 初始回填：`GET /sessions/history?role=&limit=` 读取最近消息（现有能力）。
2. 增量：`GET /sessions/stream?role=&cursor=` SSE；daemon 的 TranscriptTailer 周期检查 JSONL（建议 500ms–1s），从已知 offset 读取新增完整行，解析为消息事件推送。
3. 发送：对话 Tab 的输入框 → `POST /pty/input`（文本 + `\r`）写入同一 PTY；回复由 JSONL 增量回流为气泡。
4. **不展示 TUI 画面**：权限确认、选择菜单等交互提示不会出现在气泡里；UI 需提示「需要交互时切到终端 Tab」。

### 5.3 模式切换

- 切换只是前端视图切换，**不停止、不重启 PTY**，不调用 `--resume`。
- 两个 Tab 共享同一 session；JSONL 游标持续维护，切回对话 Tab 不重复回放已消费消息。
- 同一时刻只允许一个前端写入者；额外客户端只读跟随（见 §8）。

## 6. JSONL 增量读取设计（P3 核心）

### 6.1 数据源与安全边界

- 路径规则沿用 `transcript.py`：`<CLAUDE_CONFIG_DIR>/projects/<项目slug>/<session_id>.jsonl`；
- `session_id` 必须是 Daemon 启动 PTY 时显式传入的 UUID（36 位），不从用户输入拼接路径；
- 仅读取该文件；不回显绝对路径、不写回、不读取其它项目/文件；
- 若 `CLAUDE_CONFIG_DIR` 不在项目内，这是该功能的既有边界（`transcript.py:1-5` 已声明）；保持只读、UUID 校验，不扩大范围。

### 6.2 会话 id

- P2 启动 claude TUI 时由 daemon 生成 UUID 并传 `--session-id <uuid>`；daemon 记录 `role → session_id → transcript_path`。
- **待 spike 验证**：交互式 claude 是否接受 `--session-id`；若不接受，则改为从 CLI 输出/配置解析，或要求用户在终端首轮登录后由 daemon 读取最新 session 文件（后者需额外校验，暂不推荐）。
- 用户在 PTY 内再手动启动的嵌套 `claude` 不纳入跟踪（无稳定映射）。

### 6.3 增量算法

```text
state: offset = 已消费字节数; pending = 未完成行缓冲

每轮（500ms–1s）：
  size = stat(path).st_size
  if size < offset:                 # 文件被截断/轮转
      offset = 0; pending = b""
  if size > offset:
      chunk = read(path, offset, size)
      data = pending + chunk
      lines = data.split(b"\n")
      pending = lines.pop()         # 最后一段可能不完整，留给下轮
      offset += len(chunk)
      for line in lines:
          parse JSON; 仅保留 type=user/assistant 且非 isSidechain
          取 message.role / message.content[]，转换为 blocks
          带 seq/uuid 去重后推送
```

- **只消费完整行**：避免把写入中的半行 JSON 当消息。
- **去重**：优先用 JSONL 的 `uuid`（若存在），否则用「文件内起始 offset」作为稳定键。
- **初始回填**：前端先调 `/sessions/history` 取尾部 N 条，再以当前文件大小为 cursor 开始增量；或由服务端在 SSE 首帧返回 `backfill`。
- **重启恢复**：daemon 重启后 cursor 归零，前端重新回填；不把 cursor 持久化到磁盘（首期）。
- **延迟**：CLI 写盘可能有毫秒到秒级滞后；UI 显示「同步中/落后 N 行」的状态，不把滞后当错误。
- **现有能力差异**：`read_history()` 当前是整文件读取（上限 8MB，`transcript.py:99-164`）；增量读取是**新增**，不改变文件格式与安全边界。

### 6.4 消息映射

| JSONL 内容 | UI |
|---|---|
| `message.role=user` + text | 用户气泡 |
| `message.role=assistant` + `text` blocks | Markdown 回复气泡 |
| `thinking` block | 折叠的思考面板 |
| `tool_use` block | 工具状态条（名称 + 摘要） |
| `tool_result` | 首期省略（与现有 `_blocks` 一致） |
| `usage` | 可展示 token/费用（只读，不拦截） |

## 7. 接口草案（未实现）

| 接口 | 方法 | 入参 / 返回 | 说明 |
|---|---|---|---|
| `/pty/start` | POST | `{role, cols?, rows?}` → `{session_id, pid}` | 首期单 PTY；已存在则 409 或返回现状 |
| `/pty/stream` | GET(SSE) | `?session_id=&since_seq=` → `pty`/`exit` | base64 字节；有界队列；重连回放见 §8 |
| `/pty/input` | POST | `{session_id, data(base64)}` | 单次 ≤ 64KB；限流 |
| `/pty/resize` | POST | `{session_id, cols, rows}` | 1–500 × 1–300 |
| `/pty/stop` | POST | `{session_id}` → `{stopped, code?}` | 终止并回收进程树 |
| `/pty/status` | GET | `?role=` → `{session_id, pid, alive, exit_code}` | UI 状态 |
| `/sessions/history` | GET | 现有；初始回填 | 零 token |
| `/sessions/stream` | GET(SSE) | `?role=&cursor=` → `message`/`usage`/`lag`/`rotate` | JSONL 增量（P3） |
| `/chat` | POST | 现有；**P3 起停用** | 不再作为对话主路径 |
| `/chat/stop` | POST | 现有；P3 起停用 | 同上 |

事件示例：

```text
event: pty
data: {"seq":41,"data":"G1swbT0="}

event: exit
data: {"code":0}

event: message
data: {"seq":12,"role":"assistant","uuid":"...","blocks":[{"kind":"text","text":"..."}]}

event: lag
data: {"pending_bytes":128,"behind":true}
```

传输选择：**SSE + POST 为本期方案**，复用现有 `ThreadingHTTPServer` 与 Tauri `plugin-http`，零新 Python 依赖。WebSocket 仅作为后续可选，需新增 `websockets` 依赖与 asyncio 桥接，本期不做。

## 8. 生命周期、owner 与安全

### 8.1 单 PTY 生命周期

- 首期只维护**一个活跃 PTY**（单 owner）；不做多 PTY 并发。
- Daemon 启动 PTY 后立即绑定 Windows Job Object；Daemon 正常退出、崩溃、被停止时关闭 Job，回收整棵进程树。
- 前端断开 SSE 不等于停止 PTY；PTY 常驻，供重连。显式 `POST /pty/stop` 或 TUI 自行退出才结束。
- PTY 退出时推送 `exit` 事件；前端显示退出码与「重新启动」按钮，不自动重启（避免循环崩溃）。

### 8.2 写者与订阅

- 同一时刻只允许一个**写者**（前端主窗口）；第二个客户端默认只读跟随，或返回 409（待定）。
- 输入限流：单请求 ≤64KB，会话级 ≤256KB/s；超限返回 429。
- 输出背压：每会话有界队列（建议 1–4MB）；队列满时暂停读取 ConPTY，**不得静默丢字节**（会破坏画面）。
- 重连回放：daemon 保留最近 N 字节环形缓冲（建议 256KB）+ 每块 `seq`；`since_seq` 命中则补发，否则提示前端「重绘」——可发送一次 resize 或 Ctrl+L 触发 TUI 重绘；仍错位时提供手动重启。
- resize：ConPTY 支持；失败时返回错误并在 UI 提示，不静默忽略。

### 8.3 安全边界

- 仅监听 `127.0.0.1`；除 `/health` 外全部 Bearer。
- PTY 等于**本机用户权限下的任意 shell**，不是沙箱；UI 与文档必须明示。
- 不监听外网、不做多用户、不提供命令白名单（无法可靠限制交互式 shell）。
- cwd 固定项目根；启动命令由 daemon 指定（P1 PowerShell，P2 claude），不接受前端任意命令。
- JSONL 读取严格限定 §6.1 的路径规则，只读。

## 9. 计费

- token/费用**完全由原生 CLI 决定**；daemon 不估算、不拦截、不弹二次确认。
- 对话气泡可展示 JSONL 中的 `usage`（若存在），仅展示。
- 不因 JSONL 轮询产生模型调用或费用。

## 10. 前端设计

- 两个 Tab：「终端」「对话」；默认终端。
- 终端 Tab：xterm.js + `@xterm/addon-fit`；动态 `import()` 控制首屏体积；复制粘贴走 `navigator.clipboard`。
- 对话 Tab：输入框 + 消息气泡；Markdown、thinking 折叠、tool 状态条；顶部提示「需要权限确认/菜单交互时切到终端」。
- 状态栏：PTY 连接状态、session 短 id、JSONL 同步状态（同步中 / 落后 N 字节 / 已暂停）。
- 切换 Tab 不改变 PTY；两个视图共享同一 session。
- 断线重连：先 `GET /pty/status`，再按 `since_seq` 续流；对话流按 cursor 续读。

## 11. 阶段与验收

### P1：shell PTY + xterm

- ConPTY 拉起 PowerShell；字节流回显、输入、resize、退出码；
- xterm 中可执行 `dir`，颜色与光标正常；
- Job Object 回收：daemon 退出后无残留；
- 验收：真实进程 E2E 记录输入/输出/退出码；单测覆盖会话生命周期（注入假 PTY）。

### P2：claude TUI 常驻

- PTY 内启动 `claude` 交互式，daemon 传 `--session-id`（待 spike 验证）；
- 终端模式可完成真实一轮对话；退出码正确；session id 可查；
- 验收：真实窗口 E2E；transcript 文件出现在 §6.1 路径；无残留进程。

### P3：对话模式改为 JSONL 气泡

- 新增 `/sessions/stream` 增量读取；初始回填 + 增量推送；半行/截断/轮转处理；
- 停用 `/chat` 无头对话主路径（是否保留兼容由 dev 实施决定）；
- 验收：同一 PTY 内输入 → JSONL → 气泡，消息不重复、不丢；解析零 token；
- 验收：daemon 重启后回填正确；文件轮转后可恢复。

### P4：双 Tab 状态与重连

- 两个 Tab 共享同一 PTY；模式切换不重启；写者/只读订阅；重连回放与重绘；
- 验收：终端输入 → 切对话看到气泡 → 对话框发送 → 终端可见 TUI；SSE 断开重连不重复/不错位（按 §8.2 的降级策略）。

### P5：加固与 E2E

- 进程树回收、鉴权、输入/输出限流、大小窗口 resize、崩溃恢复；
- 验收：`ruff check` / `ruff format` / `mypy` / `pytest` exit 0；前端 `npm test` 覆盖输入编码、重连、限流；真实 E2E 覆盖 daemon 崩溃后无残留、PTY 退出后可重启。

## 12. 风险与缓解

| # | 风险 | 影响 | 缓解 |
|---|---|---|---|
| 1 | TUI ANSI 不可可靠解析 | 对话错乱 | **不解析 ANSI**；对话走 JSONL 旁路 |
| 2 | JSONL 写入滞后/半行 | 气泡延迟或截断 | 只消费完整行；显示同步状态；不把滞后当错误 |
| 3 | JSONL 轮转/截断 | 游标失效 | `size < offset` 时重置并回填；不持久化 cursor |
| 4 | PTY 退出/崩溃 | 终端失联 | `exit` 事件 + 退出码 + 手动重启；Job 回收 |
| 5 | TUI 交互提示在对话 Tab 不可见 | 用户以为卡住 | UI 明示；提供「切到终端」入口 |
| 6 | xterm 体积与性能 | 首屏变慢 | 动态 import；仅终端 Tab 加载；fit 防抖 |
| 7 | PTY = 任意 shell | 本机命令执行 | 仅 127.0.0.1 + Bearer；cwd 固定；限流；明示非沙箱 |
| 8 | ConPTY resize/读取阻塞/退出码细节 | 卡死或尺寸不同步 | P1 spike 验证；读线程独立；失败可见 |
| 9 | 多客户端同时写 | 输入交错 | 单写者；第二客户端只读或 409（待定） |
| 10 | `--session-id` 在交互模式行为未验证 | 无法定位 JSONL | P2 spike 先验证；失败则按 §6.2 备选 |
| 11 | 断线后 ANSI 状态丢失 | 画面错位 | 环形缓冲按 seq 回放；必要时触发重绘/重启 |

## 13. 依赖申请

按 `AGENTS.md` §2，以下依赖需用户明确同意后才安装（本文仅记录，未安装）：

```
[依赖申请]：pywinpty，建议 >=2.0,<3
- 原因：Windows ConPTY 伪终端；仅 daemon 使用。
- 兼容性：独立于 AgentScope 2.0.8；仅 Windows 11 / Python 3.12 需要；本机 wheel 可用性需 P1 安装后验证。
- 安装位置：仅项目 .venv（uv pip install pywinpty）。

[依赖申请]：@xterm/xterm、@xterm/addon-fit（前端 npm）
- 原因：终端渲染与自适应尺寸；仅 experiments/frontend-dashboard-tauri。
- 兼容性：独立 UI 库，无 Python 依赖影响。
- 备注：用户已要求立即开工（2026-11-12 由 dev 转达）；安装前仍以用户在本会话的明确同意为准。

SSE + POST 方案不需要 websockets；若后续改用 WebSocket，再单独申请。
```

## 14. 待确认

1. 首期单 PTY 是否绑定到单一 role（建议 `dev`）；多 role 终端后续再做？
2. 第二个前端客户端：只读跟随，还是直接 409？（建议只读跟随）
3. `/chat` 无头接口在 P3 后的去留：保留兼容、返回 410，还是删除？
4. 对话 Tab 是否需要展示 `usage`/费用（只读）？
5. 重连回放的环形缓冲大小与降级策略（重绘/重启）是否接受？

## 15. 源码索引

| 文件 | 相关点 |
|---|---|
| `tools/orchestrator/transcript.py` | JSONL 路径规则、`read_history`、`_blocks`；增量读取在此基础上扩展 |
| `tools/orchestrator/service.py` | 现有 SSE、Bearer、`/sessions/history`、`/chat` 路由；新增 `/pty/*`、`/sessions/stream` |
| `tools/orchestrator/chat.py` | 现有 `claude -p` 对话引擎与 session 管理；P3 起停用为主路径 |
| `tools/orchestrator/launcher.py` | Windows Job Object、进程回收（PTY 复用） |
| `experiments/frontend-dashboard-tauri/src/lib/backend.ts` | daemon 地址、token、SSE fetch |
| `experiments/frontend-dashboard-tauri/src/lib/sse.ts` | SSE 帧解析与重连（PTY/JSONL 流可复用） |
