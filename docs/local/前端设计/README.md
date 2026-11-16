# 前端设计目录索引

> 目录定位：Prism（原 AgentSentinel）前端工作台（Tauri v2 + React）的设计、使用与界面行为说明；编排服务（开发期工具）的契约与历史实施记录也收在本目录。
> 维护：doc-agent；最近整理：2026-11-16（新增全项目架构映射入口；现状快照与动态角色方案移入 `../待清理/`）。目录现保留 **7 份文件**；合并前的原文件已移入 `归档/`，未删除。
> 边界：仅开发期工具，不接入产品；本目录位于 `docs/local/`，不在 git 版本控制内。

## 文件索引

| 文件 | 类别 | 状态 | 内容 |
|---|---|---|---|
| [README.md](README.md) | 索引 | 现行 | 本文件：目录导航、阅读顺序、源码对应、归档说明 |
| [设计文档.md](设计文档.md) | 设计 | 现行 + 草案 + 历史 | 四部分：①前端架构与 §5.2 契约（现行）②异构 Agent 嗅探草案（P1 已实现，P2/P3 未实现）③编排服务实施计划（历史设计依据）④交互面板与开发日志工单（草案，待评估） |
| [AgentSentinel 工作台定位与前端中间层规划.md](AgentSentinel%20工作台定位与前端中间层规划.md) | 规划 | 现行（对话模式基线） | 项目定位与中间层架构；对话面板现为 JSONL 旁路 + 块级渲染（零 token），不引入 xterm（终端 Tab 除外） |
| [使用与解析.md](使用与解析.md) | 使用 / 解析 | 第一部分现行；第二部分已过期 | ①编排服务启动/注册/派单/观测/排障（现行）②界面说明书基线 `a39cadd`，已被会话坞/五态/池形态取代 |
| [PTY终端与双模式方案.md](PTY终端与双模式方案.md) | 方案 | 已实现（P1–P3 已 push） | 单 PTY 常驻 + 本地 JSONL 旁路：终端 Tab（xterm 原始 ANSI）与对话 Tab（JSONL 气泡）共享同一 PTY；SSE+POST、阶段/验收/风险/依赖申请 |
| [Claude Code TUI 交互的安全对接与前端适配.md](Claude%20Code%20TUI%20交互的安全对接与前端适配.md) | 方案 | 已交付（P1/P2 `0a0c4da`，待 push；阶段二不立项） | Hook 机制替代 TUI 解析：`Notification` 横幅（阶段一，已实现）与 `PermissionRequest` 结构化确认（阶段二，仅保留方案）；含 `--settings` 注入前置验证清单与 auto 权限模式边界 |
| [PTY池架构演进设计草案.md](PTY池架构演进设计草案.md) | 设计 | 已拍板（2026-11-14）；P0 已实现（flag 默认关，工作区未提交） | 单终端→PTY 池：数据模型/生命周期/软上限 6 可调、TTL 60min、内存 80% 兜底、role↔terminal↔session、API 复数化、SSE/JSONL 隔离、Hook 路由、前端多终端与 WebGL、重启懒恢复、Job/clean_all、权限、分阶段/回滚；含 Shell→Agent 转换 A |

> 已过期并移入 `../待清理/`：`前端工作台现状（2026-11-15）.md`、`动态Agent角色与单终端切换方案.md`。全项目文件→模块映射与当前行为见 [../项目架构与文件映射（2026-11-16）.md](../项目架构与文件映射（2026-11-16）.md)。

## 阅读顺序

1. [设计文档.md](设计文档.md) **第一部分**：架构、技术选型、阶段 C1–C3 与 §5.2 后端接口契约。
2. [使用与解析.md](使用与解析.md) **第一部分**：daemon 启动、MCP 注册、派单、观测与排障。
3. [项目架构与文件映射（2026-11-16）.md](../项目架构与文件映射（2026-11-16）.md) **§6**：当前界面行为基线（布局 / 三页 / 五色 / Token / 渲染）。

按需补充：

