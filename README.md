# Prism（工作台外壳）

Prism 是 AgentSentinel 流水线之外的本地多 Agent 工作台：Tauri 窗口 + React 前端 + 常驻 daemon + PTY 池 + 进程嗅探。它只通过 HTTP/SSE 与本仓库自己的 daemon 通信，不依赖 `agentsentinel/` 产品代码。

## 目录

| 路径 | 内容 |
|---|---|
| `experiments/frontend-dashboard-tauri/` | Tauri 2 + React 前端（工作台 UI） |
| `tools/orchestrator/` | 本地 daemon、MCP server、PTY 管理、任务路由 |
| `scripts/` | 一键启动、清理、PTY 池演示、缓存探针 |
| `.claude/hooks/` | Stop Hook：上报助手消息、投递待办任务 |
| `tests/orchestrator/`、`tests/hooks/` | 工作台测试 |
| `docs/local/前端设计/` | 工作台设计与使用文档 |

## 快速开始

```powershell
uv venv --python 3.12
uv pip install -r requirements.txt
cd experiments/frontend-dashboard-tauri
npm ci
npm run tauri dev
```

或双击根目录 `启动工作台.vbs`。

## 环境变量

- 读取优先 `PRISM_*`，旧名 `SENTINEL_*` 仅作迁移期兜底；写入一律 `PRISM_*`。
- 常用：`PRISM_ORCH_HOME`、`PRISM_DATA_DIR`、`PRISM_TERMINAL_POOL=1`、`PRISM_TERMINAL_POOL_LIMIT`。
- 前端：`VITE_PRISM_ROOT`（旧名 `VITE_AGENT_SENTINEL_ROOT` 兜底）。
- 默认数据目录：`%APPDATA%\Prism`（旧 `%APPDATA%\AgentSentinel` 首次启动只复制不删除）。

## MCP

`.mcp.json` 以 `prism` 注册：

```powershell
uv run python tools/orchestrator/mcp_server.py
```

工具：`orchestrator_register/status/resolve/need/ack/completed/heartbeat/task/tasks`。

## 质量门槛

```powershell
pytest
ruff check .
ruff format --check .
mypy .
cd experiments/frontend-dashboard-tauri
npm test
npm run lint
npm run build
```
