import { FitAddon } from "@xterm/addon-fit";
import { WebglAddon } from "@xterm/addon-webgl";
import { Terminal } from "@xterm/xterm";
import "@xterm/xterm/css/xterm.css";
import { Lock, Settings2, TriangleAlert, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetFooter,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { ptyStreamUrl, resizePty, sendPtyInput, startPty, streamFetch } from "@/lib/backend";
import { tailFromChunks } from "@/lib/ansi";
import { readClipboardText } from "@/lib/clipboard";
import { errorMessage } from "@/lib/format";
import { consumePtyStream } from "@/lib/pty";
import type { PtyNotice } from "@/lib/types";

type PtyState = "connecting" | "running" | "exited" | "error";

/** 输出合批窗口：24ms 合并多个流帧，减少 xterm 渲染次数（实测降低 CPU）。 */
const WRITE_BATCH_MS = 24;
/** 限量回放：最多 200 行 / 20KB 纯文本，既不卡死也保留最近命令历史。 */
const TAIL_REPLAY_LINES = 200;
const TAIL_REPLAY_CHARS = 20 * 1024;

async function pasteInto(term: Terminal): Promise<void> {
  const text = await readClipboardText();
  if (!text) return;
  // term.paste 会按 DECSET 2004 自动包 bracketed paste，再经 onData 写入 PTY。
  term.paste(text);
  term.focus();
}

const STATE_LABEL: Record<PtyState, string> = {
  connecting: "连接中…",
  running: "运行中",
  exited: "已退出",
  error: "连接失败",
};

interface TerminalPanelProps {
  role?: string | null;
  agentId?: string | null;
  terminalId?: string;
  workspaceName?: string | null;
  locked?: boolean;
  lockReason?: string;
  /** 当前 Agent/终端的局部错误；只在面板内提示，不升级为全局红条。 */
  error?: string | null;
  onDismissError?: () => void;
  /** 纯 Shell 终端没有 role/agent，但仍然允许输入。 */
  allowUnbound?: boolean;
  /** Shell 会话的“转为 Agent”入口（只出现在右上角齿轮里）。 */
  onConvert?: (role: string) => void;
  convertBusy?: boolean;
  /** Tab 是否可见；隐藏时保持挂载与流连接，仅停渲染，避免重连回放 backlog。 */
  active?: boolean;
}

/** 原生 PTY 终端：xterm.js 渲染 ANSI；只允许写入当前选中且在线 Agent 的 PTY。 */
export function TerminalPanel({
  role = null,
  agentId = null,
  terminalId = "1",
  workspaceName = null,
  locked = false,
  lockReason = "当前 Agent 已离线，终端已锁定",
  error = null,
  onDismissError = () => undefined,
  allowUnbound = false,
  onConvert,
  convertBusy = false,
  active = true,
}: TerminalPanelProps) {
  const [convertOpen, setConvertOpen] = useState(false);
  const [convertRole, setConvertRole] = useState("reviewer");
  const containerRef = useRef<HTMLDivElement | null>(null);
  const termRef = useRef<Terminal | null>(null);
  /** 上一次成功发送给后端的尺寸；相同尺寸不重复发 resize（反复 resize 会让 TUI 全量重绘）。 */
  const lastSizeRef = useRef<{ cols: number; rows: number } | null>(null);
  const syncSizeRef = useRef<(force?: boolean) => void>(() => undefined);
  const [state, setState] = useState<PtyState>("connecting");
  const [info, setInfo] = useState<{ command: string; sessionId: string } | null>(null);
  const [notice, setNotice] = useState<PtyNotice | null>(null);
  const [menu, setMenu] = useState<{ x: number; y: number } | null>(null);

  useEffect(() => {
    if (!notice) return;
    const timer = setTimeout(() => setNotice(null), 10_000);
    return () => clearTimeout(timer);
  }, [notice]);

  useEffect(() => {
    if (!menu) return;
    const close = () => setMenu(null);
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setMenu(null);
    };
    window.addEventListener("mousedown", close);
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("mousedown", close);
      window.removeEventListener("keydown", onKey);
    };
  }, [menu]);

  const pasteFromMenu = () => {
    const term = termRef.current;
    setMenu(null);
    if (term) void pasteInto(term);
  };

  useEffect(() => {
    if (locked) return;
    if (!allowUnbound && (!role || !agentId)) return;
    const container = containerRef.current;
    if (!container) return;
    const term = new Terminal({
      fontFamily: '"JetBrains Mono", ui-monospace, SFMono-Regular, monospace',
      fontSize: 12.5,
      lineHeight: 1.2,
      cursorBlink: false,
      /** 只保留最近 1000 行，避免 TUI 长输出把内存与滚动条拖垮。 */
      scrollback: 1000,
      convertEol: false,
      theme: {
        background: "#09090b",
        foreground: "#e4e4e7",
        cursor: "#71717a",
        selectionBackground: "#1e3a5f",
        black: "#18181b",
        brightBlack: "#52525b",
      },
    });
    const fit = new FitAddon();
    term.loadAddon(fit);
    term.open(container);
    termRef.current = term;
    if (import.meta.env.DEV) {
      (window as unknown as { __prismTerm?: Terminal }).__prismTerm = term;
    }
    try {
      fit.fit();
    } catch {
      // 容器尚未布局完成，ResizeObserver 会再次 fit
    }

    // WebGL 加速：加载失败或上下文丢失时回退默认渲染器，不允许黑屏。
    let webgl: WebglAddon | null = null;
    try {
      webgl = new WebglAddon();
      webgl.onContextLoss(() => {
        try {
          webgl?.dispose();
        } catch {
          // 已释放
        }
        webgl = null;
        try {
          term.refresh(0, Math.max(0, term.rows - 1));
        } catch {
          // 终端已销毁
        }
      });
      term.loadAddon(webgl);
    } catch {
      webgl = null;
    }

    const controller = new AbortController();
    // Ctrl/⌘+V、Shift+Insert：接管剪贴板，避免 WebView2 默认粘贴失效。
    term.attachCustomKeyEventHandler((event) => {
      if (event.type !== "keydown") return true;
      const pasteCombo =
        (event.ctrlKey || event.metaKey) &&
        !event.altKey &&
        !event.shiftKey &&
        event.key.toLowerCase() === "v";
      const shiftInsert =
        event.shiftKey &&
        !event.ctrlKey &&
        !event.metaKey &&
        !event.altKey &&
        event.key === "Insert";
      if (!pasteCombo && !shiftInsert) return true;
      event.preventDefault();
      void pasteInto(term);
      return false;
    });
    const dataDisposable = term.onData((data) => {
      void sendPtyInput(data, role, agentId, terminalId).catch(() => undefined);
    });

    let pendingChunks: Uint8Array[] = [];
    let flushTimer: ReturnType<typeof setTimeout> | null = null;
    const flush = () => {
      flushTimer = null;
      if (pendingChunks.length === 0) return;
      const chunks = pendingChunks;
      pendingChunks = [];
      const total = chunks.reduce((sum, chunk) => sum + chunk.length, 0);
      const merged = new Uint8Array(total);
      let offset = 0;
      for (const chunk of chunks) {
        merged.set(chunk, offset);
        offset += chunk.length;
      }
      term.write(merged);
    };
    const enqueue = (chunk: Uint8Array) => {
      pendingChunks.push(chunk);
      if (flushTimer === null) flushTimer = setTimeout(flush, WRITE_BATCH_MS);
    };
    const flushNow = () => {
      if (flushTimer !== null) {
        clearTimeout(flushTimer);
        flushTimer = null;
      }
      flush();
    };

    let resizeTimer: ReturnType<typeof setTimeout> | null = null;
    lastSizeRef.current = null;
    /** fit 后只在 cols/rows 真正变化时同步后端；force 用于附加终端后主动触发一次全量重绘。 */
    const syncSize = (force = false) => {
      if (container.clientWidth <= 0) return;
      try {
        fit.fit();
      } catch {
        return;
      }
      const cols = term.cols;
      const rows = term.rows;
      const last = lastSizeRef.current;
      if (!force && last && last.cols === cols && last.rows === rows) return;
      lastSizeRef.current = { cols, rows };
      void resizePty(cols, rows, role, agentId, terminalId, force)
        .then((status) => {
          if (status.cols !== cols || status.rows !== rows) {
            // 后端会钳制尺寸；记录偏差，避免下一次无意义重试。
            if (import.meta.env.DEV) {
              console.warn("pty resize clamped", { sent: { cols, rows }, actual: status });
            }
            lastSizeRef.current = { cols: status.cols, rows: status.rows };
          }
        })
        .catch(() => {
          lastSizeRef.current = null;
        });
    };
    syncSizeRef.current = syncSize;
    const applySize = () => {
      if (container.clientWidth <= 0) return;
      if (resizeTimer) clearTimeout(resizeTimer);
      resizeTimer = setTimeout(() => syncSize(false), 150);
    };
    const observer = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(applySize);
    observer?.observe(container);

    let scrollTimer: ReturnType<typeof setTimeout> | null = null;
    let replayTimer: ReturnType<typeof setTimeout> | null = null;
    const connect = async () => {
      try {        setState("connecting");
        // 附加/切换终端时先清空画布与滚动缓冲，随后只回放限量纯文本。
        term.reset();
        const isAgent = !allowUnbound;
        const status = await startPty(role, agentId, terminalId);
        setInfo({ command: status.command, sessionId: status.session_id });
        const response = await streamFetch(
          await ptyStreamUrl(role, agentId, terminalId, "tail"),
          {
            headers: { Accept: "text/event-stream" },
            signal: controller.signal,
          },
        );
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        setState("running");

        let replayRemaining = status.backlog_bytes ?? 0;
        let replayChunks: Uint8Array[] = [];
        let replayApplied = replayRemaining <= 0;

        const settleViewport = () => {
          if (scrollTimer) clearTimeout(scrollTimer);
          scrollTimer = setTimeout(() => {
            if (controller.signal.aborted) return;
            try {
              fit.fit();
            } catch {
              return;
            }
            term.scrollToBottom();
          }, 300);
        };
        const applyTail = (text: string) => {
          term.reset();
          term.write(text ? `${text.replace(/\n*$/, "")}\n` : "", () => {
            try {
              fit.fit();
            } catch {
              return;
            }
            term.scrollToBottom();
          });
          if (text) {
            setNotice({
              seq: 0,
              kind: "replay",
              message: "已回放最近 200 行 / 20KB 纯文本，可滚轮回看；更早输出不再保留",
            });
          }
          settleViewport();
        };
        const finishReplay = () => {
          if (replayApplied) return;
          replayApplied = true;
          if (replayTimer) {
            clearTimeout(replayTimer);
            replayTimer = null;
          }
          const text = tailFromChunks(replayChunks, TAIL_REPLAY_LINES, TAIL_REPLAY_CHARS);
          replayChunks = [];
          applyTail(text);
        };
        // 兜底：backlog 边界异常（被 LRU 截断/TUI 持续输出）时最多等 800ms，避免画面一直空白。
        if (!replayApplied) replayTimer = setTimeout(finishReplay, 800);
        const accept = (chunk: Uint8Array) => {
          if (replayApplied) {
            enqueue(chunk);
            return;
          }
          if (chunk.length <= replayRemaining) {
            replayChunks.push(chunk);
            replayRemaining -= chunk.length;
            if (replayRemaining === 0) finishReplay();
            return;
          }
          const take = replayRemaining;
          if (take > 0) replayChunks.push(chunk.subarray(0, take));
          replayRemaining = 0;
          finishReplay();
          const rest = chunk.subarray(take);
          if (rest.length > 0) enqueue(rest);
        };
        // Agent TUI：附加后主动 force resize，让 CLI 输出一帧干净的全量重绘。
        if (isAgent) {
          syncSize(true);
          settleViewport();
        }
        await consumePtyStream(response, {
          onData: accept,
          onReplay: (text) => {
            if (replayTimer) {
              clearTimeout(replayTimer);
              replayTimer = null;
            }
            replayApplied = true;
            replayChunks = [];
            applyTail(text);
          },
          onNotice: (next) => setNotice(next),
          onExit: (status) => {
            flushNow();
            setState("exited");
            term.write(`\r\n\x1b[33m[PTY exited${status === null ? "" : `: ${status}`}]\x1b[0m\r\n`);
          },
        });
        if (!controller.signal.aborted) {
          flushNow();
          setState("exited");
          term.write("\r\n\x1b[33m[PTY stream closed]\x1b[0m\r\n");
        }
      } catch (err) {
        if (controller.signal.aborted) return;
        setState("error");
        term.write(`\r\n\x1b[31m${errorMessage(err)}\x1b[0m\r\n`);
      }
    };
    void connect();

    return () => {
      controller.abort();
      if (resizeTimer) clearTimeout(resizeTimer);
      if (scrollTimer) clearTimeout(scrollTimer);
      if (replayTimer) clearTimeout(replayTimer);
      if (flushTimer !== null) clearTimeout(flushTimer);
      pendingChunks = [];
      observer?.disconnect();
      dataDisposable.dispose();
      syncSizeRef.current = () => undefined;
      lastSizeRef.current = null;
      termRef.current = null;
      if ((window as unknown as { __prismTerm?: Terminal }).__prismTerm === term) {
        delete (window as unknown as { __prismTerm?: Terminal }).__prismTerm;
      }
      term.dispose();
    };
  }, [role, agentId, terminalId, allowUnbound, locked]);

  // Tab 切回时重新 fit；终端全程保持挂载，不重连 /pty/stream。
  useEffect(() => {
    if (!active || locked) return;
    if (!allowUnbound && (!role || !agentId)) return;
    const timer = setTimeout(() => {
      syncSizeRef.current(false);
    }, 16);
    return () => clearTimeout(timer);
  }, [active, role, agentId, terminalId, allowUnbound, locked]);

  const displayState: PtyState =
    locked || (!allowUnbound && (!role || !agentId)) ? "exited" : state;
  const displayInfo = locked || (!allowUnbound && (!role || !agentId)) ? null : info;

  return (
    <section
      className="relative flex h-full min-h-0 min-w-0 flex-col bg-[#09090b]"
      onContextMenu={(event) => {
        event.preventDefault();
        setMenu({
          x: Math.min(event.clientX, window.innerWidth - 140),
          y: Math.min(event.clientY, window.innerHeight - 48),
        });
      }}
    >
      <div className="flex h-8 shrink-0 items-center gap-2 border-x border-b border-zinc-800 bg-zinc-950 px-3 text-[11px] text-zinc-500">
        <span
          className={`size-1.5 rounded-full ${
            locked
              ? "bg-zinc-500"
              : displayState === "running"
                ? "bg-emerald-400"
                : displayState === "error"
                  ? "bg-red-400"
                  : "bg-amber-400"
          }`}
        />
        <span className="text-zinc-300">Terminal</span>
        <span className="font-mono text-zinc-600">
          {workspaceName ? `${workspaceName} · ` : ""}
          {role ? `${role} · ` : ""}
          {displayInfo ? `${displayInfo.command} · PTY` : "claude · PTY"}
        </span>
        {displayInfo?.sessionId ? (
          <span
            className="truncate font-mono text-[10px] text-zinc-700"
            title={displayInfo.sessionId}
          >
            session {displayInfo.sessionId.slice(0, 8)}
          </span>
        ) : null}
        <span className="ml-auto font-mono text-zinc-600">
          {locked ? "已锁定" : STATE_LABEL[displayState]}
        </span>
        {onConvert ? (
          <button
            type="button"
            aria-label="终端设置"
            title="终端设置"
            onClick={() => setConvertOpen(true)}
            className="rounded p-0.5 text-zinc-600 transition-colors hover:bg-zinc-900 hover:text-zinc-300"
          >
            <Settings2 className="size-3.5" />
          </button>
        ) : null}
      </div>
      {error ? (
        <div className="flex shrink-0 items-center gap-2 border-b border-red-500/30 bg-red-500/10 px-3 py-1.5 text-[11px] text-red-300">
          <TriangleAlert className="size-3.5 shrink-0" />
          <span className="min-w-0 flex-1 truncate">{error}</span>
          <button
            type="button"
            aria-label="关闭错误提示"
            className="shrink-0 rounded p-0.5 text-red-300/60 transition-colors hover:text-red-100"
            onClick={onDismissError}
          >
            <X className="size-3.5" />
          </button>
        </div>
      ) : null}
      {onConvert ? (
        <Sheet open={convertOpen} onOpenChange={setConvertOpen}>
          <SheetContent className="w-80 border-zinc-800 bg-zinc-950 p-5">
            <SheetHeader>
              <SheetTitle className="text-sm text-zinc-100">终端设置</SheetTitle>
              <SheetDescription className="text-[11px] leading-5 text-zinc-500">
                把临时终端转换为 Agent。转换会停止当前 Shell，并在同一终端槽启动 CLI；若转换失败，终端标记为 failed，不会重开 Shell。
              </SheetDescription>
            </SheetHeader>
            <div className="mt-5 space-y-2">
              <label htmlFor="convert-role" className="text-[11px] text-zinc-400">
                Agent 角色
              </label>
              <Input
                id="convert-role"
                value={convertRole}
                placeholder="reviewer"
                onChange={(event) => setConvertRole(event.target.value)}
                className="h-9 border-white/10 bg-zinc-900/60 font-mono text-[12px] text-zinc-100"
              />
            </div>
            <SheetFooter className="mt-5">
              <Button
                size="sm"
                disabled={convertBusy || !convertRole.trim()}
                onClick={() => {
                  const role = convertRole.trim();
                  if (!role) return;
                  setConvertOpen(false);
                  onConvert(role);
                }}
              >
                转换为 Agent
              </Button>
            </SheetFooter>
          </SheetContent>
        </Sheet>
      ) : null}
      {notice ? (
        <div className="shrink-0 border-b border-amber-500/30 bg-amber-500/10 px-3 py-1.5 text-[11px] text-amber-200/90">
          {notice.message}
        </div>
      ) : null}
      <div ref={containerRef} className="min-h-0 flex-1 overflow-hidden px-2 py-1" />
      {menu ? (
        <div
          role="menu"
          className="fixed z-50 min-w-[120px] rounded-md border border-zinc-700 bg-zinc-900 p-1 shadow-xl"
          style={{ left: menu.x, top: menu.y }}
          onMouseDown={(event) => {
            event.preventDefault();
            event.stopPropagation();
          }}
          onContextMenu={(event) => event.preventDefault()}
        >
          <button
            type="button"
            role="menuitem"
            className="w-full rounded px-3 py-1.5 text-left text-[12px] text-zinc-200 hover:bg-zinc-800"
            onClick={pasteFromMenu}
          >
            粘贴
          </button>
        </div>
      ) : null}
      {locked ? (
        <div className="absolute inset-0 z-10 flex flex-col items-center justify-center gap-2 bg-zinc-950/85 backdrop-blur-sm">
          <Lock className="size-5 text-amber-400/80" />
          <span className="text-xs text-zinc-300">{lockReason}</span>
          <span className="font-mono text-[10px] text-zinc-600">
            {role ? `role=${role}` : "未选择 Agent"} · 输入已禁用
          </span>
        </div>
      ) : null}
    </section>
  );
}
