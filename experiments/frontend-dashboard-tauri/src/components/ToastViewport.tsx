import { X } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";

export interface ToastItem {
  id: number;
  message: string;
}

interface ToastViewportProps {
  toasts: ToastItem[];
  /** 退场动画结束后的真正移除；不要在动画开始前调用。 */
  onDismiss: (id: number) => void;
}

const AUTO_DISMISS_MS = 4_000;
const EXIT_ANIMATION_MS = 300;

interface ToastCardProps {
  toast: ToastItem;
  onDismiss: (id: number) => void;
}

function ToastCard({ toast, onDismiss }: ToastCardProps) {
  const [exiting, setExiting] = useState(false);
  const exitStarted = useRef(false);
  const exitTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const autoTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const beginExit = useCallback(() => {
    if (exitStarted.current) return;
    exitStarted.current = true;
    setExiting(true);
    // 先播放 300ms 退场动画，再从 DOM 移除，避免“直接闪没”。
    exitTimer.current = setTimeout(() => onDismiss(toast.id), EXIT_ANIMATION_MS);
  }, [onDismiss, toast.id]);

  useEffect(() => {
    autoTimer.current = setTimeout(beginExit, AUTO_DISMISS_MS);
    return () => {
      if (autoTimer.current !== null) clearTimeout(autoTimer.current);
      if (exitTimer.current !== null) clearTimeout(exitTimer.current);
    };
  }, [beginExit]);

  return (
    <div
      role="status"
      data-exiting={exiting ? "true" : undefined}
      className={`pointer-events-auto flex items-start gap-2 rounded-lg border border-red-500/40 bg-zinc-900/95 px-3 py-2 text-[12px] text-red-200 shadow-lg backdrop-blur will-change-transform ${
        exiting
          ? "animate-out fade-out-0 slide-out-to-right-4 zoom-out-95 duration-300 ease-in-out"
          : "animate-in fade-in-0 slide-in-from-right-4 duration-300 ease-out"
      }`}
    >
      <span className="min-w-0 flex-1 break-words">{toast.message}</span>
      <button
        type="button"
        aria-label="关闭提示"
        className="shrink-0 rounded p-0.5 text-red-300/60 transition-colors hover:text-red-100"
        onClick={beginExit}
      >
        <X className="size-3.5" />
      </button>
    </div>
  );
}

/** 轻量 Toast：操作类/业务类失败 4 秒自动退场；系统级错误仍走常驻红条。 */
export function ToastViewport({ toasts, onDismiss }: ToastViewportProps) {
  if (toasts.length === 0) return null;
  return (
    <div className="pointer-events-none fixed bottom-4 right-4 z-[100] flex w-[min(360px,calc(100vw-2rem))] flex-col gap-2">
      {toasts.map((toast) => (
        <ToastCard key={toast.id} toast={toast} onDismiss={onDismiss} />
      ))}
    </div>
  );
}
