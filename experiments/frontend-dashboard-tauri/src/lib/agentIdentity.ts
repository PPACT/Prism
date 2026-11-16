import type { Agent, PoolTerminal } from "./types";

const OFFLINE_STATUS = new Set(["stopped", "lost", "failed", "pending"]);

/** 角色当前可用的 agent_id；离线/停止的角色不参与身份复用。 */
export function liveAgentId(agents: Agent[], role: string): string | null {
  const key = role.trim();
  if (!key) return null;
  for (const agent of agents) {
    if ((agent.role ?? "").trim() !== key) continue;
    if (agent.alive !== true || !agent.agent_id) continue;
    if (OFFLINE_STATUS.has(agent.status ?? "")) continue;
    return agent.agent_id;
  }
  return null;
}

/**
 * 前端双保险：注册期身份复用后，池终端里可能还留着旧 agent_id。
 * 只要同角色 Agent 仍存活，就用最新身份证覆盖终端句柄，绝不把在线角色显示成掉线。
 * 无变化时返回原数组，避免无意义的状态刷新。
 */
export function reconcileTerminalAgentIds(
  agents: Agent[],
  terminals: PoolTerminal[],
): PoolTerminal[] {
  let changed = false;
  const next = terminals.map((terminal) => {
    if (terminal.kind !== "agent" || !terminal.role) return terminal;
    const current = liveAgentId(agents, terminal.role);
    if (!current || current === terminal.agent_id) return terminal;
    changed = true;
    return { ...terminal, agent_id: current };
  });
  return changed ? next : terminals;
}
