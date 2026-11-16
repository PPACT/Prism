# AgentSentinel 前端中控台（Tauri v2 + React）设计与架构方案

> **版本说明**：本文档为前端中控台的最新设计。目标不是简单监控看板，而是可替代本地终端的“前端控制台（Super Console）”。
> **2026-11-09 用户定稿**：采用 **Tauri v2 + React + Vite + TypeScript + Tailwind CSS + shadcn/ui**。Rust 端只负责用 Sidecar / Command 拉起现有 Python Daemon；**网络请求、SSE 监听、数据流全部在 React 的 JS/TS 侧实现**。先做隔离的“最小可运行脚手架”，测试通过并经用户确认后，才考虑并入项目。
> **方案 C 作废**：此前“原生 HTML + FastAPI 代理”的 C1 试验（`experiments/frontend-dashboard/`）仅作参考，不再投入，不迁入产品。

## 一、 设计定位与核心理念

1.  **一体化启动**：用户只启动桌面前端；前端通过 Tauri 的 Sidecar/Command 拉起现有 Python Daemon，并在 UI 显示“初始化 → 中控就绪”。
2.  **全要素可视化**：Agent 的角色、PID、PGID、状态（在线/离线/忙碌）、当前任务清晰可见。
3.  **前端即控制台**：不再要求 xterm 终端；用数据表格与 Console 列表流展示 Agent、任务、日志。所有信息流禁止聊天气泡。
4.  **强管控与熔断**：可对单个 Agent 执行停止；提供全局 `STOP ALL` 入口。

## 二、 核心 UI 模块规划

### 1. 顶部 Header
- Daemon 状态灯、监听地址、启动时间。
- 当前工作区路径与 Git Hash（后续接入）。
- 全局 `STOP ALL`。

### 2. 左侧栏：Agent 列表
- 表格/卡片：role、pid、pgid、status、alive、last_heartbeat、current_task_id、停止按钮。
- 点击选中 Agent，右侧/底部 Console 面板切换该 Agent 的日志（阶段二）。

### 3. 中间主区域：数据表格 / Console 列表流
- **不做 xterm.js 终端**；用主流可视化：数据表格 + 虚拟滚动日志列表。
- 任务列表：task_id、from_role、to_role、intent、status、updated_at。
- 日志/消息：按时间追加的 Console 行，等宽字体，不渲染聊天气泡。

### 4. 右侧栏：任务队列与审计
- 任务状态分组（queued/running/done/failed）。
- Daemon 日志与异常（可选）。

### 5. 底部：可折叠 Console 面板
- 默认折叠；展开后显示日志流/命令输出；支持拖拽改变高度。

### 6. 视觉与交互规范（2026-11-09 用户定稿）

**布局**：多面板 IDE 布局，面板支持拖拽改变宽高；底部 Console 可折叠。

**色彩**：深色模式为主。背景 `zinc-950`；卡片 `zinc-900`；边框 1px `zinc-800`；强调色使用低饱和度蓝/绿。

**组件**：shadcn/ui + Tailwind CSS。禁止任何聊天对话气泡；所有信息用 Console 列表流或数据表格展示。

**字体**：UI 用 Inter；代码、日志、终端用等宽字体（JetBrains Mono 或系统 monospace）。

**交互**：
- `Ctrl+K` 唤起全局命令面板。
- 日志列表强制虚拟滚动（Virtual Scroll）。
- 所有点击操作 100ms 内有视觉反馈；停止等操作采用乐观更新（先置 pending，失败回滚）。

## 三、 技术选型（2026-11-09 定稿）

