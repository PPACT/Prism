import { SearchIcon } from "lucide-react";
import { useEffect, useMemo, useRef, useState, type KeyboardEvent } from "react";

import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";

interface CommandPaletteProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onRefresh: () => void;
  onReconnect: () => void;
  onClearLogs: () => void;
}

interface PaletteCommand {
  id: string;
  label: string;
  shortcut: string;
  run: () => void;
}

export function CommandPalette({
  open,
  onOpenChange,
  onRefresh,
  onReconnect,
  onClearLogs,
}: CommandPaletteProps) {
  const [query, setQuery] = useState("");
  const [activeIndex, setActiveIndex] = useState(0);
  const panelRef = useRef<HTMLDivElement | null>(null);
  const inputRef = useRef<HTMLInputElement | null>(null);

  const commands = useMemo<PaletteCommand[]>(
    () => [
      {
        id: "refresh",
        label: "刷新状态与任务",
        shortcut: "R",
        run: () => {
          onRefresh();
          onOpenChange(false);
        },
      },
      {
        id: "reconnect",
        label: "重新连接 SSE",
        shortcut: "S",
        run: () => {
          onReconnect();
          onOpenChange(false);
        },
      },
      {
        id: "clear-logs",
        label: "清空 Console 事件",
        shortcut: "C",
        run: () => {
          onClearLogs();
          onOpenChange(false);
        },
      },
      {
        id: "close",
        label: "关闭面板",
        shortcut: "Esc",
        run: () => onOpenChange(false),
      },
    ],
    [onClearLogs, onOpenChange, onReconnect, onRefresh],
  );

  const filtered = useMemo(() => {
    const text = query.trim().toLowerCase();
    return text
      ? commands.filter((command) => command.label.toLowerCase().includes(text))
      : commands;
  }, [commands, query]);
  // 过滤后索引可能越界；渲染期直接钳制，避免用 effect setState 触发级联渲染。
  const safeIndex = filtered.length === 0 ? 0 : Math.min(activeIndex, filtered.length - 1);

  useEffect(() => {
    if (!open) return;
    // 不自动聚焦搜索框：焦点给面板容器，单字母快捷键直接生效。
    const frame = requestAnimationFrame(() => panelRef.current?.focus());
    return () => cancelAnimationFrame(frame);
  }, [open]);

  const runActive = () => {
    const command = filtered[safeIndex];
    if (command) command.run();
  };

  const moveActive = (delta: number) => {
    if (filtered.length === 0) return;
    setActiveIndex((current) => {
      const base = Math.min(current, filtered.length - 1);
      return (base + delta + filtered.length) % filtered.length;
    });
  };

  const enterSearch = (initial?: string) => {
    if (initial !== undefined) setQuery(initial);
    setActiveIndex(0);
    inputRef.current?.focus();
  };

  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    // 搜索框内：只处理导航/执行，字母照常输入，绝不拦截成快捷键。
    if (event.target === inputRef.current) {
      if (event.key === "ArrowDown") {
        event.preventDefault();
        moveActive(1);
      } else if (event.key === "ArrowUp") {
        event.preventDefault();
        moveActive(-1);
      } else if (event.key === "Enter") {
        event.preventDefault();
        runActive();
      }
      return;
    }

    if (event.ctrlKey || event.metaKey || event.altKey) return;

    if (event.key === "ArrowDown") {
      event.preventDefault();
      moveActive(1);
      return;
    }
    if (event.key === "ArrowUp") {
      event.preventDefault();
      moveActive(-1);
      return;
    }
    if (event.key === "Enter") {
      event.preventDefault();
      runActive();
      return;
    }
    if (event.key === "Escape") {
      event.preventDefault();
      onOpenChange(false);
      return;
    }
    if (event.key === "/") {
      event.preventDefault();
      enterSearch();
      return;
    }

    if (event.key.length === 1) {
      const upper = event.key.toUpperCase();
      const command = commands.find((item) => item.shortcut.toUpperCase() === upper);
      if (command) {
        event.preventDefault();
        command.run();
        return;
      }
      // 未绑定快捷键的字母/数字：自动进入搜索并把该字符打进去。
      if (/[a-zA-Z0-9]/.test(event.key)) {
        event.preventDefault();
        enterSearch(event.key);
      }
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        className="top-1/3 max-w-lg translate-y-0 gap-0 overflow-hidden p-0"
        showCloseButton={false}
        onOpenAutoFocus={(event) => {
          // 默认聚焦输入框会让单字母快捷键失效；焦点交给面板容器。
          event.preventDefault();
          setQuery("");
          setActiveIndex(0);
          panelRef.current?.focus();
        }}
      >
        <DialogHeader className="sr-only">
          <DialogTitle>命令面板</DialogTitle>
          <DialogDescription>Prism 中控命令</DialogDescription>
        </DialogHeader>
        <div
          ref={panelRef}
          tabIndex={-1}
          role="listbox"
          aria-label="命令面板"
          onKeyDown={onKeyDown}
          className="flex flex-col overflow-hidden rounded-xl bg-popover p-1 text-popover-foreground outline-none"
        >
          <div className="p-1 pb-0">
            <div className="flex h-8 items-center gap-2 rounded-lg border border-input/30 bg-input/30 px-2">
              <SearchIcon className="size-4 shrink-0 opacity-50" />
              <input
                ref={inputRef}
                value={query}
                onChange={(event) => {
                  setQuery(event.target.value);
                  setActiveIndex(0);
                }}
                placeholder="输入命令或搜索…（/ 聚焦）"
                className="w-full bg-transparent text-sm outline-none placeholder:text-muted-foreground"
              />
            </div>
          </div>
          <div className="max-h-72 overflow-x-hidden overflow-y-auto p-1">
            {filtered.length === 0 ? (
              <div className="py-6 text-center text-sm">无匹配命令</div>
            ) : (
              <div role="group" aria-label="操作">
                {filtered.map((command, index) => {
                  const active = index === safeIndex;
                  return (
                    <div
                      key={command.id}
                      role="option"
                      aria-selected={active}
                      data-active={active}
                      onMouseEnter={() => setActiveIndex(index)}
                      onClick={() => command.run()}
                      className={`flex cursor-pointer select-none items-center gap-2 rounded-sm px-2 py-1.5 text-sm ${
                        active ? "bg-muted text-foreground" : ""
                      }`}
                    >
                      {command.label}
                      <span className="ml-auto text-xs tracking-widest text-muted-foreground">
                        {command.shortcut}
                      </span>
                    </div>
                  );
                })}
              </div>
            )}
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}
