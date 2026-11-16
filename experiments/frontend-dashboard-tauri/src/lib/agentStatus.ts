import type { Agent } from "./types";

const OFFLINE_STATUSES = new Set(["stopped", "lost", "failed", "pending"]);

/**
 * 唯一在线口径：左侧灯、对话锁定、AgentTable 必须共用。
 * status 已停止/失联/失败/待注册，或 alive=false，一律不算在线。
 */
export function isAgentOnline(agent?: Agent | null): boolean {
  if (!agent || agent.alive !== true || !agent.agent_id) return false;
  return !OFFLINE_STATUSES.has(agent.status ?? "");
}