| 项 | 选型 | 说明 |
|---|---|---|
| 桌面框架 | **Tauri v2** | 轻量；Rust 端只做进程托管 |
| 前端 | **React + Vite + TypeScript** | 所有网络/SSE/状态逻辑在 JS/TS |
| UI | **Tailwind CSS + shadcn/ui** | 深色 IDE 风格；组件按需复制 |
| 进程拉起 | Tauri **Sidecar 或 shell Command** | 拉起 Python Daemon；失败可见 |
| 网络 | **`@tauri-apps/plugin-http`** | 走 Rust 核心，绕过 WebView CORS；支持流式读取 |
| 文件读取 | **`@tauri-apps/plugin-fs`** | 读 `token` / `daemon.json`，token 不进前端包 |
| 实时通信 | **SSE**（`fetch` + `ReadableStream`） | 禁止 `EventSource` 与轮询 |
| 终端渲染 | **不需要 xterm.js** | 用表格/Console 列表流 |
| 虚拟滚动 | `@tanstack/react-virtual` | 日志与长列表 |
| 面板拖拽 | shadcn `resizable`（`react-resizable-panels`） | IDE 布局 |
| 测试 | Vitest + React Testing Library | 前端逻辑与 SSE 解析 |

**Rust 最小化原则**：Rust 端不加业务逻辑。网络请求、SSE 解析、终端数据流、命令面板全部用 JS/TS；若某段 Rust 逻辑复杂，优先使用 Tauri 官方插件或改在 JS 侧实现。

## 四、 联调清单

1.  **进程托管**：用 `@tauri-apps/plugin-shell` 的 `Command.create()` / `Command.sidecar()` 拉起 Daemon。需要声明 `shell:allow-spawn` / `shell:allow-kill`，并把允许的命令与参数写进 capability scope。若使用 Sidecar，二进制在 `tauri.conf.json > bundle > externalBin` 声明。
2.  **网络与 SSE**：用 `@tauri-apps/plugin-http` 的 `fetch()`（Rust 核心执行，绕过 WebView CORS）；SSE 通过 `response.body.getReader()` 按帧解析。**不要用浏览器原生 `EventSource`**（不能带 Authorization，且受 CORS 限制）。
3.  **Token**：从 `SENTINEL_ORCH_HOME/token` 读取（`plugin-fs`），只在 JS 内存中拼 Bearer 头；**不得写入 localStorage、不得打进前端包、不得打印日志**。
4.  **daemon 地址**：优先读取 `SENTINEL_ORCH_HOME/daemon.json` 的 `host` / `port`；脚手架可先用固定端口 `8765` 降低复杂度。
5.  **后端接口契约**：见 §5.2；前端只依赖这些接口，不为界面方便改 Daemon 语义。
6.  **CORS**：走 `plugin-http` 时不需要 Daemon 增加 CORS；若改用 WebView 原生 `fetch`，则必须处理 CORS。

## 五、 开发试验与后端接口契约

### 5.1 试验边界与集成门槛

- **位置**：`experiments/frontend-dashboard-tauri/`（新增，与 `agentsentinel/**`、`tools/orchestrator/**` 隔离）。试验阶段不接入产品、不改 Daemon。
- **形态**：Tauri v2 + React + Vite + TS；先做最小可运行脚手架与 C1 页面。
- **集成门槛**：`npm run build`、`cargo check`、前端单测、SSE 契约测试、静态“无轮询”检查全部通过，经用户确认后才迁入项目（建议 `frontend/`）。
- **依赖**：Node 工具链与 Rust 工具链只服务试验目录；不污染 Python `.venv`。

### 5.2 后端接口契约（前端只依赖这些）

所有 Bearer 接口：`Authorization: Bearer <token>`。

