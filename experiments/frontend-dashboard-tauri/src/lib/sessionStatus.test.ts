import { describe, expect, it } from "vitest";

import { resolveSessionStatus } from "./sessionStatus";

const NOW = 1_000_000;

describe("resolveSessionStatus", () => {
  it("defaults to green online when the process is alive", () => {
    expect(
      resolveSessionStatus({ online: true, processAlive: true, now: NOW }),
    ).toEqual({ label: "在线", tone: "online" });
    expect(
      resolveSessionStatus({
        online: true,
        processAlive: true,
        activityState: "working",
        now: NOW,
      }),
    ).toEqual({ label: "在线", tone: "online" });
  });

  it("is blue running for tasks, tools, or recent output", () => {
    expect(
      resolveSessionStatus({
        online: true,
        processAlive: true,
        hasTask: true,
        now: NOW,
      }),
    ).toEqual({ label: "运行中", tone: "running" });
    expect(
      resolveSessionStatus({
        online: true,
        processAlive: true,
        pendingTools: 1,
        now: NOW,
      }),
    ).toEqual({ label: "运行中", tone: "running" });
    expect(
      resolveSessionStatus({
        online: true,
        processAlive: true,
        lastActivityAt: NOW - 2,
        now: NOW,
      }),
    ).toEqual({ label: "运行中", tone: "running" });
  });

  it("returns green immediately after an explicit end_turn", () => {
    expect(
      resolveSessionStatus({
        online: true,
        processAlive: true,
        activityState: "ended",
        lastActivityAt: NOW,
        now: NOW,
      }),
    ).toEqual({ label: "在线", tone: "online" });
  });

  it("keeps yellow action-required above running, forever", () => {
    expect(
      resolveSessionStatus({
        online: true,
        processAlive: true,
        waiting: true,
        pendingTools: 1,
        lastActivityAt: NOW - 3_600,
        now: NOW,
      }),
    ).toEqual({ label: "待处理", tone: "waiting" });
  });

  it("is red for failed processes or a disconnected daemon", () => {
    expect(
      resolveSessionStatus({ online: false, processAlive: true, now: NOW }),
    ).toEqual({ label: "报错", tone: "error" });
    expect(
      resolveSessionStatus({
        online: true,
        processAlive: false,
        terminalState: "failed",
        now: NOW,
      }),
    ).toEqual({ label: "报错", tone: "error" });
    expect(
      resolveSessionStatus({
        online: true,
        processAlive: true,
        agentStatus: "lost",
        now: NOW,
      }),
    ).toEqual({ label: "报错", tone: "error" });
  });

  it("is grey offline for stopped or killed processes", () => {
    expect(
      resolveSessionStatus({
        online: true,
        processAlive: false,
        terminalState: "stopped",
        now: NOW,
      }),
    ).toEqual({ label: "离线", tone: "offline" });
    expect(
      resolveSessionStatus({
        online: true,
        processAlive: false,
        agentStatus: "stopped",
        now: NOW,
      }),
    ).toEqual({ label: "离线", tone: "offline" });
  });
});
