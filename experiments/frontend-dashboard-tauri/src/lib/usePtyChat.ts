import { useCallback, useEffect, useRef, useState } from "react";

import {
  ACTIVE_TERMINAL_ID,
  getPtyHistory,
  ptyChatStreamUrl,
  sendPtyInput,
  startPty,
  streamFetch,
} from "./backend";
import type { ChatEntry } from "./chat";
import {
  breakdownFromBase,
  computeContextBreakdown,
  contextBaseParts,
  estimateTokens,
  requestTokens,
  type ContextBreakdown,
} from "./context";
import { errorMessage } from "./format";
import { consumePtyChatStream, type PtyChatMessage } from "./pty";
import type { PtyNotice, TokenUsage } from "./types";

const EMPTY_USAGE: TokenUsage = {
  input_tokens: 0,
  output_tokens: 0,
  cache_read_input_tokens: 0,
  cache_creation_input_tokens: 0,
};

function mergeUsage(total: TokenUsage, delta: TokenUsage): TokenUsage {
  return {
    input_tokens: total.input_tokens + delta.input_tokens,
    output_tokens: total.output_tokens + delta.output_tokens,
    cache_read_input_tokens: total.cache_read_input_tokens + delta.cache_read_input_tokens,
    cache_creation_input_tokens:
      total.cache_creation_input_tokens + delta.cache_creation_input_tokens,
  };
}

export interface PtyChatState {
  entries: ChatEntry[];
  busy: boolean;
  contextTokens: number;
  contextBreakdown: ContextBreakdown | null;
  usageDelta: number;
  usageDeltaSeq: number;
  usage: TokenUsage;
  budgetTokens: number;
  notice: PtyNotice | null;
  sessionId: string | null;
  error: string | null;
  send: (prompt: string) => Promise<void>;
  stop: () => Promise<void>;
}

const BUSY_TIMEOUT_MS = 180_000;

/**
 * 对话页唯一数据源：JSONL 旁路。输入直接写入同一 PTY 的 stdin；
 * 终端/对话共享一个绑定当前选中 Agent 的 claude 会话，消息从本地 transcript 增量读取。
 * role 为空或 Agent 不在线时不上线、不启动 PTY。
 */
