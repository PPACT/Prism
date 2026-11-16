"""工作台一键启动：静默拉起 daemon，再静默启动 tauri dev。

由 `启动工作台.bat` 用 pythonw.exe 调用；所有子进程使用 CREATE_NO_WINDOW，
不会弹出控制台黑框。前端仍然是可见的 Tauri 窗口。
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import psutil  # type: ignore[import-untyped]  # psutil 未随包提供 stubs

ROOT = Path(__file__).resolve().parents[1]
HOME = ROOT / ".claude" / "orchestrator"
LEGACY_WORKSPACES_DB = ROOT / ".claude" / "workspaces.sqlite3"
LOG_DIR = ROOT / ".claude" / "logs"
FRONTEND_DIR = ROOT / "experiments" / "frontend-dashboard-tauri"
VENV_PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"


APP_NAME = "Prism"
APP_SLUG = "prism"
LEGACY_APP_NAME = "AgentSentinel"
LEGACY_APP_SLUG = LEGACY_APP_NAME.lower()


def env_value(name: str, default: str = "") -> str:
    """PRISM_* 优先，SENTINEL_* 兜底（迁移期兼容）。"""
    return os.environ.get(f"PRISM_{name}", os.environ.get(f"SENTINEL_{name}", default))


def user_data_dir() -> Path:
    override = env_value("DATA_DIR").strip()
    if override:
        return Path(override).expanduser()
    appdata = os.environ.get("APPDATA", "").strip()
    if appdata:
        return Path(appdata) / APP_NAME
    return Path.home() / f".{APP_SLUG}"


def legacy_user_data_dir() -> Path:
    """改名前的数据目录，仅用于首次迁移复制（不删除旧目录）。"""
    appdata = os.environ.get("APPDATA", "").strip()
    if appdata:
        return Path(appdata) / LEGACY_APP_NAME
    return Path.home() / f".{LEGACY_APP_SLUG}"


DATA_DIR = user_data_dir()
WORKSPACES_DB = DATA_DIR / "workspaces.sqlite3"
WORKSPACES_ROOT = DATA_DIR / "workspaces"


def resolve_workspaces_db() -> Path:
    if WORKSPACES_DB.exists():
        return WORKSPACES_DB
    for legacy in (LEGACY_WORKSPACES_DB, legacy_user_data_dir() / "workspaces.sqlite3"):
        if not legacy.exists():
            continue
        try:
            WORKSPACES_DB.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(legacy, WORKSPACES_DB)
            log("migrated legacy workspaces.sqlite3 to Prism data directory")
        except OSError:
            pass
        break
    return WORKSPACES_DB


CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_PROCESS_GROUP = 0x00000200


def log(message: str) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}\n"
    with (LOG_DIR / "workbench.log").open("a", encoding="utf-8") as stream:
        stream.write(line)


def daemon_healthy(home: Path = HOME) -> bool:
    try:
        info = json.loads((home / "daemon.json").read_text(encoding="utf-8"))
        url = f"http://{info['host']}:{info['port']}/health"
        with urllib.request.urlopen(url, timeout=2) as response:
            return int(response.status) == 200
    except (OSError, ValueError, KeyError, TimeoutError):
        return False


def stop_port_owner(port: int) -> None:
    """停掉占用端口的旧 Vite；只处理命令行含本项目或 vite 的进程。"""
    try:
        connections = psutil.net_connections(kind="inet")
    except (psutil.Error, OSError):
        return
    for connection in connections:
        if connection.status != psutil.CONN_LISTEN or connection.laddr.port != port:
            continue
        pid = connection.pid or 0
        if pid <= 0 or pid == os.getpid():
            continue
        try:
            process = psutil.Process(pid)
            command = " ".join(process.cmdline()).lower()
        except (psutil.Error, OSError):
            continue
        if str(ROOT).lower() in command or "vite" in command:
            log(f"stop stale process on port {port}: pid {pid}")
            try:
                process.kill()
            except (psutil.Error, OSError):
                pass


def start_daemon(home: Path = HOME, extra_env: dict[str, str] | None = None) -> None:
    env = os.environ.copy()
    env["PRISM_ORCH_HOME"] = str(home)
    env["PRISM_CONTROL_HOME"] = str(HOME)
    env["PRISM_WORKSPACES_DB"] = str(resolve_workspaces_db())
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    if extra_env:
        env.update(extra_env)
    python = str(VENV_PYTHON if VENV_PYTHON.exists() else sys.executable)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with (LOG_DIR / "daemon-launch.log").open("a", encoding="utf-8") as stream:
        completed = subprocess.run(
            [python, "-m", "tools.orchestrator", "daemon", "start", "--port", "0"],
            cwd=ROOT,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=stream,
            stderr=subprocess.STDOUT,
            creationflags=CREATE_NO_WINDOW,
            check=False,
        )
    if completed.returncode != 0:
        raise RuntimeError(f"daemon start failed: exit {completed.returncode}")
    log(f"daemon is healthy (home: {home})")


def start_active_workspace_daemon() -> None:
    """启动当前 active Workspace 的独立 daemon（控制面之外时）。"""
    db_path = resolve_workspaces_db()
    if not db_path.exists():
        return
    try:
        with sqlite3.connect(db_path) as connection:
            row = connection.execute(
                "SELECT id, project_path, daemon_home FROM workspaces "
                "WHERE active=1 ORDER BY id LIMIT 1"
            ).fetchone()
    except (sqlite3.Error, OSError):
        return
    if not row:
        return
    workspace_id, project_path, daemon_home = row
    home = (
        Path(str(daemon_home))
        if daemon_home
        else (HOME if int(workspace_id) == 1 else WORKSPACES_ROOT / str(workspace_id))
    )
    if home == HOME or daemon_healthy(home):
        return
    log(f"active workspace daemon offline; starting silently (home: {home})")
    start_daemon(
        home,
        {
            "PRISM_PROJECT_ROOT": str(project_path),
            "PRISM_WORKSPACE_ID": str(workspace_id),
        },
    )


def launch_frontend() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_stream = (LOG_DIR / "frontend.log").open("a", encoding="utf-8")
    subprocess.Popen(
        ["cmd.exe", "/c", "npm", "run", "tauri", "dev"],
        cwd=FRONTEND_DIR,
        stdin=subprocess.DEVNULL,
        stdout=log_stream,
        stderr=subprocess.STDOUT,
        creationflags=CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP,
        close_fds=True,
    )
    log_stream.close()
    log("frontend (tauri dev) launched")


def app_running() -> bool:
    for process in psutil.process_iter(["name"]):
        if (process.info.get("name") or "").lower() == "prism.exe":
            return True
    return False


def main() -> int:
    try:
        if daemon_healthy():
            log("daemon already running")
        else:
            log("daemon offline; starting silently")
            start_daemon()

        start_active_workspace_daemon()

        if app_running():
            log("frontend already running; skip launch")
            return 0

        stop_port_owner(1420)
        time.sleep(0.3)
        launch_frontend()
        return 0
    except Exception as error:  # noqa: BLE001 - 启动脚本需要把错误写进日志
        log(f"ERROR: {error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
