// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";

import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ToastViewport } from "./ToastViewport";

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe("ToastViewport", () => {
  it("plays the exit animation before removing a manually closed toast", () => {
    vi.useFakeTimers();
    const onDismiss = vi.fn();
    render(<ToastViewport toasts={[{ id: 1, message: "未配置唤醒词" }]} onDismiss={onDismiss} />);

    fireEvent.click(screen.getByLabelText("关闭提示"));

    // 动画期间仍在 DOM 中，只是切换到 exit 状态。
    expect(screen.getByText("未配置唤醒词")).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveAttribute("data-exiting", "true");
    expect(onDismiss).not.toHaveBeenCalled();

    act(() => vi.advanceTimersByTime(300));
    expect(onDismiss).toHaveBeenCalledWith(1);
  });

  it("auto-dismisses after 4s, then removes after the animation", () => {
    vi.useFakeTimers();
    const onDismiss = vi.fn();
    render(<ToastViewport toasts={[{ id: 2, message: "操作失败" }]} onDismiss={onDismiss} />);

    act(() => vi.advanceTimersByTime(3_999));
    expect(onDismiss).not.toHaveBeenCalled();
    expect(screen.getByRole("status")).not.toHaveAttribute("data-exiting");

    act(() => vi.advanceTimersByTime(1));
    expect(screen.getByRole("status")).toHaveAttribute("data-exiting", "true");
    act(() => vi.advanceTimersByTime(300));
    expect(onDismiss).toHaveBeenCalledWith(2);
  });

  it("renders nothing without toasts", () => {
    const { container } = render(<ToastViewport toasts={[]} onDismiss={vi.fn()} />);
    expect(container.firstChild).toBeNull();
  });
});