| 接口 | 方法 | 用途 | 鉴权 | 说明 |
|---|---|---|---|---|
| `/health` | GET | 启动探测 | 无 | `{ok,pid,started_at}` |
| `/status` | GET | 一次性快照 | Bearer | `{daemon:{pid,host,port,started_at}, agents:[Agent]}` |
| `/events` | GET | 实时推送（SSE） | Bearer | `id:<revision>`；`event: snapshot\|heartbeat\|shutdown`；`data` 为 `/status` 形状；20s `: ping` |
| `/logs/stream?tail=&role=` | GET | 后端日志流（SSE） | Bearer | 回放 `logs/*.log` 最后 `tail` 行（默认 200、上限 1000）并跟随新增行；`event: log` + `data: {role,line,ts}`；不传 `role` 时推送全部并在约 2s 内纳入新角色日志；20s `: ping`；`9344d33` |
| `/logs/report` | POST | 外部会话日志上报 | Bearer | 入参 `{role,line}`；role 白名单、空白折叠、单行 ≤4000；追加到 `logs/<role>.log`，供 `/logs/stream` 跟随；`d481a93` |
| `/tasks?role=&status=` | GET | 任务列表 | Bearer | `{tasks:[Task]}` |
| `/discover` | POST | 重新探测 agent 存活 + 缓存整理 + 未注册进程 | Bearer | 无入参；恢复/清理缓存并扫描野生进程，返回 `{discovered, recovered, pruned, pending_registered, pending_pruned, agents, wild}`；`wild` 字段见下；`ffed730`、`4cb2cbc` |
| `/task` | POST | 任务详情 | Bearer | 入参 `{task_id}`，返回 `{task, messages}` |
| `/stop` | POST | 停止角色 | Bearer | 入参 `{role}` |
| `/observability` | GET | 汇总视图 | Bearer | MVP 可选 |
| `/shutdown` | POST | 停止 daemon | Bearer | 前端 MVP 不调用 |

字段契约（源码 `tools/orchestrator/registry.py`、`router.py` 为准）：
- Agent：`role, pid, pgid, job_id, status, started_at, last_heartbeat, command, updated_at, current_task_id, cost_tokens, cost_budget, alive, project`
- Task：`task_id, from_role, to_role, intent, pointer, acceptance[], status, idempotency_key, max_tokens, max_minutes, attempts, created_at, updated_at, ack_at, completed_at, artifacts[], cost_tokens`
- SSE：`id` = `revision`（单调递增）；`shutdown` 的 `data` 为 `{"stopping": true}`。

**已确认边界**：
- `/events` 的 snapshot/heartbeat 只含 daemon + agents，不含 tasks；前端收到 **snapshot** 后按需重取 `/tasks`（事件驱动失效重取）；**heartbeat 不触发** `/tasks` 重取。
- SSE 断开按指数退避重连；daemon 重启后 revision 归零，重连时重置去重游标。
- 日志流已提供 `GET /logs/stream`（`9344d33`）；stdin 转发仍无后端接口，不在本阶段。
- 项目隔离：daemon 由 home 推导 `project`（`SENTINEL_PROJECT_ROOT` 可覆盖）；注册 payload 携带不同 project 返回 409；`/status` 的 daemon 与 agent 均含 project（`4d1713f`）。
- MCP server 在带 `SENTINEL_ROLE` 时启动即注册，并每 15s 重注册（`SENTINEL_ORCH_HEARTBEAT_SECONDS` 可调），daemon 重启后可再识别；`POST /discover` 供前端手动重探存活（`4d1713f`）。
- 角色缓存：MCP 注册成功后按 `CLAUDE_CODE_SESSION_ID` 写 `<home>/agents/<session>.json`（session/role/ref/project/started_at/mcp_pid/claude_pid）；启动无 `SENTINEL_ROLE` 时回退缓存角色并继续心跳。daemon 每 30s（`SENTINEL_ORCH_AGENT_CACHE_SECONDS`）恢复存活缓存、删除死缓存；`/discover` 复用同一逻辑并返回 recovered/pruned（`7aa7a80`）。窗口重置后 session id 变化，需按唤醒流程重新注册一次。
- 未注册进程（P1，只读）：`/discover` 的 `wild` 为进程树根列表，字段 `pid/ppid/cwd/cmdline/create_time/source/root/child_pids/bound_role`；按 cwd 过滤到当前项目，排除 daemon/已注册/基础设施子树，`cmdline` 截断 300 字符并掩码密钥；2s 缓存（`4cb2cbc`）。前端「未注册进程」折叠区只展示，不提供绑定；P2 绑定未实现。
- 产品边界：`/stop` 仅对 `job_id` 非空的 daemon 托管 worker 生效；外部 Agent 返回 409 `external_agent`，UI 显示「外部进程」且无停止按钮；不杀 MCP server、不注入、无 UI 启动托管 worker 入口（`ffed730`）。

### 5.3 测试门槛（未通过不集成）

