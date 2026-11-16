"""P0 PTY 池 Spike：双终端、转换 A、上限/内存、pin/TTL、工作区切换保留进程。"""

from __future__ import annotations

import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest

from tools.orchestrator import service
from tools.orchestrator.claude_hooks import PERMISSION_MATCHER

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="ConPTY on Windows")

SHELL = "powershell.exe -NoLogo -NoProfile"


@pytest.fixture()
def pool_daemon(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[service.Daemon]:
    monkeypatch.setenv("SENTINEL_TERMINAL_POOL", "1")
    monkeypatch.setenv("SENTINEL_TERMINAL_POOL_LIMIT", "2")
    monkeypatch.setenv("SENTINEL_TERMINAL_POOL_MEMORY_PERCENT", "101")
    monkeypatch.setenv("SENTINEL_TERMINAL_SHELL", SHELL)
    monkeypatch.setenv("SENTINEL_PTY_COMMAND", SHELL)
    daemon = service.Daemon(tmp_path / "orch")
    yield daemon
    daemon.terminals.stop_all()


def _shell(daemon: service.Daemon) -> dict[str, Any]:
    status, body = daemon.create_terminal({"kind": "shell"})
    assert status == 201, body
    return cast(dict[str, Any], body["terminal"])


def _agent(daemon: service.Daemon, role: str) -> dict[str, Any]:
    status, body = daemon.create_terminal({"kind": "agent", "role": role, "command": SHELL})
    assert status == 201, body
    return cast(dict[str, Any], body["terminal"])


def test_pool_flag_off_disables_creation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SENTINEL_TERMINAL_POOL", raising=False)
    daemon = service.Daemon(tmp_path / "orch")
    assert daemon.pool_enabled() is False
    status, _body = daemon.create_terminal({"kind": "shell"})
    assert status == 404


def test_two_terminals_isolated_pids_and_status(pool_daemon: service.Daemon) -> None:
    shell = _shell(pool_daemon)
    agent = _agent(pool_daemon, "reviewer")
    assert shell["terminal_id"] != agent["terminal_id"]
    assert shell["pid"] > 0 and agent["pid"] > 0
    assert shell["pid"] != agent["pid"]
    statuses = {item["terminal_id"]: item for item in pool_daemon.pool_status()["terminals"]}
    assert statuses[shell["terminal_id"]]["kind"] == "shell"
    assert statuses[agent["terminal_id"]]["kind"] == "agent"
    assert statuses[agent["terminal_id"]]["role"] == "reviewer"
    assert statuses[agent["terminal_id"]]["pinned"] is True


def test_convert_shell_to_agent_keeps_terminal_id(pool_daemon: service.Daemon) -> None:
    shell = _shell(pool_daemon)
    terminal_id = shell["terminal_id"]
    old_pid = shell["pid"]
    status, body = pool_daemon.convert_terminal(terminal_id, {"role": "reviewer", "command": SHELL})
    assert status == 200, body
    converted = body["terminal"]
    assert converted["terminal_id"] == terminal_id
    assert converted["kind"] == "agent"
    assert converted["role"] == "reviewer"
    assert converted["pid"] > 0 and converted["pid"] != old_pid
    assert converted["state"] == "running"
    assert pool_daemon.terminals.get(terminal_id) is not None
    # 原 Shell 进程必须已经不在。
    status_after, _ = pool_daemon.remove_terminal(terminal_id, confirm=True)
    assert status_after == 200


def test_convert_preflight_failure_keeps_shell(pool_daemon: service.Daemon) -> None:
    shell = _shell(pool_daemon)
    terminal_id = shell["terminal_id"]
    status, body = pool_daemon.convert_terminal(
        terminal_id, {"role": "reviewer", "command": "definitely-not-a-command-xyz"}
    )
    assert status == 400, body
    current = pool_daemon.terminal_status(terminal_id)
    assert current is not None
    assert current["kind"] == "shell"
    assert current["state"] == "running"
    assert current["pid"] == shell["pid"]


def test_convert_spawn_failure_marks_failed_without_restarting_shell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SENTINEL_TERMINAL_POOL", "1")
    monkeypatch.setenv("SENTINEL_TERMINAL_POOL_LIMIT", "2")
    monkeypatch.setenv("SENTINEL_TERMINAL_SHELL", SHELL)
    monkeypatch.setenv("SENTINEL_PTY_COMMAND", "definitely-not-a-command-xyz")
    daemon = service.Daemon(tmp_path / "orch")
    try:
        shell = _shell(daemon)
        terminal_id = shell["terminal_id"]
        status, body = daemon.convert_terminal(terminal_id, {"role": "reviewer", "command": SHELL})
        assert status == 500, body
        current = daemon.terminal_status(terminal_id)
        assert current is not None
        assert current["kind"] == "agent"
        assert current["state"] == "failed"
        assert current["pid"] == 0
        failed_pty = daemon.terminals.get(terminal_id)
        assert failed_pty is not None
        assert failed_pty.status()["running"] is False
    finally:
        daemon.terminals.stop_all()


def test_limit_rejects_when_no_evictable_shell(pool_daemon: service.Daemon) -> None:
    _agent(pool_daemon, "reviewer")
    _agent(pool_daemon, "writer")
    status, body = pool_daemon.create_terminal(
        {"kind": "agent", "role": "manager", "command": SHELL}
    )
    assert status == 429, body
    assert body["reason"] == "pool_limit"


def test_memory_pressure_rejects_without_shell(
    pool_daemon: service.Daemon, monkeypatch: pytest.MonkeyPatch
) -> None:
    _agent(pool_daemon, "reviewer")
    _agent(pool_daemon, "writer")
    monkeypatch.setattr(service, "memory_percent", lambda: 95.0)
    monkeypatch.setattr(pool_daemon, "pool_limit", lambda: 6)
    monkeypatch.setattr(pool_daemon, "pool_memory_threshold", lambda: 80.0)
    status, body = pool_daemon.create_terminal(
        {"kind": "agent", "role": "manager", "command": SHELL}
    )
    assert status == 507, body
    assert body["reason"] == "memory_pressure"


def test_pin_and_ttl_only_reap_shells(pool_daemon: service.Daemon) -> None:
    shell = _shell(pool_daemon)
    agent = _agent(pool_daemon, "reviewer")
    shell_meta = pool_daemon.terminals.meta(shell["terminal_id"])
    assert shell_meta is not None
    shell_meta.last_active_at = time.monotonic() - 10_000
    removed = pool_daemon.terminals.sweep(60)
    assert shell["terminal_id"] in removed
    assert pool_daemon.terminals.get(shell["terminal_id"]) is None
    assert pool_daemon.terminals.get(agent["terminal_id"]) is not None


def test_workspace_switch_keeps_process(pool_daemon: service.Daemon, tmp_path: Path) -> None:
    shell = _shell(pool_daemon)
    other = pool_daemon.workspaces.save(
        {
            "name": "other",
            "project_path": str(tmp_path),
            "command": SHELL,
            "engine": "claude",
            "budget_tokens": 200_000,
        }
    )
    pool_daemon.activate_workspace(int(other["id"]))
    current = pool_daemon.terminal_status(shell["terminal_id"])
    assert current is not None
    assert current["state"] == "running"
    assert current["pid"] == shell["pid"]


def test_activation_does_not_refresh_activity(pool_daemon: service.Daemon) -> None:
    """前端选中/激活终端不能把它从待机刷成运行中。"""
    shell = _shell(pool_daemon)
    terminal_id = shell["terminal_id"]
    pty = pool_daemon.terminals.get(terminal_id)
    assert pty is not None
    pty.last_output_at = 0.0
    pty.last_input_at = 0.0
    pty.activity_ignore_until = 0.0
    before = pool_daemon.terminal_view(terminal_id)
    assert before is not None
    assert before["last_activity_at"] == 0.0

    pool_daemon.activate_terminal(terminal_id)
    time.sleep(0.2)
    after = pool_daemon.terminal_view(terminal_id)
    assert after is not None
    assert after["last_activity_at"] == 0.0

    # 用户提交后抑制立即结束，后续命令输出可以重新点亮工作状态。
    pty.write("\r")
    assert pty.activity_ignore_until == 0.0


def test_agent_stop_marks_offline_and_delete_removes_role(
    pool_daemon: service.Daemon,
) -> None:
    agent = _agent(pool_daemon, "reviewer")
    terminal_id = agent["terminal_id"]
    pty = pool_daemon.terminals.get(terminal_id)
    meta = pool_daemon.terminals.meta(terminal_id)
    assert pty is not None and meta is not None

    # X = 下线：主动 stop 后即使在 Windows 上退出码非 0，也必须是离线而不是报错。
    meta.closed = True
    pty.stop()
    view = pool_daemon.terminal_view(terminal_id)
    assert view is not None
    assert view["state"] == "stopped"

    # 垃圾桶 = 彻底删除：终端 + registry 记录都消失。
    assert pool_daemon.remove_terminal_for_role("reviewer") is True
    assert pool_daemon.terminals.get(terminal_id) is None
    assert pool_daemon.registry.get("reviewer") is not None
    assert pool_daemon.registry.delete("reviewer") is True
    assert pool_daemon.registry.get("reviewer") is None


def test_waiting_notice_holds_until_submit(pool_daemon: service.Daemon) -> None:
    """蓝色等待没有时间上限；输入字符不解除，回车/Ctrl+C 才解除。"""
    shell = _shell(pool_daemon)
    terminal_id = shell["terminal_id"]
    pty = pool_daemon.terminals.get(terminal_id)
    assert pty is not None
    pty.notify(PERMISSION_MATCHER, "等待确认")

    view = pool_daemon.terminal_view(terminal_id)
    assert view is not None
    assert view["waiting"] is True

    pty.write("x")
    view = pool_daemon.terminal_view(terminal_id)
    assert view is not None
    assert view["waiting"] is True

    pty.write("\r")
    view = pool_daemon.terminal_view(terminal_id)
    assert view is not None
    assert view["waiting"] is False
