"""注册期身份复用：宿主进程没死，agent_id 就不准换。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock

import psutil  # type: ignore[import-untyped]
import pytest

from tools.orchestrator import service
from tools.orchestrator.registry import AgentRecord, utc_now

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows Job Object / PID")


def _daemon(tmp_path: Path) -> service.Daemon:
    return service.Daemon(tmp_path / "orch")


class _FakeProc:
    def __init__(self, pid: int, name: str, parent: _FakeProc | None = None) -> None:
        self.pid = pid
        self._name = name
        self._parent = parent

    def name(self) -> str:
        return self._name

    def parent(self) -> _FakeProc | None:
        return self._parent


def test_host_pid_skips_mcp_wrapper_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    host = _FakeProc(100, "claude.exe")
    uv = _FakeProc(200, "uv.exe", host)
    wrapper = _FakeProc(300, "python.exe", uv)
    mcp = _FakeProc(400, "python.exe", wrapper)
    monkeypatch.setattr(psutil, "Process", lambda pid: mcp)
    assert service._host_pid(400) == 100


def test_agent_id_ignores_started_at_changes() -> None:
    now = utc_now()
    first = AgentRecord(
        role="dev",
        pid=101,
        identity_pid=202,
        pgid=101,
        job_id="",
        status="registered",
        started_at=now,
        last_heartbeat=now,
        command="mcp_server",
        updated_at=now,
    )
    second = AgentRecord(
        role="dev",
        pid=303,
        identity_pid=202,
        pgid=303,
        job_id="",
        status="registered",
        started_at="2026-12-31T00:00:00.000000Z",
        last_heartbeat=now,
        command="mcp_server",
        updated_at=now,
    )
    assert service._agent_id(first) == service._agent_id(second)


def test_register_reuses_identity_while_host_alive(tmp_path: Path) -> None:
    daemon = _daemon(tmp_path)
    first = daemon.register(
        {
            "role": "dev",
            "pid": os.getpid(),
            "claude_pid": os.getpid(),
            "started_at": "2026-01-02T03:04:05.000000Z",
            "command": "mcp_server",
        }
    )
    # /clear 后 MCP 子进程换了 PID，但宿主 Claude 进程没换。
    second = daemon.register(
        {
            "role": "dev",
            "pid": os.getppid(),
            "claude_pid": os.getpid(),
            "started_at": "2026-11-15T12:00:00.000000Z",
            "command": "mcp_server",
        }
    )

    assert second["agent_id"] == first["agent_id"]
    assert second["started_at"] == first["started_at"]
    assert second["identity_pid"] == os.getpid()
    assert second["pid"] == os.getppid()
    assert daemon.resolve("dev") is not None


def test_register_issues_new_identity_after_host_death(tmp_path: Path) -> None:
    daemon = _daemon(tmp_path)
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        first = daemon.register(
            {"role": "dev", "pid": child.pid, "claude_pid": child.pid, "command": "mcp_server"}
        )
    finally:
        child.kill()
        child.wait(timeout=10)

    second = daemon.register(
        {"role": "dev", "pid": os.getpid(), "claude_pid": os.getpid(), "command": "mcp_server"}
    )
    assert second["agent_id"] != first["agent_id"]
    assert second["identity_pid"] == os.getpid()


def test_agent_cache_entries_cached_until_directory_changes(tmp_path: Path) -> None:
    daemon = _daemon(tmp_path)
    cache_dir = daemon.home / service.AGENT_CACHE_DIR
    cache_dir.mkdir(parents=True)
    (cache_dir / "a.json").write_text(json.dumps({"role": "dev"}), encoding="utf-8")

    first = daemon._agent_cache_entries()
    second = daemon._agent_cache_entries()
    assert first is second
    assert len(first) == 1

    (cache_dir / "b.json").write_text(json.dumps({"role": "doc"}), encoding="utf-8")
    os.utime(cache_dir, (time.time() + 5, time.time() + 5))
    assert len(daemon._agent_cache_entries()) == 2


def test_delete_is_new_life_and_cleans_pending_locks(tmp_path: Path) -> None:
    daemon = _daemon(tmp_path)
    first = daemon.register(
        {"role": "dev", "pid": os.getpid(), "claude_pid": os.getpid(), "command": "mcp_server"}
    )
    daemon.terminals.remember_session("dev", "old-session")
    pending = daemon.home / service.PENDING_DIR
    pending.mkdir(parents=True, exist_ok=True)
    lock = pending / f"dev_{os.getpid()}_123456.lock"
    lock.write_text("", encoding="utf-8")

    result = daemon.delete_agent_completely(1, "dev")

    assert result["generation"] == 1
    assert result["removed_locks"] == 1
    assert result["removed_session"] is True
    assert not lock.exists()
    assert daemon.terminals.session_for("dev") is None
    assert daemon.registry.get("dev") is None

    # 同名重建：身份锚点没变，但代数 +1，必须拿到全新 agent_id。
    second = daemon.register(
        {"role": "dev", "pid": os.getpid(), "claude_pid": os.getpid(), "command": "mcp_server"}
    )
    assert second["generation"] == 1
    assert second["agent_id"] != first["agent_id"]


def test_local_agent_starts_offline_and_revives_on_demand(tmp_path: Path) -> None:
    daemon = _daemon(tmp_path)
    workspace = daemon.active_workspace()
    daemon.workspaces.save_agent(
        {
            "workspace_id": int(workspace["id"]),
            "role": "reviewer",
            "wake_prompt_type": "text",
            "wake_prompt_content": "test",
            "command": "",
        }
    )
    daemon._register_workspace_agents()

    record = daemon.registry.get("reviewer")
    assert record is not None
    assert record.status == "stopped"
    # 即使 daemon 自己的 PID 活着，stopped 也必须显示 alive=false。
    assert daemon._agent_dict(record)["alive"] is False

    revived = daemon.revive_local_agent("reviewer")
    assert revived is not None
    assert revived["status"] == "registered"
    assert revived["alive"] is True


def test_revive_does_not_start_external_agents(tmp_path: Path) -> None:
    daemon = _daemon(tmp_path)
    daemon.register(
        {"role": "ext", "pid": os.getpid(), "claude_pid": os.getpid(), "command": "mcp_server"}
    )
    daemon.registry.set_status("ext", "stopped")
    assert daemon.revive_local_agent("ext") is None


def test_cache_reconcile_preserves_live_identity(tmp_path: Path) -> None:
    daemon = _daemon(tmp_path)
    now = utc_now()
    existing = AgentRecord(
        role="dev",
        pid=os.getpid(),
        identity_pid=os.getppid(),
        pgid=os.getpid(),
        job_id="",
        status="registered",
        started_at="2026-01-02T03:04:05.000000Z",
        last_heartbeat=now,
        command="workspace",
        updated_at=now,
        project=daemon.project,
    )
    daemon.registry.upsert(existing)
    before = service._agent_id(existing)

    cache_dir = daemon.home / service.AGENT_CACHE_DIR
    cache_dir.mkdir(parents=True)
    (cache_dir / "s1.json").write_text(
        json.dumps(
            {
                "session_id": "session-cache-1",
                "role": "dev",
                "project": "",
                "started_at": "2026-11-15T12:00:00.000000Z",
                "mcp_pid": os.getpid(),
                "claude_pid": os.getpid(),
                "updated_at": now,
            }
        ),
        encoding="utf-8",
    )

    daemon._reconcile_agent_cache()

    record = daemon.registry.get("dev")
    assert record is not None
    assert service._agent_id(record) == before
    assert record.identity_pid == os.getppid()
    assert record.started_at == "2026-01-02T03:04:05.000000Z"


def test_running_terminal_binding_follows_reused_identity(tmp_path: Path) -> None:
    daemon = _daemon(tmp_path)
    first = daemon.register(
        {"role": "dev", "pid": os.getpid(), "claude_pid": os.getpid(), "command": "mcp_server"}
    )
    pty = MagicMock()
    pty.status.return_value = {"running": True}
    daemon.terminals.install(pty, "t-binding")
    daemon.terminals.set_meta(
        service.TerminalMeta("t-binding", kind="agent", role="dev", agent_id="stale-id")
    )

    assert daemon._sync_terminal_binding("dev", first["agent_id"]) is True
    meta = daemon.terminals.meta("t-binding")
    assert meta is not None
    assert meta.agent_id == first["agent_id"]
    assert pty.agent_id == first["agent_id"]

    # 已停止的终端不复活旧绑定。
    pty.status.return_value = {"running": False}
    daemon.terminals.set_meta(
        service.TerminalMeta("t-binding", kind="agent", role="dev", agent_id="stale-id")
    )
    assert daemon._sync_terminal_binding("dev", first["agent_id"]) is False
