import { ChevronUp, Terminal } from "lucide-react";
import {
  lazy,
  Suspense,
  useCallback,
  useEffect,
  useRef,
  useState,
  type PointerEvent as ReactPointerEvent,
} from "react";

import { AgentsPanel } from "@/components/AgentsPanel";
import { AgentConfigSheet } from "@/components/AgentConfigSheet";
import { CenterTabBar } from "@/components/CenterTabBar";
import { CommandPalette } from "@/components/CommandPalette";
import { ConsolePanel } from "@/components/ConsolePanel";
import { ProgressPanel } from "@/components/ProgressPanel";
import { TaskDetailsDialog } from "@/components/TaskDetailsDialog";
import { TasksPanel } from "@/components/TasksPanel";
import { TopBar } from "@/components/TopBar";
import { ToastViewport, type ToastItem } from "@/components/ToastViewport";
import { WorkspaceDialog } from "@/components/WorkspaceDialog";
import {
  activateTerminal,
  cleanupWildProcess,
  convertTerminal,
  createTerminal,
  deleteWorkspaceAgent,
  discoverAgents,
  ensureDaemon,
  ensureWorkspaceDaemon,
  eventStreamUrl,
  getStatus,
  getTasks,
  getTerminalPool,
  getWorkspaceAgent,
  getWorkspaces,
  getHandoff,
  logStreamUrl,
  removeTerminal,
  setDaemonHome,
  setStaleAgentHandler,
  shutdownAllDaemons,
  startHandoff,
  startPty,
  stopRole,
  stopTerminal,
  streamFetch,
  updateWorkspaceBudget,
} from "@/lib/backend";
import { errorMessage, formatTokenCount } from "@/lib/format";
import { isAgentOnline } from "@/lib/agentStatus";
import { reconcileTerminalAgentIds } from "@/lib/agentIdentity";
import { CENTER_TABS, type CenterTabKey } from "@/lib/centerTabs";
import { shortcutTarget } from "@/lib/keyboard";
import { appendBounded, logEventText, makeLogEntry, type LogEntry, type LogLevel } from "@/lib/log";
import { consumeEventStream, parseEventData } from "@/lib/sse";
import type {
  Agent,
  ConnectionState,
  EngineOption,
  HandoffStatus,
  PoolTerminal,
  StatusSnapshot,
  Task,
  WildProcess,
  Workspace,
  WorkspacesResponse,
} from "@/lib/types";import { useMediaQuery } from "@/lib/useMediaQuery";
import { usePtyChat } from "@/lib/usePtyChat";

// Markdown/高亮体积较大，聊天面板独立分包；主界面先渲染，面板异步加载。
const ChatPanel = lazy(() =>
  import("@/components/ChatPanel").then((module) => ({ default: module.ChatPanel })),
);
const TerminalPanel = lazy(() =>
  import("@/components/TerminalPanel").then((module) => ({ default: module.TerminalPanel })),
);

/** 角色/终端恢复健康后自动清掉对应的局部错误，避免红标长期挂着。 */
function pruneHealthyErrors(
  errors: Record<string, string>,
  healthy: Set<string>,
): Record<string, string> {
  const entries = Object.entries(errors).filter(([key]) => !healthy.has(key));
  if (entries.length === Object.keys(errors).length) return errors;
  return Object.fromEntries(entries);
}

/** 快捷键切换后把焦点送到目标面板：对话→输入框，终端→xterm 文本框，进度→Tab 按钮。 */
function focusCenterPane(key: CenterTabKey): void {
  if (key === "progress") {
    document.querySelector<HTMLButtonElement>('[data-tab-key="progress"]')?.focus();
    return;
  }
  const selector = key === "chat" ? "[data-chat-input]" : ".xterm-helper-textarea";
  const deadline = performance.now() + 800;
  const attempt = () => {
    const node = document.querySelector<HTMLElement>(selector);
    if (node) {
      node.focus();
      return;
    }
    if (performance.now() < deadline) window.setTimeout(attempt, 30);
  };
  window.setTimeout(attempt, 0);
}

