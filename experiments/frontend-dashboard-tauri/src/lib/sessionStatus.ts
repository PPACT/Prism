export type SessionTone = "online" | "running" | "waiting" | "error" | "offline";

export interface SessionStatusInput {
  /** Daemon SSE 是否在线；断网按“报错”处理。 */
  online: boolean;
  /** 进程是否存活。 */
  processAlive: boolean;
  /** 终端生命周期状态。 */
  terminalState?: string;
  /** Agent registry 状态（stopped / failed / lost / pending…）。 */
  agentStatus?: string;
  /** Hook 是否在等待用户处理权限/菜单（最高优先级的动态状态）。 */
  waiting?: boolean;
  /** 是否有中控派发任务。 */
  hasTask?: boolean;
  /** 未完成的 tool_use 数量。 */
  pendingTools?: number;
  /** JSONL 事件状态：working / ended / unknown。 */
  activityState?: string;
  /** 最近一次底层输出活动（epoch 秒：PTY 输出 / assistant JSONL）。 */
  lastActivityAt?: number;
  /** 当前时间（epoch 秒）。 */
  now: number;
}

export interface SessionStatus {
  label: "在线" | "运行中" | "待处理" | "报错" | "离线";
  tone: SessionTone;
}

/** 输出结束后的合并窗口；end_turn 会立即回在线，不依赖这个窗口。 */
const OUTPUT_WINDOW_SECONDS = 3;

/** 五色矩阵：离线/报错 > 待处理（黄）> 运行中（蓝）> 在线（绿）。 */
export function resolveSessionStatus(input: SessionStatusInput): SessionStatus {
  // 网络/SSE 断开：视为报错（进程是否存在未知）。
  if (!input.online) {
    return { label: "报错", tone: "error" };
  }
  if (input.terminalState === "failed") {
    return { label: "报错", tone: "error" };
  }
  if (input.agentStatus === "failed" || input.agentStatus === "lost") {
    return { label: "报错", tone: "error" };
  }
  if (!input.processAlive) {
    if (
      input.terminalState === "stopped" ||
      input.agentStatus === "stopped" ||
      input.agentStatus === "pending"
    ) {
      return { label: "离线", tone: "offline" };
    }
    return { label: "报错", tone: "error" };
  }
  // 等待用户操作优先于运行/在线，且无时间上限。
  if (input.waiting) {
    return { label: "待处理", tone: "waiting" };
  }
  if (input.hasTask || (input.pendingTools ?? 0) > 0) {
    return { label: "运行中", tone: "running" };
  }
  // 一轮明确结束：立刻回到在线绿色。
  if (input.activityState === "ended") {
    return { label: "在线", tone: "online" };
  }
  const last = input.lastActivityAt ?? 0;
  if (last > 0 && input.now - last <= OUTPUT_WINDOW_SECONDS) {
    return { label: "运行中", tone: "running" };
  }
  return { label: "在线", tone: "online" };
}
