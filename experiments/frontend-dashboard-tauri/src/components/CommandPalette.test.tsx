// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { CommandPalette } from "./CommandPalette";

afterEach(cleanup);

function renderPalette() {
  const onRefresh = vi.fn();
  const onReconnect = vi.fn();
  const onClearLogs = vi.fn();
  const onOpenChange = vi.fn();
  render(
    <CommandPalette
      open
      onOpenChange={onOpenChange}
      onRefresh={onRefresh}
      onReconnect={onReconnect}
      onClearLogs={onClearLogs}
    />,
  );
  return { onRefresh, onReconnect, onClearLogs, onOpenChange };
}

describe("CommandPalette", () => {
  it("opens with focus on the panel, not the search input", async () => {
    renderPalette();
    const input = screen.getByPlaceholderText(/输入命令或搜索/);
    const panel = screen.getByRole("listbox", { name: "命令面板" });

    await waitFor(() => expect(document.activeElement).toBe(panel));
    expect(document.activeElement).not.toBe(input);
  });

  it("runs single-letter shortcuts while the panel is focused", async () => {
    const { onRefresh, onOpenChange } = renderPalette();
    const panel = screen.getByRole("listbox", { name: "命令面板" });
    await waitFor(() => expect(document.activeElement).toBe(panel));

    fireEvent.keyDown(panel, { key: "r" });

    expect(onRefresh).toHaveBeenCalledTimes(1);
    expect(onOpenChange).toHaveBeenCalledWith(false);
  });

  it("moves the highlight with arrows and executes with Enter", async () => {
    const { onReconnect } = renderPalette();
    const panel = screen.getByRole("listbox", { name: "命令面板" });
    await waitFor(() => expect(document.activeElement).toBe(panel));

    fireEvent.keyDown(panel, { key: "ArrowDown" });
    fireEvent.keyDown(panel, { key: "Enter" });

    expect(onReconnect).toHaveBeenCalledTimes(1);
  });

  it("switches to search mode for unbound letters and slash", async () => {
    renderPalette();
    const input = screen.getByPlaceholderText(/输入命令或搜索/);
    const panel = screen.getByRole("listbox", { name: "命令面板" });
    await waitFor(() => expect(document.activeElement).toBe(panel));

    fireEvent.keyDown(panel, { key: "x" });
    expect(document.activeElement).toBe(input);
    expect(input).toHaveValue("x");

    fireEvent.keyDown(panel, { key: "/" });
    expect(document.activeElement).toBe(input);
  });
});
