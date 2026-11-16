"""单 PTY 常驻：PowerShell 回显、resize 与回收（真实 ConPTY）。"""

from __future__ import annotations

import json
import queue
import sys
import time
from pathlib import Path
from typing import Any, cast

import pytest

from tools.orchestrator.pty_manager import (
    PtyManager,
    claude_command,
    split_partial_escape,
    strip_ansi,
    tail_text,
)

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="ConPTY on Windows")

ROOT = Path(__file__).resolve().parents[2]
SHELL = ("powershell.exe", "-NoLogo", "-NoProfile")


def test_claude_command_pins_session() -> None:
    assert claude_command("1111-2222") == ["claude", "--session-id", "1111-2222"]


def test_shell_echo_resize_and_stop() -> None:
    manager = PtyManager(cwd=str(ROOT), command=SHELL)
    subscriber = None
    try:
        status = manager.start()
        assert status["running"] is True
        assert status["pid"] > 0
        assert status["session_id"]

        subscriber, _backlog = manager.subscribe()
        assert manager.write("echo PTY_SMOKE\r") is True

        output = ""
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and "PTY_SMOKE" not in output:
            try:
                event, data = subscriber.get(timeout=0.5)
            except queue.Empty:
                continue
            if event == "output":
                output += bytes(data["data"]).decode("utf-8", errors="replace")
        assert "PTY_SMOKE" in output

        resized = manager.resize(100, 40)
        assert resized["cols"] == 100 and resized["rows"] == 40
    finally:
        if subscriber is not None:
            manager.unsubscribe(subscriber)
        stopped = manager.stop()
    assert stopped["running"] is False


def test_strip_ansi_and_tail_text() -> None:
    raw = "\x1b[2J\x1b[H\x1b[31mred\x1b[0m\r\n\x1b]0;title\x07ok\r\n"
    assert strip_ansi(raw) == "red\nok\n"
    lines = "\n".join(f"line-{index}" for index in range(300))
    tail = tail_text(lines, 200, 20 * 1024)
    assert tail.startswith("line-100")
    assert len(tail.splitlines()) == 200
    assert split_partial_escape("abc\x1b[3") == ("abc", "\x1b[3")
    assert split_partial_escape("abc\x1b[31m") == ("abc\x1b[31m", "")


def test_plain_tail_tracks_broadcast_without_ansi(tmp_path: Path) -> None:
    manager = PtyManager(cwd=str(tmp_path), command=SHELL)
    manager._broadcast(b"\x1b[32mgreen\x1b[0m\r\nline2\r\n")
    assert manager.plain_tail(200, 20 * 1024) == "green\nline2"


def test_resize_skips_unchanged_dimensions_and_honors_force(tmp_path: Path) -> None:
    manager = PtyManager(cwd=str(tmp_path), command=SHELL)
    calls: list[tuple[int, int]] = []

    class FakeProcess:
        def isalive(self) -> bool:
            return True

        def setwinsize(self, rows: int, cols: int) -> None:
            calls.append((rows, cols))

    manager._process = cast(Any, FakeProcess())
    manager.cols, manager.rows = 120, 30

    assert manager.resize(120, 30)["cols"] == 120
    assert calls == []
    assert manager.resize(100, 40)["cols"] == 100
    assert calls == [(40, 100)]
    assert manager.resize(100, 40)["cols"] == 100
    assert calls == [(40, 100)]
    assert manager.resize(100, 40, force=True)["cols"] == 100
    assert calls == [(40, 100), (40, 100)]


def test_write_without_process_returns_false(tmp_path: Path) -> None:
    manager = PtyManager(cwd=str(tmp_path), command=SHELL)
    assert manager.write("dir\r") is False
    assert manager.status()["running"] is False


def test_state_file_tracks_running_pty(tmp_path: Path) -> None:
    state = tmp_path / "pty.json"
    manager = PtyManager(cwd=str(ROOT), command=SHELL, state_path=state)
    try:
        status = manager.start()
        payload = json.loads(state.read_text(encoding="utf-8"))
        assert payload["pid"] == status["pid"]
        assert payload["session_id"] == status["session_id"]
        assert payload["command"] == SHELL[0]
    finally:
        manager.stop()
    assert not state.exists()
