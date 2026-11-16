import { FolderCog, ListTodo, PanelLeft } from "lucide-react";

import { Button } from "@/components/ui/button";
import { connectionLabel } from "@/lib/format";
import type { ConnectionState } from "@/lib/types";

interface TopBarProps {
  connection: ConnectionState;
  onOpenCommand: () => void;
  showAgentsToggle?: boolean;
  onToggleAgents?: () => void;
  showTasksToggle?: boolean;
  onToggleTasks?: () => void;
  workspaceName?: string;
  onOpenWorkspaces?: () => void;
}

export function TopBar({
  connection,
  onOpenCommand,
  showAgentsToggle = false,
  onToggleAgents,
  showTasksToggle = false,
  onToggleTasks,
  workspaceName,
  onOpenWorkspaces,
}: TopBarProps) {
  const open = connection === "open";
  return (
    <header className="flex items-center justify-between gap-3 border-b border-zinc-800 bg-zinc-900/70 px-4 py-2.5">
      <div className="flex min-w-0 items-center gap-3">
        <h1 className="shrink-0 text-[13px] font-medium tracking-tight text-zinc-300">
          Prism
        </h1>
        {workspaceName ? (
          <button
            type="button"
            className="hidden max-w-[220px] items-center gap-1.5 truncate rounded border border-zinc-800 bg-zinc-900/60 px-2 py-0.5 text-[11px] text-zinc-300 transition-colors hover:bg-zinc-800/70 sm:flex"
            title="当前工作区"
            onClick={onOpenWorkspaces}
          >
            <FolderCog className="size-3 text-zinc-500" />
            {workspaceName}
          </button>
        ) : null}
      </div>
      <div className="flex shrink-0 items-center gap-2">
        {showAgentsToggle ? (
          <Button
            aria-label="Agents"
            variant="outline"
            size="sm"
            className="h-7 px-2"
            title="Agents"
            onClick={onToggleAgents}
          >
            <PanelLeft className="size-3.5" />
          </Button>
        ) : null}
        {showTasksToggle ? (
          <Button
            aria-label="Tasks"
            variant="outline"
            size="sm"
            className="h-7 px-2"
            title="Tasks"
            onClick={onToggleTasks}
          >
            <ListTodo className="size-3.5" />
          </Button>
        ) : null}
        <span
          className={`inline-flex items-center gap-2 rounded-full border px-2.5 py-0.5 text-[11px] ${
            open
              ? "border-emerald-500/40 text-emerald-400"
              : "border-amber-500/40 text-amber-400"
          }`}
        >
          <span className={`size-1.5 rounded-full ${open ? "bg-emerald-400" : "bg-amber-400"}`} />
          {connectionLabel(connection)}
        </span>
        <Button
          variant="outline"
          size="sm"
          className="h-7 font-mono text-[11px]"
          onClick={onOpenCommand}
        >
          Ctrl+K
        </Button>
      </div>
    </header>
  );
}
