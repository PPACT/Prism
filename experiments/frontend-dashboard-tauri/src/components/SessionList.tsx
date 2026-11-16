import { Pencil, Square, Trash2, TriangleAlert, X } from "lucide-react";
import { useEffect, useState } from "react";

import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";
import { isAgentOnline } from "@/lib/agentStatus";
import { resolveSessionStatus, type SessionTone } from "@/lib/sessionStatus";
import type { Agent, PoolTerminal } from "@/lib/types";

interface SessionListProps {
  agents: Agent[];
  terminals: PoolTerminal[];
  /** 角色级局部错误：只标红对应行，不升级为全局红条。 */
  errors?: Record<string, string>;
  activeRole: string | null;
  activeTerminalId: string;
  daemonOnline?: boolean;
  onSelectAgent: (role: string) => void;
  onSelectTerminal: (terminalId: string) => void;
  onRemoveTerminal: (terminalId: string) => void;
  onEditAgent: (role: string) => void;
  onDeleteAgent: (role: string) => void;
  onStop: (role: string) => void;
}

interface Row {
  key: string;
  kind: "agent" | "shell";
  title: string;
  subtitle: string;
  initials: string;
  terminalId: string | null;
  role: string;
  agent: Agent | null;
  terminal: PoolTerminal | null;
}

const AVATAR_TONE = [
  "border-sky-500/25 bg-gradient-to-br from-sky-500/20 to-cyan-500/5 text-sky-200",
  "border-emerald-500/25 bg-gradient-to-br from-emerald-500/20 to-teal-500/5 text-emerald-200",
  "border-amber-500/25 bg-gradient-to-br from-amber-500/20 to-orange-500/5 text-amber-200",
  "border-rose-500/25 bg-gradient-to-br from-rose-500/20 to-pink-500/5 text-rose-200",
  "border-indigo-500/25 bg-gradient-to-br from-indigo-500/20 to-sky-500/5 text-indigo-200",
  "border-violet-500/25 bg-gradient-to-br from-violet-500/20 to-fuchsia-500/5 text-violet-200",
  "border-teal-500/25 bg-gradient-to-br from-teal-500/20 to-emerald-500/5 text-teal-200",
  "border-orange-500/25 bg-gradient-to-br from-orange-500/20 to-amber-500/5 text-orange-200",
];

function avatarTone(role: string): string {
  let hash = 0;
  for (const char of role) hash = (hash * 31 + char.charCodeAt(0)) % 997;
  return AVATAR_TONE[hash % AVATAR_TONE.length];
}

/** 带数字的角色提取数字做后缀：test-01 → T01、test-02 → T02，避免缩写撞车。 */
function initials(role: string): string {
  const trimmed = role.trim();
  if (!trimmed) return "?";
  const numberPart = trimmed.match(/(\d+)(?!.*\d)/)?.[1] ?? "";
  const letters = trimmed.match(/[A-Za-z]/g)?.join("") ?? "";
  if (numberPart) {
    const head = letters ? letters[0].toUpperCase() : "";
    return `${head}${numberPart}`.slice(0, 4).toUpperCase();
  }
  const parts = trimmed.split(/[-_\s]+/).filter(Boolean);
  if (parts.length >= 2) return parts.slice(0, 3).map((part) => part[0]).join("").toUpperCase();
  return trimmed.slice(0, 3).toUpperCase();
}

function agentSource(agent: Agent): string {
  if (agent.source === "external") return "外部注册";
  if (agent.job_id) return "中控托管";
  return "本地角色";
}

const TONE_DOT: Record<SessionTone, string> = {
  offline: "bg-zinc-500",
  online: "bg-emerald-500",
  running: "bg-blue-500 animate-pulse",
  waiting: "bg-amber-500",
  error: "bg-red-500",
};

const TONE_TEXT: Record<SessionTone, string> = {
  offline: "text-zinc-500",
  online: "text-zinc-400",
  running: "text-zinc-400",
  waiting: "text-zinc-400",
  error: "text-red-300/90",
};

function rowStatus(row: Row, now: number, daemonOnline: boolean) {
  if (row.terminal) {
    // 与 ChatPanel 同源：终端必须真在跑，且角色记录在线；任一 stopped/alive=false 都是灰点。
    const agentUp = row.agent ? isAgentOnline(row.agent) : true;
    return resolveSessionStatus({
      online: daemonOnline,
      processAlive: row.terminal.state === "running" && agentUp,
      terminalState: row.terminal.state,
      agentStatus: row.agent?.status,
      waiting: row.terminal.waiting,
      hasTask: row.terminal.has_task,
      pendingTools: row.terminal.pending_tools,
      activityState: row.terminal.activity_state,
      lastActivityAt: row.terminal.last_activity_at,
      now,
    });
  }
  if (row.agent) {
    return resolveSessionStatus({
      online: daemonOnline,
      processAlive: isAgentOnline(row.agent),
      agentStatus: row.agent.status,
      hasTask: row.agent.has_task ?? Boolean(row.agent.current_task_id),
      pendingTools: row.agent.pending_tools,
      activityState: row.agent.activity_state,
      lastActivityAt: row.agent.last_activity_at,
      now,
    });
  }
  return resolveSessionStatus({
    online: daemonOnline,
    processAlive: false,
    agentStatus: "stopped",
    now,
  });
}