- 全项目文件→模块映射与当前行为 → [../项目架构与文件映射（2026-11-16）.md](../项目架构与文件映射（2026-11-16）.md) **建议先读**
- 嗅探/野生进程与产品边界 → [设计文档.md](设计文档.md) 第二部分
- 编排服务设计依据、§2 契约与 P0 证据 → [设计文档.md](设计文档.md) 第三部分（历史）
- 交互面板/开发日志改造方向 → [设计文档.md](设计文档.md) 第四部分（草案，待可行性评估）
- CLI 权限等待的对话页提示（Hook → 横幅） → [Claude Code TUI 交互的安全对接与前端适配.md](Claude%20Code%20TUI%20交互的安全对接与前端适配.md)
- PTY 池演进选项 → [PTY池架构演进设计草案.md](PTY池架构演进设计草案.md)（P0 已实现，flag 默认关；待用户验收）
- 项目定位与对话模式基线 → [AgentSentinel 工作台定位与前端中间层规划.md](AgentSentinel%20工作台定位与前端中间层规划.md)
- PTY 终端 / 双模式 → [PTY终端与双模式方案.md](PTY终端与双模式方案.md)（已实现 P1–P3；见「当前实现基线」）

> 口径说明：工作台规划中「前端不引入 xterm.js」仅指**对话面板**；终端 Tab 使用 xterm.js。默认实现为**三页共享单 PTY**（对话 / 进度记录 / 原生终端）；池开关打开后每个会话各有独立 PTY，但会话内仍是三页共享。见 [../项目架构与文件映射（2026-11-16）.md](../项目架构与文件映射（2026-11-16）.md) §6。

## 与源码的对应关系

源码根：`experiments/frontend-dashboard-tauri/src/`

> 下表「参见」列中的“现状快照 §X”指向已移入 `../待清理/` 的 2026-11-15 快照；当前行为统一见 [../项目架构与文件映射（2026-11-16）.md](../项目架构与文件映射（2026-11-16）.md) §6。

| 源码 | 职责 | 参见 |
|---|---|---|
| `App.tsx` | 页面布局、启动、SSE/日志流接线、停止/检测、对话框状态 | 使用与解析 第二部分 §1、§2、§10 |
| `main.tsx` | React 挂载与 `AppErrorBoundary` 包裹 | 使用与解析 第二部分 §9 |
| `components/TopBar.tsx` | 顶栏摘要、连接徽标、命令入口 | 使用与解析 第二部分 §3 |
| `components/AgentTable.tsx` | Agents 表、停止按钮/外部进程展示 | 使用与解析 第二部分 §4 |
| `components/WildTable.tsx` | 未注册进程（wild）表 | 使用与解析 第二部分 §5 |
| `components/TaskTable.tsx` | Tasks 表、行选择 | 使用与解析 第二部分 §6.1 |
| `components/TaskDetailsDialog.tsx` | 任务详情对话框 | 使用与解析 第二部分 §6.2 |
| `components/ConsolePanel.tsx` | Console 过滤/搜索/暂停/清空、虚拟滚动 | 使用与解析 第二部分 §7 |
| `components/CommandPalette.tsx` | `Ctrl+K` 命令面板 | 使用与解析 第二部分 §8 |
| `components/AppErrorBoundary.tsx` | 渲染错误兜底 | 使用与解析 第二部分 §9 |
| `components/AgentsPanel.tsx` | 左侧唯一会话坞外壳、刷新、新建菜单、wild 折叠 | 现状快照 §4.2 |
| `components/SessionList.tsx` | Agent/Shell 会话行、头像、五色状态点、行操作 | 现状快照 §4.2 |
| `lib/sessionStatus.ts` | 五色状态机 `resolveSessionStatus` | 现状快照 §5 |
| `components/ChatPanel.tsx` | 对话页：气泡、输入、停止、交接、预算 | 现状快照 §4.3、§6 |
| `components/ProgressPanel.tsx` | 进度记录：工具时间轴（零 token） | 现状快照 §2.1 |
| `components/TerminalPanel.tsx` | 原生终端：xterm + FitAddon、按 terminal_id 路由、转换 Sheet | 现状快照 §4.3、§7 |
| `components/BudgetPopover.tsx` | 紫色预算条、精确 Tooltip、usage 增量动画 | 现状快照 §6 |
| `lib/backend.ts` | daemon 发现、token、HTTP 请求、SSE URL | 使用与解析 第二部分 §1、§10；设计文档 第一部分 §7.8 |
| `lib/sse.ts` | SSE 帧解析与重连退避 | 使用与解析 第二部分 §10；设计文档 第一部分 §7.9 |
| `lib/log.ts` | Console 条目、角色过滤、上限 500 | 使用与解析 第二部分 §7 |
| `lib/wild.ts` | wild 进程数据整形与过滤 | 使用与解析 第二部分 §5 |
| `lib/pty.ts` | `/pty/<id>/stream`、`/pty/<id>/chat/stream` SSE 解码 | 现状快照 §3.1 |
| `lib/usePtyChat.ts` | 对话页数据源：JSONL 旁路 + 按 terminal_id 写入 | 现状快照 §2、§3 |
| `lib/format.ts` | 本地时间、连接标签、错误文案 | 使用与解析 第二部分 §4、§6 |
| `lib/types.ts` | Agent/Task/WildProcess/DaemonInfo、`PoolTerminal`、`PoolStatus` 与活跃度字段 | 现状快照 §3.2 |
| `components/ui/*` | shadcn/ui 基础组件（无业务逻辑） | 设计文档 第一部分 §7 |
| `src-tauri/capabilities/default.json` | Tauri 权限最小集（shell/http/fs scope） | 设计文档 第一部分 §7.5 |