1. `npm run build`（含 `tsc`）exit 0。
2. `cargo check`（`src-tauri`）exit 0。
3. `npm test`（Vitest + RTL）：SSE 帧解析、reconnect、401、Agent/Task 渲染、stop 乐观更新。
4. 静态检查：无 `setInterval` / `new EventSource` / 循环 `fetch` 轮询（重连退避除外）；源码与产物不含 token。
5. 手工 E2E：`npm run tauri dev` 打开窗口；拉起隔离 Daemon；Agent/Task 表格渲染；注册/停止 1s 内更新；断开 daemon 显示错误并重连。
6. 通过后由用户确认，才允许迁入项目。

## 六、 分阶段落地实施路径

### 📍 阶段 C1：最小可运行脚手架 + 进程可视化（当前）
*   **目标**：Tauri v2 + React 脚手架；Agent 列表、任务列表、Daemon 状态、停止按钮；SSE 实时更新。
*   **操作**：先交付 §7 的最小脚手架；再接入 §5.2 接口；禁止轮询。
*   **当前状态（2026-11-09）**：C1 基本完成（`8169cac`）：Tauri v2 + React 19 + Vite 8 + TS + Tailwind v4/shadcn；已接项目默认 daemon（dev/doc 同一 registry）、Console 事件流/停止乐观更新、`start_frontend.bat` 双击开发启动、Ctrl+K 黑屏修复；文档侧代码级复核通过（`0fb3920..8169cac`），待用户复测；C2 日志流与 Tasks 列宽已完成（`2bb42b0`、`9344d33`）。

### 📍 阶段 C2：Console 面板与命令面板
*   **目标**：底部可折叠 Console、虚拟滚动日志、`Ctrl+K` 命令面板、乐观更新完善。
*   **前置**：后端日志流 `GET /logs/stream` 已交付（`9344d33`）；stdin 交互另行设计。
*   **当前状态（2026-11-10）**：Console 已接后端真实日志流（回放 + 跟随、`[role]` 前缀、500 条上限）、角色过滤/搜索/暂停滚动/清空（`b15fdaa`）、Tasks 固定列宽/截断/本地时间（`2bb42b0`、`9344d33`）、任务详情对话框（`421eec0`）、外部会话日志上报（`d481a93`，Stop Hook；按用户裁决需移除 transcript 回退）；文档侧代码级复核通过，待用户确认。

### 📍 阶段 C3：桌面应用化与自启
*   **目标**：Tauri 打包、Sidecar 分发、点击图标自动拉起 Daemon、优雅退出。
*   **Windows 代码签名（2026-11-10 用户要求纳入 C3）**：
    1.  **证书选型**：EV/OV（硬件令牌或 HSM）或 Microsoft Trusted Signing。OV 需积累 SmartScreen 信誉；EV/Trusted Signing 更利于首次运行信任。
    2.  **Tauri v2 配置**（字段以本地 `@tauri-apps/cli/config.schema.json` 为准）：
        ```jsonc
        "bundle": {
          "windows": {
            "certificateThumbprint": "<证书 SHA1 指纹>",
            "digestAlgorithm": "sha256",
            "timestampUrl": "http://timestamp.digicert.com",
            "tsp": true
          }
        }
        ```
        EV/HSM/云签名使用 `signCommand: { "cmd": "<签名工具>", "args": ["...", "%1"] }`，`args` 中的 `%1` 为待签二进制占位。
    3.  **密钥管理**：证书/PFX/密码只放 CI secret 或本机证书库，禁止入库；签名脚本在 C3 落地（如 `scripts/sign_windows.ps1`）。
    4.  **验证**：`signtool verify /pa /all /v <file>` 与 `Get-AuthenticodeSignature`，覆盖 exe/msi/nsis 产物。
*   **开发期便利（可选）**：`.lnk` 或终端别名可减少 `.bat` 安全警告；该提示为 Windows 常规行为，不阻塞当前流程。

## 七、 最小可运行 Tauri 脚手架（v2 + React + TS）

> 以下是最小配置草案；具体版本号以 `npm create tauri-app` 与 `npm run tauri add` 生成的当前版本为准。所有路径以试验目录 `experiments/frontend-dashboard-tauri/` 为根。

### 7.1 前置条件