export default function App() {
  const [agents, setAgents] = useState<Agent[]>([]);
  const [tasks, setTasks] = useState<Task[]>([]);
  const [selectedTask, setSelectedTask] = useState<Task | null>(null);
  const [connection, setConnection] = useState<ConnectionState>("connecting");
  const [globalError, setGlobalError] = useState<string | null>(null);
  const [agentErrors, setAgentErrors] = useState<Record<string, string>>({});
  const [terminalErrors, setTerminalErrors] = useState<Record<string, string>>({});
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [centerTab, setCenterTab] = useState<"chat" | "progress" | "terminal">("chat");
  const [selectedRole, setSelectedRole] = useState<string | null>(null);
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [engines, setEngines] = useState<EngineOption[]>([]);
  const [activeWorkspace, setActiveWorkspace] = useState<Workspace | null>(null);
  const [workspaceDialogOpen, setWorkspaceDialogOpen] = useState(false);
  const [agentSheet, setAgentSheet] = useState<{ role: string | null } | null>(null);
  const [workspaceEpoch, setWorkspaceEpoch] = useState(0);
  const [budgetOverride, setBudgetOverride] = useState<number | null>(null);
  const [handoff, setHandoff] = useState<HandoffStatus | null>(null);
  const [terminalPoolEnabled, setTerminalPoolEnabled] = useState(false);
  const [poolTerminals, setPoolTerminals] = useState<PoolTerminal[]>([]);
  const [poolLimit, setPoolLimit] = useState(6);
  const [activeTerminalId, setActiveTerminalId] = useState("1");
  const [poolBusy, setPoolBusy] = useState(false);
  const [toasts, setToasts] = useState<ToastItem[]>([]);

  /** 操作/业务错误走轻提示：Toast 自己负责 4 秒后播放退场动画再移除。 */
  const pushToast = useCallback((message: string) => {
    const id = Date.now() + Math.random();
    setToasts((prev) => [...prev, { id, message }]);
  }, []);
  const dismissToast = useCallback((id: number) => {
    setToasts((prev) => prev.filter((toast) => toast.id !== id));
  }, []);
  const fallbackRole = agents.find((agent) => isAgentOnline(agent) && agent.role)?.role ?? null;
  const selectedExists = agents.some((agent) => agent.role === selectedRole);
  const managedRole =
    selectedRole === null || (agents.length > 0 && !selectedExists) ? fallbackRole : selectedRole;
  const poolSessions = poolTerminals.filter((terminal) => terminal.kind !== "legacy");
  const requestedTerminal = poolTerminals.find(
    (terminal) => terminal.terminal_id === activeTerminalId,
  );
  const resolvedTerminalId = terminalPoolEnabled
    ? requestedTerminal && requestedTerminal.kind !== "legacy"
      ? requestedTerminal.terminal_id
      : ([...poolSessions].sort((a, b) => (a.created_at ?? 0) - (b.created_at ?? 0))[0]
          ?.terminal_id ?? activeTerminalId)
    : activeTerminalId;
  const poolTerminal = terminalPoolEnabled
    ? (poolTerminals.find((terminal) => terminal.terminal_id === resolvedTerminalId) ?? null)
    : null;
  const activeRole =
    terminalPoolEnabled && poolTerminal && poolTerminal.kind !== "legacy"
      ? poolTerminal.role || null
      : managedRole;
  const selectedAgent = agents.find((agent) => agent.role === activeRole);
  const selectedAgentId =
    terminalPoolEnabled && poolTerminal?.agent_id
      ? poolTerminal.agent_id
      : (selectedAgent?.agent_id ?? null);
  const selectedOnline =
    terminalPoolEnabled && poolTerminal && poolTerminal.kind !== "legacy"
      ? poolTerminal.state === "running" &&
        (poolTerminal.kind !== "agent" || isAgentOnline(selectedAgent))
      : isAgentOnline(selectedAgent);
  const shellTerminal = terminalPoolEnabled && poolTerminal?.kind === "shell";
  const chatSupported =
    activeWorkspace === null ? false : (activeWorkspace.capabilities?.chat ?? true);
  const chatEnabled = chatSupported && !shellTerminal;
  const effectiveCenterTab = chatEnabled ? centerTab : "terminal";
  const visibleTabs = CENTER_TABS.filter((tab) => chatEnabled || tab.key === "terminal");
  const terminalLocked = !selectedOnline;
  const terminalLockReason = activeRole
    ? "当前 Agent 已离线，终端已锁定"
    : "请先在左侧选择在线 Agent";
  const chatLockReason = activeRole
    ? "当前 Agent 已离线，对话已锁定"
    : "请先在左侧选择在线 Agent";
  const ptyChat = usePtyChat(
    activeRole,
    selectedAgentId,
    selectedOnline && chatEnabled,
    workspaceEpoch,
    resolvedTerminalId,
    pushToast,
  );
  const activeHandoff =
    handoff && handoff.role === activeRole && handoff.agent_id === selectedAgentId
      ? handoff
      : null;
  const effectiveBudget =
    budgetOverride ?? activeWorkspace?.budget_tokens ?? ptyChat.budgetTokens;
  const [pendingRole, setPendingRole] = useState<string | null>(null);
  const [discovering, setDiscovering] = useState(false);
  const [cleaningPid, setCleaningPid] = useState<number | null>(null);
  const [wild, setWild] = useState<WildProcess[]>([]);
  const [wildOpen, setWildOpen] = useState(true);
  const [logs, setLogs] = useState<LogEntry[]>([]);
  const [tasksWidth, setTasksWidth] = useState(340);
  const [consoleOpen, setConsoleOpen] = useState(false);
  const [consoleHeight, setConsoleHeight] = useState(240);
  const [agentsDrawer, setAgentsDrawer] = useState(false);
  const [tasksDrawer, setTasksDrawer] = useState(false);
  const narrowTasks = useMediaQuery("(max-width: 1199px)");
  const narrowAgents = useMediaQuery("(max-width: 899px)");
  const taskDrag = useRef<{ startX: number; startWidth: number } | null>(null);
  const consoleDrag = useRef<{ startY: number; startHeight: number } | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const logAbortRef = useRef<AbortController | null>(null);
  const tasksTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const logIdRef = useRef(0);
  const agentsRef = useRef<Agent[]>([]);
  const poolTerminalsRef = useRef<PoolTerminal[]>([]);

  useEffect(() => {
    agentsRef.current = agents;
  }, [agents]);

  useEffect(() => {
    poolTerminalsRef.current = poolTerminals;
  }, [poolTerminals]);

  const appendLog = useCallback((level: LogLevel, text: string, at?: Date, role?: string) => {
    const entry = makeLogEntry(logIdRef.current++, level, text, at, role);
    setLogs((prev) => appendBounded(prev, entry));
  }, []);

  /** Agent 级错误只落到对应会话行与面板，绝不升级为全局红条。 */
  const setAgentError = useCallback((role: string | null | undefined, message: string | null) => {
    const key = (role ?? "").trim();
    if (!key) return;
    setAgentErrors((prev) => {
      if (message === null) {
        if (!(key in prev)) return prev;
        const next = { ...prev };
        delete next[key];
        return next;
      }
      return prev[key] === message ? prev : { ...prev, [key]: message };
    });
  }, []);

  const setTerminalError = useCallback(
    (terminalId: string | null | undefined, message: string | null) => {
      const key = (terminalId ?? "").trim();
      if (!key) return;
      setTerminalErrors((prev) => {
        if (message === null) {
          if (!(key in prev)) return prev;
          const next = { ...prev };
          delete next[key];
          return next;
        }
        return prev[key] === message ? prev : { ...prev, [key]: message };
      });
    },
    [],
  );

  const applySnapshot = useCallback((snapshot: StatusSnapshot) => {
    const nextAgents = Array.isArray(snapshot.agents) ? snapshot.agents : [];
    const nextTerminals = Array.isArray(snapshot.terminals) ? snapshot.terminals : [];
    setAgents(nextAgents);
    setTerminalPoolEnabled(Boolean(snapshot.terminal_pool));
    if (Array.isArray(snapshot.terminals)) {
      // 双保险：后端已有身份复用，前端仍按 live Agent 修正终端旧句柄。
      setPoolTerminals(reconcileTerminalAgentIds(nextAgents, nextTerminals));
    }
    const healthyRoles = new Set(
      nextAgents
        .filter((agent) => isAgentOnline(agent) && agent.role)
        .map((agent) => String(agent.role)),
    );
    const runningTerminals = new Set(
      nextTerminals.filter((terminal) => terminal.state === "running").map((terminal) => terminal.terminal_id),
    );
    setAgentErrors((prev) => pruneHealthyErrors(prev, healthyRoles));
    setTerminalErrors((prev) => pruneHealthyErrors(prev, runningTerminals));
    if (typeof snapshot.terminal_pool_limit === "number") {
      setPoolLimit(snapshot.terminal_pool_limit);
    }
  }, []);

  useEffect(() => {
    // 极端情况下后端仍返回 stale_agent：静默查 /status，按存活角色刷新身份证。
    setStaleAgentHandler(() => {
      void getStatus()
        .then((snapshot) => applySnapshot(snapshot))
        .catch(() => undefined);
    });
    return () => setStaleAgentHandler(null);
  }, [applySnapshot]);

  const refreshWorkspaces = useCallback(async (): Promise<WorkspacesResponse | null> => {
    try {
      const data = await getWorkspaces();
      setWorkspaces(data.workspaces ?? []);
      setEngines(data.engines ?? []);
      setActiveWorkspace(
        data.workspaces.find((workspace) => workspace.id === data.active_id) ??
          data.workspaces[0] ??
          null,
      );
      return data;
    } catch (err) {
      appendLog("warn", `读取工作区失败：${errorMessage(err)}`);
      return null;
    }
  }, [appendLog]);

  const refreshPool = useCallback(async () => {
    try {
      const pool = await getTerminalPool();
      setPoolTerminals(reconcileTerminalAgentIds(agentsRef.current, pool.terminals));
      setPoolLimit(pool.limit);
      setTerminalPoolEnabled(pool.enabled);
      if (!pool.terminals.some((terminal) => terminal.terminal_id === activeTerminalId)) {
        setActiveTerminalId(pool.active_terminal_id || "1");
        setSelectedRole(null);
      }
    } catch (err) {
      appendLog("warn", `终端池刷新失败：${errorMessage(err)}`);
    }
  }, [activeTerminalId, appendLog]);

  const focusTerminal = useCallback(
    async (terminalId: string) => {
      const target = poolTerminals.find((terminal) => terminal.terminal_id === terminalId);
      const targetAgent = target?.role
        ? agents.find((agent) => agent.role === target.role)
        : undefined;
      setActiveTerminalId(terminalId);
      setSelectedRole(target?.role || null);
      // 切换 Agent 即清掉上一条局部错误，避免红条跨会话残留。
      setAgentError(target?.role, null);
      setTerminalError(terminalId, null);
      try {
        await activateTerminal(terminalId);
        // 终端停止、或本地角色记录离线时，点击即懒加载拉起并重新绑定。
        const needsRevive =
          target?.state === "stopped" ||
          (target?.kind === "agent" && target.role !== "" && !isAgentOnline(targetAgent));
        if (needsRevive) {
          await startPty(target?.role || undefined, target?.agent_id || undefined, terminalId);
          await refreshPool();
          applySnapshot(await getStatus());
        }
        setAgentError(target?.role, null);
        setTerminalError(terminalId, null);
      } catch (err) {
        const message = errorMessage(err);
        if (message.includes("agent offline") || message.includes("stale agent")) {
          setAgentError(target?.role, message);
          setTerminalError(terminalId, message);
        } else {
          pushToast(message);
        }
        appendLog("warn", `${target?.role || terminalId}：${message}`);
      }
    },
    [
      agents,
      appendLog,
      applySnapshot,
      poolTerminals,
      pushToast,
      refreshPool,
      setAgentError,
      setTerminalError,
    ],
  );

  const handleCreateShell = useCallback(async () => {
    setPoolBusy(true);
    try {
      const { terminal } = await createTerminal("shell");
      setActiveTerminalId(terminal.terminal_id);
      setSelectedRole(null);
      await activateTerminal(terminal.terminal_id);
      await refreshPool();
      appendLog("info", `已新建 Shell 终端 ${terminal.terminal_id}`);
    } catch (err) {
      const message = errorMessage(err);
      appendLog("error", `新建临时终端失败：${message}`);
      pushToast(`新建临时终端失败：${message}`);
    } finally {
      setPoolBusy(false);
    }
  }, [appendLog, pushToast, refreshPool]);

  const selectAgentSession = useCallback(
    async (role: string) => {
      if (!terminalPoolEnabled) {
        setSelectedRole(role);
        setAgentError(role, null);
        const agent = agents.find((item) => item.role === role);
        // 非池模式：点击离线本地角色时懒加载拉起 legacy PTY，避免对话永久锁死。
        if (agent && !isAgentOnline(agent)) {
          try {
            await startPty(role, agent.agent_id);
            applySnapshot(await getStatus());
          } catch (err) {
            const message = errorMessage(err);
            if (message.includes("agent offline") || message.includes("stale agent")) {
              setAgentError(role, message);
            } else {
              pushToast(message);
            }
            appendLog("warn", `${role}：${message}`);
          }
        }
        return;
      }
      const existing = poolTerminals.find(
        (terminal) => terminal.kind === "agent" && terminal.role === role,
      );
      if (existing) {
        await focusTerminal(existing.terminal_id);
        return;
      }
      setPoolBusy(true);
      setAgentError(role, null);
      try {
        const { terminal } = await createTerminal("agent", role, "");
        setActiveTerminalId(terminal.terminal_id);
        setSelectedRole(role);
        await activateTerminal(terminal.terminal_id);
        await refreshPool();
        appendLog("info", `已为 ${role} 创建池终端 ${terminal.terminal_id}`);
      } catch (err) {
        const message = errorMessage(err);
        pushToast(message);
        appendLog("warn", `${role}：${message}`);
      } finally {
        setPoolBusy(false);
      }
    },
    [
      agents,
      appendLog,
      applySnapshot,
      focusTerminal,
      poolTerminals,
      pushToast,
      refreshPool,
      setAgentError,
      terminalPoolEnabled,
    ],
  );

  const handleConvertTerminal = useCallback(
    async (terminalId: string, role: string) => {
      setPoolBusy(true);
      setAgentError(role, null);
      setTerminalError(terminalId, null);
      try {
        const { terminal } = await convertTerminal(terminalId, role);
        setSelectedRole(terminal.role || role);
        setActiveTerminalId(terminalId);
        await refreshPool();
        appendLog("info", `终端 ${terminalId} 已转换为 Agent ${role}`);
      } catch (err) {
        const message = errorMessage(err);
        pushToast(message);
        appendLog("warn", `${role}：${message}`);
        await refreshPool();
      } finally {
        setPoolBusy(false);
      }
    },
    [appendLog, pushToast, refreshPool, setAgentError, setTerminalError],
  );

  const handleRemoveTerminal = useCallback(
    async (terminalId: string) => {
      const target = poolTerminals.find((terminal) => terminal.terminal_id === terminalId);
      if (!target) return;
      setPoolBusy(true);
      try {
        if (target.kind === "agent") {
          // X = 下线：杀进程但保留角色与终端记录，状态转灰。
          await stopTerminal(terminalId);
          appendLog("info", `终端 ${terminalId} 已下线`);
        } else {
          await removeTerminal(terminalId, true);
          if (resolvedTerminalId === terminalId) {
            setActiveTerminalId("1");
            setSelectedRole(null);
          }
          appendLog("info", `已关闭终端 ${terminalId}`);
        }
        setAgentError(target.role, null);
        setTerminalError(terminalId, null);
        await refreshPool();
      } catch (err) {
        const message = errorMessage(err);
        if (message.includes("agent offline")) {
          // 已经离线再下线：静默同步，不让幂等冲突变成红条。
          appendLog("info", `${target.role || terminalId} 已离线，已同步状态`);
          setAgentError(target.role, null);
          await refreshPool();
        } else {
          pushToast(message);
          appendLog("warn", `${target.role || terminalId}：${message}`);
        }
      } finally {
        setPoolBusy(false);
      }
    },
    [
      appendLog,
      poolTerminals,
      pushToast,
      refreshPool,
      resolvedTerminalId,
      setAgentError,
      setTerminalError,
    ],
  );

  const handleHandoff = useCallback(async () => {
    if (!activeRole || !selectedAgentId || !activeWorkspace) return;
    // 再次点击交接：先清掉上一轮红条/错误。
    setAgentError(activeRole, null);
    setTerminalError(resolvedTerminalId, null);
    setHandoff(null);
    try {
      const config = await getWorkspaceAgent(activeWorkspace.id, activeRole);
      if (!config.agent || !config.agent.configured) {
        const message = `当前 Agent（${activeRole}）未配置唤醒词`;
        appendLog("warn", message);
        pushToast(`${message}；请到 设置/工作区 → Agent 唤醒词 配置`);
        return;
      }
      setHandoff(await startHandoff(activeRole, selectedAgentId));
    } catch (err) {
      const message = errorMessage(err);
      appendLog("error", `一键交接失败：${message}`);
      pushToast(`一键交接失败：${message}`);
    }
  }, [
    activeRole,
    selectedAgentId,
    activeWorkspace,
    appendLog,
    pushToast,
    resolvedTerminalId,
    setAgentError,
    setTerminalError,
  ]);

  const handleBudgetChange = useCallback(
    async (tokens: number) => {
      if (!activeWorkspace) return;
      try {
        const data = await updateWorkspaceBudget(activeWorkspace.id, tokens);
        setBudgetOverride(tokens);
        setActiveWorkspace(data.workspace);
        setWorkspaces((previous) =>
          previous.map((workspace) =>
            workspace.id === data.workspace.id ? data.workspace : workspace,
          ),
        );
        appendLog("info", `上下文预算：${tokens.toLocaleString()} tokens`);
      } catch (err) {
        const message = errorMessage(err);
        appendLog("error", `预算更新失败：${message}`);
        pushToast(`预算更新失败：${message}`);
      }
    },
    [activeWorkspace, appendLog, pushToast],
  );

  useEffect(() => {
    if (!handoff?.active) return;
    const timer = setInterval(() => {
      void getHandoff()
        .then((status) => {
          setHandoff(status);
          if (!status.active && status.phase === "error") {
            pushToast(`一键交接失败：${status.error || status.message}`);
            setHandoff(null);
            return;
          }
          if (!status.active && status.phase === "done") {
            setWorkspaceEpoch((value) => value + 1);
          }
        })
        .catch(() => undefined);
    }, 500);
    return () => clearInterval(timer);
  }, [handoff?.active, pushToast]);

  const refreshTasks = useCallback(async () => {
    try {
      const data = await getTasks();
      setTasks(Array.isArray(data.tasks) ? data.tasks : []);
    } catch (err) {
      appendLog("warn", `读取任务失败：${errorMessage(err)}`);
    }
  }, [appendLog]);

  /** 仅由 SSE snapshot 触发：去抖 250ms 重取 /tasks（事件驱动失效重取，不是轮询）。 */
  const scheduleTasksRefresh = useCallback(() => {
    if (tasksTimerRef.current !== null) clearTimeout(tasksTimerRef.current);
    tasksTimerRef.current = setTimeout(() => {
      tasksTimerRef.current = null;
      void refreshTasks();
    }, 250);
  }, [refreshTasks]);

  const startStream = useCallback(() => {
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    setConnection("connecting");
    appendLog("info", "正在连接中控事件流…");
    void consumeEventStream({
      url: eventStreamUrl,
      fetchImpl: streamFetch,
      signal: controller.signal,
      onFrame(frame) {
        const payload = parseEventData<StatusSnapshot>(frame);
        if (payload && (frame.event === "snapshot" || frame.event === "heartbeat")) {
          applySnapshot(payload);
          setGlobalError(null);
        }
        if (frame.event === "snapshot") {
          appendLog("info", `snapshot #${frame.id ?? "-"} · agents=${payload?.agents?.length ?? 0}`);
        }
        if (frame.event === "shutdown") {
          setConnection("closed");
          appendLog("warn", "中控已停止（shutdown）");
        }
        if (frame.event === "agent_removed") {
          const removedRole = (payload as unknown as { role?: string } | null)?.role;
          if (removedRole) {
            const removedTerminals = poolTerminalsRef.current
              .filter((terminal) => terminal.role === removedRole)
              .map((terminal) => terminal.terminal_id);
            setAgents((prev) => prev.filter((agent) => agent.role !== removedRole));
            setPoolTerminals((prev) => prev.filter((terminal) => terminal.role !== removedRole));
            setAgentErrors((prev) => {
              if (!(removedRole in prev)) return prev;
              const next = { ...prev };
              delete next[removedRole];
              return next;
            });
            if (removedTerminals.length > 0) {
              setTerminalErrors((prev) => {
                const next = { ...prev };
                for (const terminalId of removedTerminals) delete next[terminalId];
                return next;
              });
            }
            setSelectedRole((prev) => (prev === removedRole ? null : prev));
            setActiveTerminalId((prev) =>
              removedTerminals.includes(prev) ? "1" : prev,
            );
            appendLog("info", `角色已移除：${removedRole}`);
          }
        }
        if (frame.event === "snapshot") scheduleTasksRefresh();
      },
      onState(state) {
        setConnection(state);
        if (state === "open") appendLog("info", "SSE 已连接");
        else if (state === "closed") appendLog("warn", "SSE 已关闭，准备重连");
        else if (state === "error") appendLog("warn", "SSE 连接中断，正在重连");
      },
      onError(err) {
        setGlobalError(errorMessage(err));
        appendLog("warn", errorMessage(err));
      },
    });
  }, [applySnapshot, appendLog, scheduleTasksRefresh]);

  /** 后端真实日志流（logs/<role>.log + daemon.log）；与状态 SSE 分开，互不影响。 */
  const startLogStream = useCallback(() => {
    logAbortRef.current?.abort();
    const controller = new AbortController();
    logAbortRef.current = controller;
    void consumeEventStream({
      url: async () => `${await logStreamUrl()}?tail=100`,
      fetchImpl: streamFetch,
      signal: controller.signal,
      onFrame(frame) {
        if (frame.event !== "log") return;
        const payload = parseEventData<{ role?: string; line?: string; ts?: string }>(frame);
        if (payload?.line) {
          const parsed = payload.ts ? new Date(payload.ts) : null;
          const at = parsed && !Number.isNaN(parsed.getTime()) ? parsed : undefined;
          appendLog("info", logEventText(payload.role, payload.line), at, payload.role);
        }
      },
      onState() {},
      onError() {
        // 状态流会提示连接错误；日志流保持静默重连，避免刷屏。
      },
    });
  }, [appendLog]);

  const reconnectStreams = useCallback(() => {
    startStream();
    startLogStream();
  }, [startLogStream, startStream]);

  const refreshAll = useCallback(async () => {
    try {
      applySnapshot(await getStatus());
      await refreshTasks();
      setGlobalError(null);
    } catch (err) {
      setGlobalError(errorMessage(err));
    }
  }, [applySnapshot, refreshTasks]);

  const switchWorkspace = useCallback(
    async (workspace: Workspace | null) => {
      abortRef.current?.abort();
      logAbortRef.current?.abort();
      setConnection("connecting");
      setAgents([]);
      setTasks([]);
      setSelectedRole(null);
      setHandoff(null);
      setBudgetOverride(null);
      try {
        if (workspace) await ensureWorkspaceDaemon(workspace);
        setDaemonHome(workspace?.daemon_home ?? null);
        setActiveWorkspace(workspace);
        setWorkspaceEpoch((value) => value + 1);
        await refreshAll();
        startStream();
        startLogStream();
        appendLog("info", `已切换工作区：${workspace?.name ?? "控制面"}`);
      } catch (err) {
        const message = errorMessage(err);
        setGlobalError(message);
        appendLog("error", message);
        setConnection("stopped");
      }
    },
    [appendLog, refreshAll, startLogStream, startStream],
  );

  const handleWorkspacesChanged = useCallback(
    (data: WorkspacesResponse) => {
      setWorkspaces(data.workspaces ?? []);
      setEngines(data.engines ?? []);
      const active =
        data.workspaces.find((workspace) => workspace.id === data.active_id) ??
        data.workspaces[0] ??
        null;
      if ((active?.daemon_home ?? null) !== (activeWorkspace?.daemon_home ?? null)) {
        void switchWorkspace(active);
        return;
      }
      setActiveWorkspace(active);
      setWorkspaceEpoch((value) => value + 1);
      void refreshAll();
      appendLog("info", `工作区已更新：${active?.name ?? "未选择"}`);
    },
    [activeWorkspace, appendLog, refreshAll, switchWorkspace],
  );

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        await ensureDaemon();
        if (cancelled) return;
        appendLog("info", "daemon 已就绪，加载状态…");
        const data = await refreshWorkspaces();
        const active =
          data?.workspaces.find((workspace) => workspace.id === data.active_id) ??
          data?.workspaces[0] ??
          null;
        if (active) await ensureWorkspaceDaemon(active);
        setDaemonHome(active?.daemon_home ?? null);
        setActiveWorkspace(active);
        await refreshAll();
        startStream();
        startLogStream();
      } catch (err) {
        if (!cancelled) {
          const message = errorMessage(err);
          setGlobalError(message);
          appendLog("error", message);
          setConnection("stopped");
        }
      }
    })();
    const onKeyDown = (event: KeyboardEvent) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        setPaletteOpen(true);
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => {
      cancelled = true;
      window.removeEventListener("keydown", onKeyDown);
      abortRef.current?.abort();
      logAbortRef.current?.abort();
      if (tasksTimerRef.current !== null) clearTimeout(tasksTimerRef.current);
    };
  }, [appendLog, refreshAll, refreshWorkspaces, startLogStream, startStream]);

  // 全局切换：Ctrl/⌘+Tab（Shift 反向）、Alt+1/2/3；捕获阶段拦截，避免终端吞键。
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      const tabs = CENTER_TABS.filter((tab) => chatEnabled || tab.key === "terminal");
      const target = shortcutTarget(tabs, effectiveCenterTab, event);
      if (!target) return;
      event.preventDefault();
      event.stopPropagation();
      setCenterTab(target.key);
      focusCenterPane(target.key);
    };
    window.addEventListener("keydown", onKeyDown, true);
    return () => window.removeEventListener("keydown", onKeyDown, true);
  }, [chatEnabled, effectiveCenterTab]);

  // 点击窗口 X：先让 daemon 优雅退出（Job Object 回收 PTY/claude），确认后再销毁窗口。
  useEffect(() => {
    let unlisten: (() => void) | undefined;
    let disposed = false;
    let closing = false;
    void (async () => {
      try {
        const { getCurrentWindow } = await import("@tauri-apps/api/window");
        const currentWindow = getCurrentWindow();
        unlisten = await currentWindow.onCloseRequested(async (event) => {
          event.preventDefault();
          if (closing) return;
          closing = true;
          try {
            await shutdownAllDaemons();
          } catch (error) {
            appendLog("warn", `关闭中控时清理 daemon 失败：${errorMessage(error)}`);
          } finally {
            await currentWindow.destroy();
          }
        });
        if (disposed) {
          unlisten();
          unlisten = undefined;
        }
      } catch {
        // 非 Tauri 环境（单元测试）忽略
      }
    })();
    return () => {
      disposed = true;
      unlisten?.();
    };
  }, [appendLog]);

  // 切换 Agent 只切换视图；PTY/session 由后端续接，前端不再主动 stop。
  const handleStop = useCallback(
    async (role: string) => {
      setPendingRole(role);
      setAgents((prev) =>
        prev.map((agent) => (agent.role === role ? { ...agent, status: "stopping" } : agent)),
      );
      appendLog("info", `正在停止 ${role}…`);
      try {
        const agentId = agents.find((agent) => agent.role === role)?.agent_id;
        await stopRole(role, agentId);
        appendLog("info", `已停止 ${role}`);
        setAgentError(role, null);
        await refreshTasks();
      } catch (err) {
        const message = errorMessage(err);
        if (message.includes("agent offline")) {
          appendLog("info", `${role} 已离线，已同步状态`);
          setAgentError(role, null);
          try {
            applySnapshot(await getStatus());
          } catch {
            // 等下一次 SSE 覆盖
          }
          return;
        }
        appendLog("error", `停止 ${role} 失败：${message}`);
        pushToast(`停止 ${role} 失败：${message}`);
        try {
          applySnapshot(await getStatus());
        } catch {
          // 保留乐观状态，等待下一次 SSE 覆盖
        }
      } finally {
        setPendingRole(null);
      }
    },
    [agents, appendLog, applySnapshot, pushToast, refreshTasks, setAgentError],
  );

  const handleAgentSaved = useCallback(
    (role: string) => {
      void selectAgentSession(role);
      void refreshAll();
    },
    [refreshAll, selectAgentSession],
  );

  const handleDeleteAgent = useCallback(
    async (role: string) => {
      const workspaceId = activeWorkspace?.id;
      if (!workspaceId) return;
      if (
        !window.confirm(
          `移除角色 ${role}？将删除列表记录、PTY 映射与 pending 锁；历史 JSONL 保留，重建为全新会话。`,
        )
      ) {
        return;
      }
      try {
        await deleteWorkspaceAgent(workspaceId, role);
        // 立刻从本地数组抹掉，不等下一次全量快照；后端还会补发 agent_removed。
        setAgents((prev) => prev.filter((agent) => agent.role !== role));
        setPoolTerminals((prev) => prev.filter((terminal) => terminal.role !== role));
        setAgentErrors((prev) => {
          if (!(role in prev)) return prev;
          const next = { ...prev };
          delete next[role];
          return next;
        });
        if (selectedRole === role) setSelectedRole(null);
        if (
          poolTerminals.some(
            (terminal) =>
              terminal.kind === "agent" &&
              terminal.role === role &&
              terminal.terminal_id === resolvedTerminalId,
          )
        ) {
          setActiveTerminalId("1");
        }
        await refreshAll();
        await refreshPool();
      } catch (err) {
        const message = errorMessage(err);
        pushToast(`删除 ${role} 失败：${message}`);
        appendLog("warn", `删除 ${role} 失败：${message}`);
      }
    },
    [
      activeWorkspace?.id,
      appendLog,
      poolTerminals,
      pushToast,
      refreshAll,
      refreshPool,
      resolvedTerminalId,
      selectedRole,
      setAgentError,
    ],
  );

  const handleDiscover = useCallback(async () => {
    setDiscovering(true);
    try {
      const result = await discoverAgents();
      const found = Array.isArray(result.agents) ? result.agents : [];
      setAgents(found);
      const wildProcesses = Array.isArray(result.wild) ? result.wild : [];
      setWild(wildProcesses);
      const online = found.filter((agent) => agent.alive).length;
      const recovered = result.recovered ?? 0;
      const pruned = result.pruned ?? 0;
      appendLog(
        "info",
        `检测 Agent：${found.length} 个，在线 ${online} 个；缓存恢复 ${recovered}，清理 ${pruned}，未注册进程 ${wildProcesses.length}`,
      );
      setGlobalError(null);
    } catch (err) {
      const message = errorMessage(err);
      appendLog("warn", `检测 Agent 失败：${message}`);
      pushToast(`检测 Agent 失败：${message}`);
    } finally {
      setDiscovering(false);
    }
  }, [appendLog, pushToast]);

  const handleCleanupWild = useCallback(
    async (process: WildProcess) => {
      if (process.pid === undefined || process.create_time === undefined) return;
      setCleaningPid(process.pid);
      try {
        const result = await cleanupWildProcess(process.pid, process.create_time);
        const killed = Array.isArray(result.killed) ? result.killed : [];
        appendLog("info", `强制清理 pid=${process.pid}：已结束 ${killed.join(", ") || "0"} 个进程`);
        await handleDiscover();
      } catch (err) {
        const message = errorMessage(err);
        appendLog("warn", `清理 pid=${process.pid} 失败：${message}`);
        pushToast(`清理 pid=${process.pid} 失败：${message}`);
      } finally {
        setCleaningPid(null);
      }
    },
    [appendLog, handleDiscover, pushToast],
  );

  const onTasksResizeStart = (event: ReactPointerEvent<HTMLDivElement>) => {
    taskDrag.current = { startX: event.clientX, startWidth: tasksWidth };
    event.currentTarget.setPointerCapture(event.pointerId);
  };
  const onTasksResizeMove = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (!taskDrag.current) return;
    const delta = taskDrag.current.startX - event.clientX;
    setTasksWidth(Math.min(400, Math.max(300, taskDrag.current.startWidth + delta)));
  };
  const onTasksResizeEnd = (event: ReactPointerEvent<HTMLDivElement>) => {
    taskDrag.current = null;
    event.currentTarget.releasePointerCapture(event.pointerId);
  };

  const onConsoleResizeStart = (event: ReactPointerEvent<HTMLDivElement>) => {
    consoleDrag.current = { startY: event.clientY, startHeight: consoleHeight };
    event.currentTarget.setPointerCapture(event.pointerId);
  };
  const onConsoleResizeMove = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (!consoleDrag.current) return;
    const delta = consoleDrag.current.startY - event.clientY;
    const max = Math.max(220, window.innerHeight * 0.6);
    setConsoleHeight(Math.min(max, Math.max(140, consoleDrag.current.startHeight + delta)));
  };
  const onConsoleResizeEnd = (event: ReactPointerEvent<HTMLDivElement>) => {
    consoleDrag.current = null;
    event.currentTarget.releasePointerCapture(event.pointerId);
  };

  const agentsPane = (
    <AgentsPanel
      agents={agents}
      terminals={poolTerminals}
      agentErrors={agentErrors}
      poolEnabled={terminalPoolEnabled}
      poolLimit={poolLimit}
      activeTerminalId={resolvedTerminalId}
      activeRole={activeRole}
      daemonOnline={connection === "open"}
      pendingRole={pendingRole}
      onStop={(role) => {
        void handleStop(role);
      }}
      selectedRole={activeRole}
      onSelectRole={(role) => {
        void selectAgentSession(role);
      }}
      onSelectTerminal={(terminalId) => {
        void focusTerminal(terminalId);
      }}
      onCreateShell={() => {
        void handleCreateShell();
      }}
      onRemoveTerminal={(terminalId) => {
        void handleRemoveTerminal(terminalId);
      }}
      onNewAgent={() => setAgentSheet({ role: null })}
      onEditAgent={(role) => setAgentSheet({ role })}
      onDeleteAgent={(role) => {
        void handleDeleteAgent(role);
      }}
      wild={wild}
      wildOpen={wildOpen}
      onToggleWild={setWildOpen}
      discovering={discovering}
      onDiscover={() => {
        void handleDiscover();
      }}
      onCleanupWild={handleCleanupWild}
      cleaningPid={cleaningPid}
    />
  );
  const tasksPane = <TasksPanel tasks={tasks} onSelect={setSelectedTask} />;

  const contextWarning =
    ptyChat.contextTokens > 0
      ? `PTY 上下文约 ${formatTokenCount(ptyChat.contextTokens)} tokens；继续对话按输入计费（缓存命中亦有成本）`
      : null;

  return (
    <div className="flex h-screen flex-col bg-zinc-950 text-zinc-100">
      <TopBar
        connection={connection}
        onOpenCommand={() => setPaletteOpen(true)}
        showAgentsToggle={narrowAgents}
        onToggleAgents={() => setAgentsDrawer((value) => !value)}
        showTasksToggle={narrowTasks}
        onToggleTasks={() => setTasksDrawer((value) => !value)}
        workspaceName={activeWorkspace?.name}
        onOpenWorkspaces={() => setWorkspaceDialogOpen(true)}
      />
      {globalError ? (
        <div className="flex items-center justify-between gap-3 border-b border-red-500/30 bg-red-500/10 px-5 py-1.5 text-[11px] text-red-300">
          <span className="truncate">工作台不可用：{globalError}</span>
          <button
            type="button"
            aria-label="关闭全局错误提示"
            className="shrink-0 text-red-300/60 transition-colors hover:text-red-200"
            onClick={() => setGlobalError(null)}
          >
            ×
          </button>
        </div>
      ) : null}

      <div className="flex min-h-0 flex-1">
        {narrowAgents ? null : (
          <aside className="w-60 shrink-0 border-r border-zinc-800 bg-zinc-950">
            {agentsPane}
          </aside>
        )}

        <main className="flex min-w-0 flex-1 flex-col">
          <CenterTabBar
            tabs={visibleTabs}
            active={effectiveCenterTab}
            onSelect={(key) => setCenterTab(key)}
          />
          <div className="relative min-h-0 flex-1">
            {/* 对话面板常驻：切 Tab 不卸载，避免重新挂载/重放历史造成卡顿与误判重连。 */}
            {chatEnabled ? (
              <div
                aria-hidden={effectiveCenterTab !== "chat"}
                inert={effectiveCenterTab !== "chat"}
                className={
                  effectiveCenterTab === "chat"
                    ? "h-full min-h-0"
                    : "pointer-events-none invisible absolute inset-0"
                }
              >
                <Suspense
                  fallback={
                    <div className="flex h-full items-center justify-center text-xs text-zinc-600">
                      加载对话面板…
                    </div>
                  }
                >
                  <ChatPanel
                    key={`${resolvedTerminalId}:${workspaceEpoch}`}
                    entries={ptyChat.entries}
                    busy={ptyChat.busy}
                    sessionId={ptyChat.sessionId}
                    contextWarning={contextWarning}
                    contextTokens={ptyChat.contextTokens}
                    contextBreakdown={ptyChat.contextBreakdown}
                    usageDelta={ptyChat.usageDelta}
                    usageDeltaSeq={ptyChat.usageDeltaSeq}
                    usage={ptyChat.usage}
                    budgetTokens={effectiveBudget}
                    notice={ptyChat.notice}
                    handoff={activeHandoff}
                    error={activeRole ? (agentErrors[activeRole] ?? null) : null}
                    onDismissError={() => setAgentError(activeRole, null)}
                    locked={!selectedOnline}
                    lockReason={chatLockReason}
                    onSend={(prompt) => {
                      // 发新消息前清掉上一轮局部错误。
                      setAgentError(activeRole, null);
                      setTerminalError(resolvedTerminalId, null);
                      void ptyChat.send(prompt);
                    }}
                    onStop={() => {
                      void ptyChat.stop();
                    }}
                    onHandoff={() => {
                      void handleHandoff();
                    }}
                    onBudgetChange={(tokens) => {
                      void handleBudgetChange(tokens);
                    }}
                    onOpenTerminal={() => setCenterTab("terminal")}
                  />
                </Suspense>
              </div>
            ) : null}
            {effectiveCenterTab === "progress" ? (
              <ProgressPanel
                role={selectedOnline && ptyChat.sessionId ? activeRole : null}
                entries={ptyChat.entries}
              />
            ) : null}
            {/* 终端常驻：切 Tab 只隐藏不卸载，避免重连 /pty/stream 回放截断的 backlog。 */}
            <div
              aria-hidden={effectiveCenterTab !== "terminal"}
              inert={effectiveCenterTab !== "terminal"}
              className={
                effectiveCenterTab === "terminal"
                  ? "h-full min-h-0"
                  : "pointer-events-none invisible absolute inset-0"
              }
            >
              <Suspense
                fallback={
                  <div className="flex h-full items-center justify-center text-xs text-zinc-600">
                    加载终端…
                  </div>
                }
              >
                <TerminalPanel
                  key={`${activeWorkspace?.id ?? "none"}:${resolvedTerminalId}:${workspaceEpoch}`}
                  role={activeRole}
                  agentId={selectedAgentId}
                  terminalId={resolvedTerminalId}
                  allowUnbound={shellTerminal}
                  onConvert={
                    shellTerminal
                      ? (role) => {
                          void handleConvertTerminal(resolvedTerminalId, role);
                        }
                      : undefined
                  }
                  convertBusy={poolBusy}
                  workspaceName={activeWorkspace?.name ?? null}
                  error={activeRole ? (agentErrors[activeRole] ?? null) : (terminalErrors[resolvedTerminalId] ?? null)}
                  onDismissError={() => {
                    setAgentError(activeRole, null);
                    setTerminalError(resolvedTerminalId, null);
                  }}
                  locked={terminalLocked}
                  lockReason={terminalLockReason}
                  active={effectiveCenterTab === "terminal"}
                />
              </Suspense>
            </div>
          </div>
        </main>

        {narrowTasks ? null : (
          <>
            <div
              role="separator"
              aria-orientation="vertical"
              className="w-1 shrink-0 cursor-col-resize bg-zinc-800/60 transition-colors hover:bg-sky-700/60"
              onPointerDown={onTasksResizeStart}
              onPointerMove={onTasksResizeMove}
              onPointerUp={onTasksResizeEnd}
              onPointerCancel={onTasksResizeEnd}
            />
            <aside
              style={{ width: tasksWidth }}
              className="shrink-0 border-l border-zinc-800 bg-zinc-950"
            >
              {tasksPane}
            </aside>
          </>
        )}
      </div>

      <div className="shrink-0 border-t border-zinc-800 bg-zinc-900/60">
        {consoleOpen ? (
          <div style={{ height: consoleHeight }} className="relative flex min-h-0 flex-col">
            <div
              role="separator"
              aria-orientation="horizontal"
              className="absolute -top-1 z-10 h-2 w-full cursor-row-resize"
              onPointerDown={onConsoleResizeStart}
              onPointerMove={onConsoleResizeMove}
              onPointerUp={onConsoleResizeEnd}
              onPointerCancel={onConsoleResizeEnd}
            />
            <ConsolePanel
              logs={logs}
              onClear={() => setLogs([])}
              onCollapse={() => setConsoleOpen(false)}
            />
          </div>
        ) : (
          <button
            type="button"
            className="flex h-8 w-full items-center gap-2 px-3 text-[11px] text-zinc-500 transition-colors hover:bg-zinc-800/40"
            onClick={() => setConsoleOpen(true)}
          >
            <Terminal className="size-3.5" />
            Console
            <span className="font-mono text-zinc-600">{logs.length}</span>
            <ChevronUp className="ml-auto size-3.5" />
          </button>
        )}
      </div>

      {narrowAgents && agentsDrawer ? (
        <div className="fixed inset-0 z-40">
          <button
            type="button"
            aria-label="关闭 Agents"
            className="absolute inset-0 bg-black/50"
            onClick={() => setAgentsDrawer(false)}
          />
          <aside className="absolute inset-y-0 left-0 z-10 w-64 border-r border-zinc-800 bg-zinc-950 shadow-2xl">
            {agentsPane}
          </aside>
        </div>
      ) : null}

      {narrowTasks && tasksDrawer ? (
        <div className="fixed inset-0 z-40">
          <button
            type="button"
            aria-label="关闭 Tasks"
            className="absolute inset-0 bg-black/50"
            onClick={() => setTasksDrawer(false)}
          />
          <aside className="absolute inset-y-0 right-0 z-10 w-[340px] border-l border-zinc-800 bg-zinc-950 shadow-2xl">
            {tasksPane}
          </aside>
        </div>
      ) : null}

      <CommandPalette
        open={paletteOpen}
        onOpenChange={setPaletteOpen}
        onRefresh={() => {
          void refreshAll();
        }}
        onReconnect={reconnectStreams}
        onClearLogs={() => setLogs([])}
      />
      <TaskDetailsDialog
        task={selectedTask}
        onOpenChange={(open) => {
          if (!open) setSelectedTask(null);
        }}
      />
      {workspaceDialogOpen ? (
        <WorkspaceDialog
          open={workspaceDialogOpen}
          onOpenChange={setWorkspaceDialogOpen}
          workspaces={workspaces}
          engines={engines}
          activeId={activeWorkspace?.id ?? null}
          onRefresh={handleWorkspacesChanged}
        />
      ) : null}
      <AgentConfigSheet
        open={agentSheet !== null}
        onOpenChange={(open) => {
          if (!open) setAgentSheet(null);
        }}
        workspaceId={activeWorkspace?.id ?? null}
        role={agentSheet?.role ?? null}
        onSaved={handleAgentSaved}
      />
      <ToastViewport toasts={toasts} onDismiss={dismissToast} />
    </div>
  );
}