### 当前实现基线（2026-11-16）

> 完整事实见 [../项目架构与文件映射（2026-11-16）.md](../项目架构与文件映射（2026-11-16）.md) §6。本节只留索引级要点。

- **双形态**：默认三页共享单 PTY（对话 = JSONL 旁路、进度 = 工具时间轴、终端 = xterm 原始 ANSI；`POST /chat` 已 410）；`PRISM_TERMINAL_POOL=1`（旧名 `SENTINEL_TERMINAL_POOL` 读取兜底）时每会话独立 PTY，按 `terminal_id` 路由，默认终端 id=`1` 兼容，flag 默认关。
- **布局**：左侧唯一会话坞（Agent + 临时 Shell + 五色状态点 + `n/6` 容量），中间对话/进度/终端三页常驻切换，右侧任务面板可拖宽，底部 Console；顶栏收敛为 wordmark + 工作区 + SSE + `Ctrl+K`。
- **五色状态**：在线（绿）/ 运行中（蓝）/ 待处理（黄）/ 报错（红）/ 离线（灰）；`waiting` 无时间上限，长 `tool_use` 不衰减，`ended` 立即回绿；前端不解析 ANSI。
- **Token**：数字向下取整；1s Tooltip 显示精确整数；增量读 JSONL `usage.input_tokens+output_tokens` 差值，2.2s 合并；紫色分段预算 Popover 保留。
- **渲染与常驻**：xterm + WebGL 0.19（失败回退默认渲染器），SSE 24ms 合批；对话/终端切 Tab 不卸载，切回 16ms 后 `fit()` + resize；切换 `terminal_id`/工作区时旧面板按 key 卸载并释放 DOM/WebGL；附加终端只回放最近 200 行 / 20KB 纯文本（`term.reset()` → 写尾部 → `fit()` + `scrollToBottom()`），Agent 再 force resize 输出全量重绘；`scrollback: 1000`，滚动条 6px 暗灰；尺寸只在变化时发送（150ms 防抖），后端跳过同尺寸 SIGWINCH，后端钳制 cols 20–400 / rows 5–200。
- **权限等待**：`--settings` 注入 Hook（仅 `Notification` / `permission_prompt`）→ 对话页横幅提示切原生终端，不自动按键；默认 auto 模式不触发。
- **身份修复（2026-11-16）**：daemon 以 `identity_pid` 穿透 `uv/python` 包装链锚定宿主 `claude.exe`，旧身份存活即复用、跨 `/clear` 不换证；前端遇 409 `stale_agent` 静默查 `/status` 自愈。
- **状态**：`main = 654e472`（已 push）；本节改动**全部未提交/未 push**，等待用户验收后提交并进 P1。验证（2026-11-16 交接）：`pytest` 609 passed / 2 skipped、前端 124 passed、`ruff`/`mypy`/lint/build 0；daemon 已重启并加载新代码（pool=1）。