**2026-11-09 本机检查**：Node v24.14.1 ✅、npm 11.17.0 ✅、WebView2 ✅；Rust 1.99.1 ✅（rustup，stable-msvc）；MSVC VC.Tools.x86.x64 + Windows SDK 10.0.26100 ✅；`cargo build` 冒烟通过。安装由用户本人完成，agent 不做系统级安装。

- Node.js 20+（含 npm）——已具备。
- WebView2 运行时——已具备（Windows 11 自带）。
- **MSVC C++ 工作负载**（缺失）：
  1. 打开 Visual Studio Installer → 修改（Modify）。
  2. 勾选「使用 C++ 的桌面开发」（Desktop development with C++）。
  3. 确认包含「MSVC v143/V14x C++ x64/x86 生成工具」与「Windows SDK」（Windows 10/11 SDK 任一）。
  4. 安装完成后，在「Developer Command Prompt for VS」运行 `cl`，应打印编译器版本。
- **Rust stable（rustup）**（缺失）：
  1. 安装 rustup：`winget install --id Rustlang.Rustup -e`，或从 <https://win.rustup.rs/x86_64> 下载。
  2. 新开终端后执行：`rustup default stable-msvc`。
  3. 验证：`rustc --version`、`cargo --version`。
- 安装完成后**重开终端与 dev 会话**（PATH 需要刷新），再让 dev 重新检查工具链。
- 项目根可通过环境变量或配置提供：`AGENT_SENTINEL_ROOT`（用于 `cwd` 启动 `uv run`）。

### 7.2 创建命令与目录

```bash
cd experiments
npm create tauri-app@latest frontend-dashboard-tauri -- --template react-ts --manager npm
cd frontend-dashboard-tauri
npm install
npm run tauri add shell
npm run tauri add http
npm run tauri add fs
npx shadcn@latest init
npx shadcn@latest add button table resizable command dialog
npm install @tanstack/react-virtual @fontsource-variable/inter @fontsource/jetbrains-mono
npm install -D vitest @testing-library/react @testing-library/jest-dom jsdom
```

目录：

```text
experiments/frontend-dashboard-tauri/
├── package.json
├── vite.config.ts
├── tsconfig.json
├── index.html
├── src/
│   ├── main.tsx
│   ├── App.tsx
│   ├── styles.css
│   ├── components/            # shadcn/ui 与业务组件
│   └── lib/
│       ├── backend.ts         # token/daemon.json + plugin-http fetch
│       ├── sse.ts             # SSE 帧解析与重连
│       └── types.ts           # Agent/Task/SSE 类型
└── src-tauri/
    ├── Cargo.toml
    ├── build.rs
    ├── tauri.conf.json
    ├── capabilities/default.json
    └── src/
        ├── lib.rs
        └── main.rs
```

### 7.3 `package.json`（关键项）

```json
{
  "name": "agentsentinel-console",
  "private": true,
  "version": "0.1.0",
  "type": "module",
  "scripts": {
    "dev": "vite",
    "build": "tsc && vite build",
    "test": "vitest run",
    "tauri": "tauri"
  },
  "dependencies": {
    "@tauri-apps/api": "^2",
    "@tauri-apps/plugin-fs": "^2",
    "@tauri-apps/plugin-http": "^2",
    "@tauri-apps/plugin-shell": "^2",
    "@tanstack/react-virtual": "^3",
    "@fontsource-variable/inter": "^5",
    "@fontsource/jetbrains-mono": "^5",
    "react": "^19",
    "react-dom": "^19"
  },
  "devDependencies": {
    "@tauri-apps/cli": "^2",
    "@vitejs/plugin-react": "^4",
    "@tailwindcss/vite": "^4",
    "tailwindcss": "^4",
    "typescript": "^5",
    "vite": "^6",
    "vitest": "^3",
    "@testing-library/react": "^16",
    "@testing-library/jest-dom": "^6",
    "jsdom": "^26"
  }
}
```

### 7.4 `src-tauri/tauri.conf.json`

