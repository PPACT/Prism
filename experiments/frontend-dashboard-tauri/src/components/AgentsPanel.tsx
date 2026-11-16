import { Bot, Plus, RefreshCw, SquareTerminal } from "lucide-react";
import { useState } from "react";

import { AgentTable } from "@/components/AgentTable";
import { SessionList } from "@/components/SessionList";
import { WildTable } from "@/components/WildTable";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import type { Agent, PoolTerminal, WildProcess } from "@/lib/types";

interface AgentsPanelProps {
  agents: Agent[];
  terminals: PoolTerminal[];
  /** 角色级局部错误：仅在对应会话行标红，不进全局红条。 */
  agentErrors?: Record<string, string>;
  poolEnabled: boolean;
  poolLimit: number;
  activeTerminalId: string;
  activeRole: string | null;
  daemonOnline?: boolean;
  pendingRole: string | null;
  onStop: (role: string) => void;
  selectedRole: string | null;
  onSelectRole: (role: string) => void;
  onSelectTerminal: (terminalId: string) => void;
  onCreateShell: () => void;
  onRemoveTerminal: (terminalId: string) => void;
  onNewAgent: () => void;
  onEditAgent: (role: string) => void;
  onDeleteAgent: (role: string) => void;
  wild: WildProcess[];
  wildOpen: boolean;
  onToggleWild: (open: boolean) => void;
  discovering: boolean;
  onDiscover: () => void;
  onCleanupWild: (process: WildProcess) => void;
  cleaningPid: number | null;
}

/** 左侧唯一会话坞：Agent 与临时 Shell 并列；新建只在这里。 */
export function AgentsPanel({
  agents,
  terminals,
  agentErrors = {},
  poolEnabled,
  poolLimit,
  activeTerminalId,
  activeRole,
  daemonOnline = true,
  pendingRole,
  onStop,
  selectedRole,
  onSelectRole,
  onSelectTerminal,
  onCreateShell,
  onRemoveTerminal,
  onNewAgent,
  onEditAgent,
  onDeleteAgent,
  wild,
  wildOpen,
  onToggleWild,
  discovering,
  onDiscover,
  onCleanupWild,
  cleaningPid,
}: AgentsPanelProps) {
  const [menuOpen, setMenuOpen] = useState(false);
  const poolCount = terminals.filter((terminal) => terminal.kind !== "legacy").length;

  return (
    <section className="flex h-full min-h-0 min-w-0 flex-col px-2.5 pb-2.5 pt-3">
      <div className="mb-2 flex items-center justify-between gap-1 px-0.5">
        <div className="flex items-baseline gap-1.5">
          <h2 className="text-[10px] font-medium uppercase tracking-[0.14em] text-zinc-500">
            Agents
          </h2>
          {poolEnabled ? (
            <span className="font-mono text-[9px] text-zinc-600">
              {poolCount}/{poolLimit}
            </span>
          ) : null}
        </div>
        <div className="flex items-center gap-0.5">
          <button
            type="button"
            aria-label="检测 Agent"
            title="检测 Agent"
            disabled={discovering}
            onClick={onDiscover}
            className="rounded p-1 text-zinc-500 transition-colors hover:bg-zinc-900 hover:text-zinc-200 disabled:opacity-40"
          >
            <RefreshCw className={`size-3.5 ${discovering ? "animate-spin" : ""}`} />
          </button>
          <Popover open={menuOpen} onOpenChange={setMenuOpen}>
            <PopoverTrigger asChild>
              <button
                type="button"
                aria-label="新建"
                title="新建"
                className="rounded border border-zinc-800/80 p-1 text-zinc-400 transition-colors hover:border-zinc-700 hover:bg-zinc-900 hover:text-zinc-100 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-zinc-500/60"
              >
                <Plus className="size-3.5" />
              </button>
            </PopoverTrigger>
            <PopoverContent
              align="end"
              sideOffset={6}
              className="w-56 border-zinc-800/80 bg-zinc-950 p-1 text-zinc-200 shadow-xl"
            >
              <button
                type="button"
                className="flex w-full items-center gap-2 rounded px-2 py-1.5 text-left text-[12px] transition-colors hover:bg-zinc-900"
                onClick={() => {
                  setMenuOpen(false);
                  onNewAgent();
                }}
              >
                <Bot className="size-3.5 text-zinc-400" />
                新建 Agent
              </button>
              <button
                type="button"
                disabled={!poolEnabled}
                title={poolEnabled ? "新建纯 Shell 临时终端" : "需要 PTY 池（PRISM_TERMINAL_POOL=1）"}
                className="flex w-full items-center gap-2 rounded px-2 py-1.5 text-left text-[12px] transition-colors hover:bg-zinc-900 disabled:cursor-not-allowed disabled:text-zinc-600 disabled:hover:bg-transparent"
                onClick={() => {
                  if (!poolEnabled) return;
                  setMenuOpen(false);
                  onCreateShell();
                }}
              >
                <SquareTerminal className="size-3.5" />
                新建临时终端
              </button>
            </PopoverContent>
          </Popover>
        </div>
      </div>

      <div className="min-h-0 flex-1 overflow-auto">
        {poolEnabled ? (
          <SessionList
            agents={agents}
            terminals={terminals}
            errors={agentErrors}
            activeRole={activeRole}
            activeTerminalId={activeTerminalId}
            daemonOnline={daemonOnline}
            onSelectAgent={onSelectRole}
            onSelectTerminal={onSelectTerminal}
            onRemoveTerminal={onRemoveTerminal}
            onEditAgent={onEditAgent}
            onDeleteAgent={onDeleteAgent}
            onStop={onStop}
          />
        ) : (
          <AgentTable
            agents={agents}
            pendingRole={pendingRole}
            onStop={onStop}
            onSelect={onSelectRole}
            selectedRole={selectedRole}
            onEdit={onEditAgent}
            onDelete={onDeleteAgent}
          />
        )}
      </div>

      {wild.length > 0 ? (
        <details
          className="mt-2 rounded-md border border-zinc-900 bg-zinc-950/60"
          open={wildOpen}
          onToggle={(event) => onToggleWild(event.currentTarget.open)}
        >
          <summary className="cursor-pointer select-none px-2 py-1 text-[10px] font-medium text-amber-300/80">
            未注册进程（{wild.length}）
          </summary>
          <div className="max-h-36 overflow-auto border-t border-zinc-900">
            <WildTable processes={wild} onCleanup={onCleanupWild} busyPid={cleaningPid} />
          </div>
        </details>
      ) : null}
    </section>
  );
}