export function usePtyChat(
  role: string | null,
  agentId: string | null,
  enabled: boolean,
  generation = 0,
  terminalId: string = ACTIVE_TERMINAL_ID,
  onError?: (message: string) => void,
): PtyChatState {
  const [entries, setEntries] = useState<ChatEntry[]>([]);
  const [busy, setBusy] = useState(false);
  const [contextTokens, setContextTokens] = useState(0);
  const [contextBreakdown, setContextBreakdown] = useState<ContextBreakdown | null>(null);
  const [usageDelta, setUsageDelta] = useState(0);
  const [usageDeltaSeq, setUsageDeltaSeq] = useState(0);
  const [usage, setUsage] = useState<TokenUsage>(EMPTY_USAGE);
  const [budgetTokens, setBudgetTokens] = useState(0);
  const [notice, setNotice] = useState<PtyNotice | null>(null);
  const [epoch, setEpoch] = useState(0);
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const onErrorRef = useRef(onError);
  useEffect(() => {
    onErrorRef.current = onError;
  }, [onError]);
  const nextId = useRef(0);
  const busyTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const deltaTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const deltaAccumRef = useRef(0);
  const lastContextRef = useRef(0);
  const baseTokensRef = useRef(0);
  const firstUserTokensRef = useRef(-1);
  const lastBindRef = useRef<string | null>(null);

  const toEntries = useCallback((messages: PtyChatMessage[]): ChatEntry[] => {
    const items: ChatEntry[] = [];
    for (const message of messages) {
      for (const block of message.blocks) {
        // transcript 里 user 的文本块也是 kind=text，这里恢复成用户气泡。
        const normalized =
          message.role === "user" && block.kind === "text"
            ? ({ kind: "user", text: block.text } as const)
            : block;
        items.push({ id: nextId.current++, block: normalized, timestamp: message.timestamp ?? null });
      }
    }
    return items;
  }, []);

  /** 记录首条用户消息与首条 assistant usage，供对话中途进入时也能算出分类。 */
  const rememberContextParts = useCallback((messages: PtyChatMessage[]) => {
    if (firstUserTokensRef.current < 0) {
      const firstUser = messages.find((message) => message.role === "user");
      if (firstUser) {
        firstUserTokensRef.current = firstUser.blocks.reduce(
          (sum, block) => sum + (block.kind === "text" ? estimateTokens(block.text) : 0),
          0,
        );
      }
    }
    if (baseTokensRef.current <= 0) {
      const firstAssistant = messages.find(
        (message) => message.role === "assistant" && requestTokens(message.usage) > 0,
      );
      if (firstAssistant) baseTokensRef.current = requestTokens(firstAssistant.usage);
    }
  }, []);

  const syncBreakdown = useCallback((tokens: number) => {
    if (baseTokensRef.current <= 0 || tokens <= 0) return;
    setContextBreakdown(
      breakdownFromBase(
        baseTokensRef.current,
        Math.max(0, firstUserTokensRef.current),
        tokens,
      ),
    );
  }, []);

  const appendMessages = useCallback(
    (messages: PtyChatMessage[]) => {
      rememberContextParts(messages);
      syncBreakdown(lastContextRef.current);
      const items = toEntries(messages);
      if (items.length > 0) setEntries((previous) => [...previous, ...items]);
      const hasText = messages.some(
        (message) =>
          message.role === "assistant" && message.blocks.some((block) => block.kind === "text"),
      );
      if (hasText) {
        setBusy(false);
        if (busyTimer.current) clearTimeout(busyTimer.current);
      }
    },
    [rememberContextParts, syncBreakdown, toEntries],
  );

  /** 上下文变化只更新额度与分类；增量动画改由真实 usage 驱动。 */
  const applyContext = useCallback(
    (tokens: number) => {
      const previous = lastContextRef.current;
      if (tokens <= previous) {
        setContextTokens((value) => Math.max(value, tokens));
        syncBreakdown(tokens);
        return;
      }
      lastContextRef.current = tokens;
      setContextTokens(tokens);
      syncBreakdown(tokens);
    },
    [syncBreakdown],
  );

  /** 每次 JSONL usage 到达时累加 input+output；同一轮多次调用合并显示。 */
  const pushUsageDelta = useCallback((amount: number) => {
    if (amount <= 0) return;
    const accumulated = deltaAccumRef.current + amount;
    deltaAccumRef.current = accumulated;
    setUsageDelta(accumulated);
    setUsageDeltaSeq((seq) => seq + 1);
    if (deltaTimer.current) clearTimeout(deltaTimer.current);
    deltaTimer.current = setTimeout(() => {
      deltaAccumRef.current = 0;
      setUsageDelta(0);
    }, 2_200);
  }, []);

  useEffect(
    () => () => {
      if (deltaTimer.current) clearTimeout(deltaTimer.current);
    },
    [],
  );

  useEffect(() => {
    if (!role || !agentId || !enabled) {
      return;
    }
    const controller = new AbortController();
    let cancelled = false;
    const bindKey = `${role}:${agentId}:${generation}`;
    const connect = async () => {
      if (lastBindRef.current !== bindKey) {
        lastBindRef.current = bindKey;
        setNotice(null);
      }
      setEntries([]);
      setSessionId(null);
      setContextTokens(0);
      setContextBreakdown(null);
      setUsageDelta(0);
      deltaAccumRef.current = 0;
      lastContextRef.current = 0;
      baseTokensRef.current = 0;
      firstUserTokensRef.current = -1;
      setUsage(EMPTY_USAGE);
      try {
        const history = await getPtyHistory(role, agentId, terminalId);
        if (cancelled) return;
        setSessionId(history.session_id);
        setContextTokens(history.context_tokens);
        lastContextRef.current = history.context_tokens;
        const parts = contextBaseParts(history.messages);
        baseTokensRef.current = parts?.base ?? 0;
        firstUserTokensRef.current = parts?.firstUserTokens ?? -1;
        setContextBreakdown(
          computeContextBreakdown(history.messages, history.context_tokens),
        );
        setUsage(history.usage ?? EMPTY_USAGE);
        setBudgetTokens(history.budget_tokens ?? 0);
        setEntries(toEntries(history.messages));
        const response = await streamFetch(
          await ptyChatStreamUrl(history.offset, role, agentId, terminalId),
          {
            headers: { Accept: "text/event-stream" },
            signal: controller.signal,
          },
        );
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        await consumePtyChatStream(response, {
          onMessages: appendMessages,
          onContext: applyContext,
          onUsage: (delta) => {
            setUsage((previous) => mergeUsage(previous, delta));
            pushUsageDelta(delta.input_tokens + delta.output_tokens);
          },
          onNotice: (next) => setNotice(next),
          onSession: (sid) => {
            if (!cancelled) {
              setSessionId(sid);
              setEpoch((value) => value + 1);
            }
          },
          onExit: () => {
            if (!cancelled) setBusy(false);
          },
        });
      } catch (err) {
        if (!controller.signal.aborted) {
          const message = errorMessage(err);
          setError(message);
          onErrorRef.current?.(message);
        }
      }
    };
    void connect();
    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [appendMessages, applyContext, pushUsageDelta, toEntries, role, agentId, enabled, epoch, generation, terminalId]);

  const send = useCallback(
    async (prompt: string) => {
      const text = prompt.trim();
      if (!text || !role || !agentId || !enabled) return;
      setBusy(true);
      setError(null);
      if (busyTimer.current) clearTimeout(busyTimer.current);
      busyTimer.current = setTimeout(() => setBusy(false), BUSY_TIMEOUT_MS);
      try {
        const status = await startPty(role, agentId, terminalId);
        setSessionId(status.session_id);
        await sendPtyInput(`${text}\r`, role, agentId, terminalId);
      } catch (err) {
        setBusy(false);
        const message = errorMessage(err);
        setError(message);
        onErrorRef.current?.(message);
      }
    },
    [role, agentId, enabled, terminalId],
  );

  const stop = useCallback(async () => {
    setBusy(false);
    if (busyTimer.current) clearTimeout(busyTimer.current);
    if (!role || !agentId) return;
    try {
      await sendPtyInput("\u0003", role, agentId, terminalId);
    } catch {
      // PTY 已退出时忽略
    }
  }, [role, agentId, terminalId]);

  const active = Boolean(role) && Boolean(agentId) && enabled;
  return {
    entries: active ? entries : [],
    busy: active ? busy : false,
    contextTokens: active ? contextTokens : 0,
    contextBreakdown: active ? contextBreakdown : null,
    usageDelta: active ? usageDelta : 0,
    usageDeltaSeq,
    usage: active ? usage : EMPTY_USAGE,
    budgetTokens: active ? budgetTokens : 0,
    notice: active ? notice : null,
    sessionId: active ? sessionId : null,
    error: active ? error : null,
    send,
    stop,
  };
}
