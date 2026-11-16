import { describe, expect, it } from "vitest";

import { liveAgentId, reconcileTerminalAgentIds } from "./agentIdentity";
import type { Agent, PoolTerminal } from "./types";

function agent(role: string, agentId: string, alive = true, status = "registered"): Agent {
  return { role, agent_id: agentId, alive, status };
}

function terminal(role: string, agentId: string, kind: "agent" | "shell" = "agent"): PoolTerminal {
  return {
    terminal_id: `t-${role}`,
    kind,
    role,
    agent_id: agentId,
    command: "claude",
    state: "running",
    pid: 1,
    session_id: "",
    pinned: true,
    busy: false,
    failed_reason: "",
  };
}

describe("liveAgentId", () => {
  it("returns the current id for a live role and ignores stopped roles", () => {
    const agents = [agent("dev", "new-doc", false), agent("dev", "new-dev")];
    expect(liveAgentId(agents, "dev")).toBe("new-dev");
    expect(liveAgentId(agents, "doc")).toBeNull();
  });
});

describe("reconcileTerminalAgentIds", () => {
  it("updates stale terminal handles while the role stays alive", () => {
    const agents = [agent("dev", "id-new")];
    const terminals = [terminal("dev", "id-old")];

    const synced = reconcileTerminalAgentIds(agents, terminals);

    expect(synced[0].agent_id).toBe("id-new");
    expect(terminals[0].agent_id).toBe("id-old");
  });

  it("keeps the original array when identities already match", () => {
    const agents = [agent("dev", "id-new")];
    const terminals = [terminal("dev", "id-new")];
    expect(reconcileTerminalAgentIds(agents, terminals)).toBe(terminals);
  });

  it("does not touch offline roles or shell terminals", () => {
    const agents = [agent("dev", "id-new", false)];
    const terminals = [terminal("dev", "id-old"), terminal("", "shell-id", "shell")];
    expect(reconcileTerminalAgentIds(agents, terminals)).toBe(terminals);
  });
});