PTY 相关源码映射：前端 `experiments/frontend-dashboard-tauri/src/components/TerminalPanel.tsx`、`src/lib/usePtyChat.ts`、`src/lib/pty.ts`、`src/components/ChatPanel.tsx`、`src/components/ProgressPanel.tsx`、`src/App.tsx`；后端 `tools/orchestrator/pty_manager.py`（ConPTY、backlog、会话冲突重试、活动时间戳）、`tools/orchestrator/service.py`（`/pty/*`、`/pty/<id>/chat/stream`、池生命周期）、`tools/orchestrator/transcript.py`（`session_activity`）。

## 归档区

`归档/` 存放合并前的原文件与已被现行文档取代的历史内容；不参与日常阅读路径，不删除。

| 文件 | 来源 / 说明 |
|---|---|
| `归档/frontend-dashboard-plan.md` | 合并前原件 → 现为 `设计文档.md` 第一部分 |
| `归档/异构Agent嗅探层设计草案.md` | 合并前原件 → 现为 `设计文档.md` 第二部分 |
| `归档/编排服务实施计划.md` | 合并前原件 → 现为 `设计文档.md` 第三部分 |
| `归档/交互面板与开发日志设计.md` | 合并前原件 → 现为 `设计文档.md` 第四部分 |
| `归档/编排服务使用说明.md` | 合并前原件 → 现为 `使用与解析.md` 第一部分 |
| `归档/前端设计文档解析.md` | 合并前原件 → 现为 `使用与解析.md` 第二部分 |
| `归档/保留-旧编排实现要点.md` | 产品侧多 Agent 编排（上下文治理、异构模型调度、用量与预算）及旧开发期互通清理结论；不属当前中控台实现 |
| `归档/前端终端渲染极限优化与 CLI 智能交互适配.md` | 已交付（`4222370` + `ae8cc4f`）；当前行为见「当前实现基线」 |

## 目录处置说明

- 2026-11-11 按用户要求合并：设计类 → [设计文档.md](设计文档.md)；使用/解析类 → [使用与解析.md](使用与解析.md)；索引 → 本文件。原 6 份文件移入 `归档/`，未删除。
- 合并后各部分保留原有章节编号；跨部分引用写作「第 N 部分 §X」。合并前文件内的旧文件名引用只保留在归档原件中。
- 2026-11-12 新增 [PTY终端与双模式方案.md](PTY终端与双模式方案.md)（草案，未实施；按用户裁决采用单 PTY 常驻 + JSONL 旁路，旧的 PTY/`-p` 双引擎方案作废）与 [AgentSentinel 工作台定位与前端中间层规划.md](AgentSentinel%20工作台定位与前端中间层规划.md)（对话模式基线）；本索引与 `docs/local/README.md` 已同步。
- 后续移动或删除本目录内文件仍须用户确认（`docs/local/` 不在 git，不可恢复）。
- 2026-11-14 按用户要求：已完成且正文不再引用的《前端终端渲染极限优化与 CLI 智能交互适配.md》移入 `归档/`；当前行为见「当前实现基线」。
- 2026-11-15 按用户要求新增 [前端工作台现状（2026-11-15）.md](前端工作台现状（2026-11-15）.md)（dev-agent 提供当前实现事实，供 doc-agent 同步）；本索引与「当前实现基线」已同步刷新。
- 2026-11-16 按用户要求做全项目架构拆解：新增 [../项目架构与文件映射（2026-11-16）.md](../项目架构与文件映射（2026-11-16）.md)。同日将已过期的 `前端工作台现状（2026-11-15）.md` 与 `动态Agent角色与单终端切换方案.md` 移入 `../待清理/`（仅移动、未删除，待用户确认）；`使用与解析.md` 第二部分已标注过期。