```json
{
  "$schema": "https://schema.tauri.app/config/2",
  "productName": "AgentSentinel Console",
  "version": "0.1.0",
  "identifier": "com.agentsentinel.console",
  "build": {
    "beforeDevCommand": "npm run dev",
    "devUrl": "http://localhost:1420",
    "beforeBuildCommand": "npm run build",
    "frontendDist": "../dist"
  },
  "app": {
    "windows": [
      {
        "label": "main",
        "title": "AgentSentinel Console",
        "width": 1280,
        "height": 800,
        "minWidth": 960,
        "minHeight": 600
      }
    ],
    "security": { "csp": null }
  },
  "bundle": {
    "active": true,
    "targets": "all",
    "icon": ["icons/32x32.png", "icons/128x128.png", "icons/128x128@2x.png", "icons/icon.icns", "icons/icon.ico"]
  }
}
```

### 7.5 `src-tauri/capabilities/default.json`

> 权限标识以 `npm run tauri add` 生成结果与当前 Tauri schema 为准；下面是最小集合。

```json
{
  "$schema": "../gen/schemas/desktop-schema.json",
  "identifier": "default",
  "description": "C1 minimal permissions",
  "windows": ["main"],
  "permissions": [
    "core:default",
    "shell:allow-kill",
    {
      "identifier": "shell:allow-spawn",
      "allow": [
        {
          "name": "start-daemon",
          "cmd": "uv",
          "args": ["run", "python", "-m", "tools.orchestrator.service", "--serve", "--port", "8765"],
          "sidecar": false
        }
      ]
    },
    {
      "identifier": "http:default",
      "allow": [{ "url": "http://127.0.0.1:8765/**" }]
    },
    {
      "identifier": "http:allow-fetch",
      "allow": [{ "url": "http://127.0.0.1:8765/**" }]
    },
    {
      "identifier": "fs:allow-read-text-file",
      "allow": [
        { "path": "$APPDATA/**/orchestrator/token" },
        { "path": "$APPDATA/**/orchestrator/daemon.json" }
      ]
    }
  ]
}
```

说明：
- `http:default` 本身不放大任何 origin，必须显式 allow URL；`/stop` 是 POST，若 `http:default` 不允许 POST，补 `http:allow-fetch`。
- URL scope 的端口匹配若不被接受，改用固定端口 `8765` 并检查 Tauri 的 URL pattern 语法。
- `fs` scope 若 `$APPDATA` 不适用，改为试验目录下的固定 `SENTINEL_ORCH_HOME`，并在 capability 中写对应绝对路径。

### 7.6 Rust 端（最小，不写业务逻辑）

`src-tauri/Cargo.toml`：

```toml
[package]
name = "agentsentinel-console"
version = "0.1.0"
edition = "2021"

[lib]
name = "agentsentinel_console_lib"
crate-type = ["staticlib", "cdylib", "rlib"]

[build-dependencies]
tauri-build = { version = "2", features = [] }

[dependencies]
tauri = { version = "2", features = [] }
tauri-plugin-shell = "2"
tauri-plugin-http = "2"
tauri-plugin-fs = "2"
serde = { version = "1", features = ["derive"] }
serde_json = "1"
```

`src-tauri/src/lib.rs`：

```rust
#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_http::init())
        .plugin(tauri_plugin_fs::init())
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
```

`src-tauri/src/main.rs`：

```rust
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    agentsentinel_console_lib::run()
}
```

`src-tauri/build.rs`：

```rust
fn main() {
    tauri_build::build()
}
```

### 7.7 `vite.config.ts`

```ts
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  clearScreen: false,
  server: { port: 1420, strictPort: true },
});
```

`src/styles.css` 开头：`@import "tailwindcss";`；字体在 `main.tsx` 引入：

```ts
import "@fontsource-variable/inter";
import "@fontsource/jetbrains-mono";
```

### 7.8 前端启动 Daemon 与读取配置（全在 JS）

`src/lib/backend.ts` 关键逻辑（示意）：

