// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { SessionList } from "./SessionList";
import type { Agent, PoolTerminal } from "@/lib/types";

afterEach(cleanup);

function terminal(overrides: Partial<PoolTerminal>): PoolTerminal {
  return {
    terminal_id: "t-00000001",
    kind: "shell",
    role: "",
    agent_id: "",
    command: "powershell.exe",
    state: "running",
    pid: 100,
    session_id: "",
    pinned: false,
    busy: false,
    failed_reason: "",
    created_at: 1,
    ...overrides,
  };
}

function agent(role: string, override: Partial<Agent> = {}): Agent {
  return {
    role,
    agent_id: `${role}-id`,
    pid: 100,
    status: "registered",
    alive: true,
    source: "external",
    ...override,
  };
}

describe("SessionList", () => {
  it("lists agent terminals and shells together and routes selection by terminal", () => {
    const onSelectAgent = vi.fn();
    const onSelectTerminal = vi.fn();
    render(
      <SessionList
        agents={[agent("reviewer"), agent("dev")]}
        terminals={[
          terminal({ terminal_id: "t-agent", kind: "agent", role: "reviewer", created_at: 1 }),
          terminal({ terminal_id: "t-shell", kind: "shell", created_at: 2 }),
        ]}
        activeRole="reviewer"
        activeTerminalId="t-agent"
        onSelectAgent={onSelectAgent}
        onSelectTerminal={onSelectTerminal}
        onRemoveTerminal={vi.fn()}
        onEditAgent={vi.fn()}
        onDeleteAgent={vi.fn()}
        onStop={vi.fn()}
      />,
    );

    expect(screen.getByText("reviewer")).toBeInTheDocument();
    expect(screen.getByText("终端 1")).toBeInTheDocument();

    fireEvent.click(screen.getByText("终端 1"));
    expect(onSelectTerminal).toHaveBeenCalledWith("t-shell");

    fireEvent.click(screen.getByText("dev"));
    expect(onSelectAgent).toHaveBeenCalledWith("dev");
  });

  it("closes a terminal from the row actions", () => {
    const onRemoveTerminal = vi.fn();
    render(
      <SessionList
        agents={[]}
        terminals={[terminal({ terminal_id: "t-shell" })]}
        activeRole={null}
        activeTerminalId="t-shell"
        onSelectAgent={vi.fn()}
        onSelectTerminal={vi.fn()}
        onRemoveTerminal={onRemoveTerminal}
        onEditAgent={vi.fn()}
        onDeleteAgent={vi.fn()}
        onStop={vi.fn()}
      />,
    );

    fireEvent.click(screen.getByLabelText("下线终端 终端 1"));
    expect(onRemoveTerminal).toHaveBeenCalledWith("t-shell");
  });

  it("keeps online internal agents to the offline action only", () => {
    render(
      <SessionList
        agents={[agent("reviewer", { source: "local" })]}
        terminals={[terminal({ terminal_id: "t-reviewer", kind: "agent", role: "reviewer" })]}
        activeRole="reviewer"
        activeTerminalId="t-reviewer"
        onSelectAgent={vi.fn()}
        onSelectTerminal={vi.fn()}
        onRemoveTerminal={vi.fn()}
        onEditAgent={vi.fn()}
        onDeleteAgent={vi.fn()}
        onStop={vi.fn()}
      />,
    );

    expect(screen.getByLabelText("下线终端 reviewer")).toBeInTheDocument();
    expect(screen.queryByLabelText("彻底删除 reviewer")).not.toBeInTheDocument();
  });

  it("keeps online external agents read-only", () => {
    render(
      <SessionList
        agents={[agent("test-ext")]}
        terminals={[]}
        activeRole={null}
        activeTerminalId="1"
        onSelectAgent={vi.fn()}
        onSelectTerminal={vi.fn()}
        onRemoveTerminal={vi.fn()}
        onEditAgent={vi.fn()}
        onDeleteAgent={vi.fn()}
        onStop={vi.fn()}
      />,
    );

    expect(screen.queryByLabelText("下线终端 test-ext")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("彻底删除 test-ext")).not.toBeInTheDocument();
  });

  it("shows grey offline when the agent record stopped even if the terminal runs", () => {
    render(
      <SessionList
        agents={[agent("test-ext", { alive: true, status: "stopped" })]}
        terminals={[
          terminal({ terminal_id: "t-ext", kind: "agent", role: "test-ext", state: "running" }),
        ]}
        activeRole="test-ext"
        activeTerminalId="t-ext"
        onSelectAgent={vi.fn()}
        onSelectTerminal={vi.fn()}
        onRemoveTerminal={vi.fn()}
        onEditAgent={vi.fn()}
        onDeleteAgent={vi.fn()}
        onStop={vi.fn()}
      />,
    );

    expect(screen.getByText("离线")).toBeInTheDocument();
    expect(screen.queryByText("在线")).not.toBeInTheDocument();
  });

  it("hides the offline action for stopped agents and keeps delete", () => {
    render(
      <SessionList
        agents={[agent("test-ext", { alive: false, status: "stopped" })]}
        terminals={[terminal({ terminal_id: "t-ext", kind: "agent", role: "test-ext", state: "stopped" })]}
        activeRole={null}
        activeTerminalId="t-ext"
        onSelectAgent={vi.fn()}
        onSelectTerminal={vi.fn()}
        onRemoveTerminal={vi.fn()}
        onEditAgent={vi.fn()}
        onDeleteAgent={vi.fn()}
        onStop={vi.fn()}
      />,
    );

    expect(screen.queryByLabelText("下线终端 test-ext")).not.toBeInTheDocument();
    expect(screen.getByLabelText("彻底删除 test-ext")).toBeInTheDocument();
  });

  it("marks a failed agent locally instead of using a global alert", () => {
    render(
      <SessionList
        agents={[agent("test-ext")]}
        terminals={[]}
        errors={{ "test-ext": "agent offline: test-ext" }}
        activeRole={null}
        activeTerminalId="1"
        onSelectAgent={vi.fn()}
        onSelectTerminal={vi.fn()}
        onRemoveTerminal={vi.fn()}
        onEditAgent={vi.fn()}
        onDeleteAgent={vi.fn()}
        onStop={vi.fn()}
      />,
    );

    expect(screen.getByText("test-ext")).toHaveClass("text-red-300");
    expect(screen.getByLabelText("test-ext 有错误")).toBeInTheDocument();
  });

  it("builds distinct avatars from role numbers", () => {
    render(
      <SessionList
        agents={[agent("test-01"), agent("test-02")]}
        terminals={[]}
        activeRole={null}
        activeTerminalId="1"
        onSelectAgent={vi.fn()}
        onSelectTerminal={vi.fn()}
        onRemoveTerminal={vi.fn()}
        onEditAgent={vi.fn()}
        onDeleteAgent={vi.fn()}
        onStop={vi.fn()}
      />,
    );

    expect(screen.getByText("T01")).toBeInTheDocument();
    expect(screen.getByText("T02")).toBeInTheDocument();
  });
});