/** 左侧唯一会话坞：Agent、纯 Shell 与未起终端的外部 Agent 并列。 */
export function SessionList({
  agents,
  terminals,
  errors = {},
  activeRole,
  activeTerminalId,
  daemonOnline = true,
  onSelectAgent,
  onSelectTerminal,
  onRemoveTerminal,
  onEditAgent,
  onDeleteAgent,
  onStop,
}: SessionListProps) {
  const [now, setNow] = useState(() => Date.now() / 1_000);

  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now() / 1_000), 1_000);
    return () => clearInterval(timer);
  }, []);

  const agentTerminals = terminals.filter((terminal) => terminal.kind === "agent");
  const terminalByRole = new Map(agentTerminals.map((terminal) => [terminal.role, terminal]));
  const shells = terminals
    .filter((terminal) => terminal.kind === "shell")
    .sort((a, b) => (a.created_at ?? 0) - (b.created_at ?? 0));

  const rows: Row[] = [];
  const seen = new Set<string>();
  for (const agent of agents) {
    const role = (agent.role ?? "").trim();
    if (!role || seen.has(role)) continue;
    seen.add(role);
    const terminal = terminalByRole.get(role) ?? null;
    rows.push({
      key: `agent:${role}`,
      kind: "agent",
      title: role,
      subtitle: terminal ? "池终端" : agentSource(agent),
      initials: initials(role) || "?",
      terminalId: terminal?.terminal_id ?? null,
      role,
      agent,
      terminal,
    });
  }
  for (const terminal of agentTerminals) {
    if (seen.has(terminal.role)) continue;
    seen.add(terminal.role);
    rows.push({
      key: `agent:${terminal.role}`,
      kind: "agent",
      title: terminal.role,
      subtitle: "池终端",
      initials: initials(terminal.role) || "?",
      terminalId: terminal.terminal_id,
      role: terminal.role,
      agent: null,
      terminal,
    });
  }
  shells.forEach((terminal, index) => {
    rows.push({
      key: `shell:${terminal.terminal_id}`,
      kind: "shell",
      title: `终端 ${index + 1}`,
      subtitle: "临时终端",
      initials: ">_",
      terminalId: terminal.terminal_id,
      role: "",
      agent: null,
      terminal,
    });
  });

  if (rows.length === 0) {
    return (
      <div className="flex h-full flex-col items-center justify-center gap-1 px-4 text-center">
        <p className="text-[12px] text-zinc-500">还没有会话</p>
        <p className="text-[11px] text-zinc-600">点右上角 + 新建 Agent 或临时终端</p>
      </div>
    );
  }

  return (
    <TooltipProvider delayDuration={300}>
      <ul className="flex flex-col gap-0.5 p-1">
        {rows.map((row) => {
          const selected =
            row.kind === "shell"
              ? row.terminalId === activeTerminalId
              : row.terminalId
                ? row.terminalId === activeTerminalId
                : row.role === activeRole;
          const status = rowStatus(row, now, daemonOnline);
          const rowError = row.role ? (errors[row.role] ?? null) : null;
          const offline = status.tone === "offline";
          // 在线只允许内部 Agent 下线；外部 Agent 在线时只读。物理删除仅离线可用。
          const internal =
            row.agent?.source === "local" ||
            row.terminal?.kind === "agent" ||
            row.terminal?.kind === "legacy";
          const open = () => {
            if (row.terminalId) onSelectTerminal(row.terminalId);
            else if (row.role) onSelectAgent(row.role);
          };
          return (
            <li key={row.key}>
              <div
                role="button"
                tabIndex={row.terminalId || row.role ? 0 : undefined}
                aria-current={selected ? "true" : undefined}
                onClick={open}
                onKeyDown={(event) => {
                  if (event.key === "Enter" || event.key === " ") {
                    event.preventDefault();
                    open();
                  }
                }}
                className={`group flex h-[46px] cursor-pointer items-center gap-2.5 rounded-md px-2 transition-colors ${
                  selected
                    ? "bg-zinc-800/60 ring-1 ring-inset ring-zinc-700/50"
                    : "hover:bg-zinc-900/80"
                }`}
              >
                {row.kind === "agent" ? (
                  <span
                    aria-hidden
                    className={`flex size-7 shrink-0 items-center justify-center rounded-md border text-[9px] font-semibold tracking-[0.06em] ${avatarTone(row.role)}`}
                  >
                    {row.initials}
                  </span>
                ) : (
                  <span
                    aria-hidden
                    className="flex size-7 shrink-0 items-center justify-center rounded-md border border-zinc-800 bg-zinc-900 font-mono text-[10px] text-zinc-400"
                  >
                    {row.initials}
                  </span>
                )}

                <span className="flex min-w-0 flex-1 flex-col leading-tight">
                  <Tooltip>
                    <TooltipTrigger asChild>
                      <span
                        className={`block max-w-full truncate text-[13px] font-medium ${
                          rowError ? "text-red-300" : "text-zinc-100"
                        }`}
                      >
                        {row.title}
                      </span>
                    </TooltipTrigger>
                    <TooltipContent
                      side="right"
                      sideOffset={6}
                      className="border-zinc-700 bg-zinc-800 px-2 py-1 text-[10px] text-zinc-100"
                    >
                      {row.title}
                    </TooltipContent>
                  </Tooltip>
                  <span className="truncate text-[10px] text-zinc-600">{row.subtitle}</span>
                </span>

                {rowError ? (
                  <Tooltip>
                    <TooltipTrigger asChild>
                      <TriangleAlert
                        aria-label={`${row.title} 有错误`}
                        className="size-3.5 shrink-0 text-red-400"
                      />
                    </TooltipTrigger>
                    <TooltipContent
                      side="right"
                      sideOffset={6}
                      className="max-w-[240px] border-red-500/40 bg-zinc-800 px-2 py-1 text-[10px] text-red-200"
                    >
                      {rowError}
                    </TooltipContent>
                  </Tooltip>
                ) : null}

                <Tooltip>
                  <TooltipTrigger asChild>
                    <span className="flex w-[54px] shrink-0 items-center justify-end gap-1.5">
                      <span
                        aria-hidden="true"
                        className={`size-2 shrink-0 rounded-full ${TONE_DOT[status.tone] ?? "bg-zinc-500"}`}
                      />
                      <span className={`text-[10px] ${TONE_TEXT[status.tone] ?? "text-zinc-400"}`}>
                        {status.label}
                      </span>
                    </span>
                  </TooltipTrigger>
                  <TooltipContent
                    side="right"
                    sideOffset={6}
                    className="border-zinc-700 bg-zinc-800 px-2 py-1 text-[10px] text-zinc-100"
                  >
                    {status.label}
                  </TooltipContent>
                </Tooltip>

                <span className="flex w-0 shrink-0 items-center justify-center gap-0.5 overflow-hidden opacity-0 transition-opacity duration-150 group-focus-within:w-auto group-focus-within:opacity-100 group-hover:w-auto group-hover:opacity-100">
                  {row.agent?.source === "local" && !row.terminalId ? (
                    <button
                      type="button"
                      aria-label={`编辑 ${row.role}`}
                      title="编辑角色"
                      className="rounded p-0.5 text-zinc-500 hover:text-zinc-200"
                      onClick={(event) => {
                        event.stopPropagation();
                        onEditAgent(row.role);
                      }}
                    >
                      <Pencil className="size-3" />
                    </button>
                  ) : null}
                  {offline && row.role ? (
                    <button
                      type="button"
                      aria-label={`彻底删除 ${row.role}`}
                      title="移除角色（物理删除；重建即新局）"
                      className="rounded p-0.5 text-zinc-500 hover:text-red-300"
                      onClick={(event) => {
                        event.stopPropagation();
                        onDeleteAgent(row.role);
                      }}
                    >
                      <Trash2 className="size-3" />
                    </button>
                  ) : null}
                  {row.agent && !row.terminalId && row.agent.job_id ? (
                    <button
                      type="button"
                      aria-label={`停止 ${row.role}`}
                      className="rounded p-0.5 text-zinc-500 hover:text-zinc-200"
                      onClick={(event) => {
                        event.stopPropagation();
                        onStop(row.role);
                      }}
                    >
                      <Square className="size-3" />
                    </button>
                  ) : null}
                  {row.terminalId && (row.kind === "shell" || (!offline && internal)) ? (
                    <button
                      type="button"
                      aria-label={`下线终端 ${row.title}`}
                      title={row.kind === "agent" ? "下线（保留角色记录）" : "关闭临时终端"}
                      className="rounded p-0.5 text-zinc-500 hover:text-red-300"
                      onClick={(event) => {
                        event.stopPropagation();
                        if (row.terminalId) onRemoveTerminal(row.terminalId);
                      }}
                    >
                      <X className="size-3" />
                    </button>
                  ) : null}
                </span>
              </div>
            </li>
          );
        })}
      </ul>
    </TooltipProvider>
  );
}