```ts
import { Command } from "@tauri-apps/plugin-shell";
import { readTextFile } from "@tauri-apps/plugin-fs";
import { fetch as tauriFetch } from "@tauri-apps/plugin-http";

const ORCH_HOME = /* 试验自用目录或项目 .claude/orchestrator */ "";
const BASE = "http://127.0.0.1:8765";

export async function startDaemon(): Promise<Command<string>> {
  const child = await Command.create(
    "uv",
    ["run", "python", "-m", "tools.orchestrator.service", "--serve", "--port", "8765"],
    { cwd: /* AgentSentinel 项目根 */, env: { SENTINEL_ORCH_HOME: ORCH_HOME } }
  ).spawn();
  return child;
}

export async function tokenHeader(): Promise<Record<string, string>> {
  const token = (await readTextFile(`${ORCH_HOME}/token`)).trim();
  return token ? { Authorization: `Bearer ${token}` } : {};
}

export async function getStatus() {
  const response = await tauriFetch(`${BASE}/status`, { headers: await tokenHeader() });
  return response.json();
}
```

> `cwd`、`ORCH_HOME` 与 capability scope 必须一致；启动失败要在 UI 显示可读错误，不留下空窗口。

### 7.9 SSE 客户端

- 复用 `fetch` + `ReadableStream` 的帧解析逻辑（字段 `id` / `event` / `data`，`:` 注释忽略）。
- 使用 `@tauri-apps/plugin-http` 的 `fetch`，带 Bearer 头。
- 断开指数退避重连；只对 `snapshot` 触发 `/tasks` 重取，`heartbeat` 不触发。
- 不引入 `EventSource`、不写 `setInterval`。

### 7.10 验收（最小脚手架）

1. `npm install` 成功；`npm run tauri dev` 打开窗口。
2. 窗口内布局可见：顶栏、左侧 Agent 表、任务表、底部 Console 占位。
3. 能拉起隔离 Daemon；`/status` 与 `/events` 可通；注册/停止后 UI 更新。
4. `npm run build`、`cargo check`、`npm test` exit 0。
5. 无轮询、无 token 泄漏；不并入产品、不 push 前端试验。

## 八、 避坑与硬性约束

1.  **Rust 最小化**：Rust 只做 Tauri 脚手架与进程拉起；网络、SSE、解析、状态管理全在 React JS/TS。复杂逻辑优先官方插件。
2.  **`http` 权限**：`http:default` 默认不放大任何 origin，必须显式 allow URL；`/stop` 的 POST 需要 `http:allow-fetch` 或等效权限。URL/方法都要匹配。
3.  **`shell` 权限**：`shell:allow-spawn` / `allow-kill` 必须按命令与参数 scope，禁止 `cmd: ""` 全放行。
4.  **token**：只从文件读入 JS 内存；禁止打进包、localStorage、日志、截图。
5.  **禁止轮询**：状态与日志一律 SSE；不得 `setInterval` / 循环 `fetch`。重连退避除外。
6.  **端口**：脚手架先用固定 `8765`；接项目 Daemon 后改为读 `daemon.json`，并同步 capability 的 URL scope。
7.  **capability 改动后重启**：`tauri dev` 需重启才生效。
8.  **方案 C 目录**：`experiments/frontend-dashboard/` 不投产、不并入；可保留作参考，确认无引用后删除。
9.  **状态同步**：不缓存 PID；以 SSE snapshot 与 `/status` 为准。
10. **防并发冲突**：前端停止操作与终端手动停止可能竞争；前端需显示 pending/失败回滚。
11. **产品边界（2026-11-10 用户拍板）**：中控只观察与调度，不终止外部进程。`/stop` 仅对 `job_id` 非空的 daemon 托管 worker 生效；外部 Agent 返回 409 `external_agent`，UI 显示「外部进程」且不提供停止按钮；不杀 MCP server、不注入、不新增退出协议；暂不提供 UI 启动托管 worker 入口。详见 `异构Agent嗅探层设计草案.md` §3。

## 九、 参考

- Tauri v2 项目创建与配置：<https://v2.tauri.app/start/create-project/>、<https://v2.tauri.app/reference/config/>
- Tauri v2 shell 插件：<https://v2.tauri.app/plugin/shell/>
- Tauri v2 HTTP 插件：<https://v2.tauri.app/plugin/http-client/>
- shadcn/ui + Vite：<https://ui.shadcn.com/docs/installation/vite>
