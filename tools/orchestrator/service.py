"""P1 最小 daemon：``127.0.0.1`` + token，常驻持有 Job 句柄与 registry。

CLI 只是 HTTP 客户端；daemon 退出（正常或崩溃）时 Job 句柄关闭，
``KILL_ON_JOB_CLOSE`` 会回收全部 worker 与子孙进程。
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import queue
import re
import secrets
import shlex
import shutil
import signal
import socket
import sys
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import FrameType
from typing import Any
from urllib.parse import parse_qs, urlparse

import psutil  # type: ignore[import-untyped]  # psutil 未随包提供 stubs

from tools.orchestrator import env as workbench_env
from tools.orchestrator.claude_hooks import (
    NOTIFICATION_PATH as CLAUDE_HOOK_NOTIFICATION_PATH,
)
from tools.orchestrator.claude_hooks import (
    PERMISSION_MATCHER,
)
from tools.orchestrator.claude_hooks import (
    write_settings as write_claude_hook_settings,
)
from tools.orchestrator.drivers.cwd_process import CwdProcessDriver, _normalize_path, _within
from tools.orchestrator.engines import (
    ENGINES,
    EngineAdapter,
    adapter_for,
    bracketed_paste,
    preflight,
)
from tools.orchestrator.launcher import (
    Launcher,
    kill_process_tree,
    process_alive,
    sweep_leftover_real_workers,
)
from tools.orchestrator.locking import (
    LOCK_SUFFIX,
    parse_lock_file_name,
    unlink_with_retry,
)
from tools.orchestrator.locking import (
    probe as probe_lock,
)
from tools.orchestrator.pty_manager import PtyManager
from tools.orchestrator.registry import AgentRecord, Registry, utc_now
from tools.orchestrator.router import (
    InvalidTransition,
    RoleBudgetExceeded,
    RoleBusy,
    RoleMismatch,
    Router,
    TaskNotFound,
    TaskStore,
    ack_timeout_seconds,
    lost_timeout_seconds,
    watchdog_seconds,
)
from tools.orchestrator.transcript import (
    USAGE_KEYS,
    newest_session,
    read_appended,
    read_history,
    session_activity,
    transcript_path,
)
from tools.orchestrator.workspaces import WorkspaceStore

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_HOST = "127.0.0.1"
APP_NAME = "Prism"
APP_SLUG = "prism"
LEGACY_APP_NAME = "AgentSentinel"
LEGACY_APP_SLUG = LEGACY_APP_NAME.lower()
_ROLE_PATTERN = re.compile(r"^[a-z0-9_-]+$")
DASHBOARD_REFRESH_ENV = "PRISM_ORCH_DASHBOARD_REFRESH_SECONDS"
DEFAULT_DASHBOARD_REFRESH_SECONDS = 0.0
SSE_PING_ENV = "PRISM_ORCH_SSE_PING_SECONDS"
DEFAULT_SSE_PING_SECONDS = 20.0
WATCH_DEBOUNCE_SECONDS = 0.15
LOG_TAIL_DEFAULT = 200
LOG_TAIL_MAX = 1000
LOG_POLL_SECONDS = 0.3
LOG_CHUNK_BYTES = 64 * 1024
REPORT_LINE_MAX = 4000
AGENT_CACHE_INTERVAL_ENV = "PRISM_ORCH_AGENT_CACHE_SECONDS"
DEFAULT_AGENT_CACHE_SECONDS = 30.0
AGENT_CACHE_DIR = "agents"
INSTANCE_LOCK_NAME = "daemon.lock"
SWEEP_WORKERS_ENV = "PRISM_ORCH_SWEEP_WORKERS"
WILD_CLEANUP_ALLOWED_EXES = frozenset({"node.exe", "python.exe", "pythonw.exe"})

AGENT_CACHE_TTL_SECONDS = 30 * 24 * 60 * 60
PENDING_DIR = "pending"
CLAUDE_HOOK_NOTICE_MESSAGE = "Claude Code 正在等待权限确认，请切换到原生终端处理"
HOOK_NOTICE_DEDUPE_SECONDS = 30.0


def agent_cache_seconds() -> float:
    raw = workbench_env.get(AGENT_CACHE_INTERVAL_ENV, "").strip()
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_AGENT_CACHE_SECONDS
    return value if value > 0 else DEFAULT_AGENT_CACHE_SECONDS


class ProjectMismatch(ValueError):
    """agent 上报的项目与 daemon 所属项目不一致。"""


@lru_cache(maxsize=32)
def _normalize_project(value: str | Path) -> str:
    return os.path.normcase(str(Path(value).resolve()))


def derive_project(home: Path) -> str:
    """daemon 所属项目：显式 env 优先；默认取 <项目>/.claude/orchestrator 的祖父目录。"""
    override = workbench_env.get("PROJECT_ROOT", "").strip()
    if override:
        return _normalize_project(override)
    resolved = home.resolve()
    if resolved.name == "orchestrator" and resolved.parent.name == ".claude":
        return _normalize_project(resolved.parent.parent)
    return _normalize_project(PROJECT_ROOT)


def user_data_dir() -> Path:
    """打包/多工作区运行时数据目录：默认 %APPDATA%/Prism。"""
    override = workbench_env.get("DATA_DIR", "").strip()
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


def control_daemon_home() -> Path:
    override = workbench_env.get("CONTROL_HOME", "").strip()
    if override:
        return Path(override).expanduser()
    return PROJECT_ROOT / ".claude" / "orchestrator"


def workspace_db_path(home: Path) -> Path:
    """全局配置库：默认在用户数据目录；隔离测试用独立 home。"""
    override = workbench_env.get("WORKSPACES_DB", "").strip()
    if override:
        return Path(override)
    resolved = home.resolve()
    default_home = (PROJECT_ROOT / ".claude" / "orchestrator").resolve()
    if resolved == default_home:
        return user_data_dir() / "workspaces.sqlite3"
    return home / "workspaces.sqlite3"


def ensure_workspace_db(home: Path) -> Path:
    """首次迁移：把旧的配置库复制到 Prism 数据目录（只复制，不删除旧目录）。"""
    path = workspace_db_path(home)
    legacy_candidates = (
        PROJECT_ROOT / ".claude" / "workspaces.sqlite3",
        legacy_user_data_dir() / "workspaces.sqlite3",
    )
    try:
        if not path.exists():
            for legacy in legacy_candidates:
                if legacy != path and legacy.exists():
                    path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(legacy, path)
                    break
    except OSError:
        pass
    return path


def _configure_stdio(*streams: Any) -> None:
    """daemon 入口：编码失败时用替换字符，避免日志写入中断或产生乱码。"""
    for stream in streams:
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(errors="replace")
        except (OSError, ValueError):
            continue


def dashboard_refresh_seconds() -> float:
    raw = workbench_env.get(DASHBOARD_REFRESH_ENV, "").strip()
    if not raw:
        return DEFAULT_DASHBOARD_REFRESH_SECONDS
    try:
        return max(0.0, float(raw))
    except ValueError:
        return DEFAULT_DASHBOARD_REFRESH_SECONDS


def sse_ping_seconds() -> float:
    raw = workbench_env.get(SSE_PING_ENV, "").strip()
    if not raw:
        return DEFAULT_SSE_PING_SECONDS
    try:
        return max(0.1, float(raw))
    except ValueError:
        return DEFAULT_SSE_PING_SECONDS


def worker_mode() -> str:
    return workbench_env.get("ORCH_WORKER_MODE", "dummy").strip().lower() or "dummy"


def format_agents_table(agents: list[dict[str, Any]]) -> str:
    header = (
        f"{'role':<10} {'pid':<8} {'status':<12} {'alive':<6} "
        f"{'last_heartbeat':<26} current_task_id"
    )
    lines = [header]
    if not agents:
        lines.append("暂无已注册 agent")
        return "\n".join(lines)
    for agent in agents:
        alive = "在线" if agent.get("alive") else "已停止"
        lines.append(
            f"{agent.get('role', '')!s:<10} {agent.get('pid', '')!s:<8} "
            f"{agent.get('status', '')!s:<12} {alive:<6} "
            f"{agent.get('last_heartbeat') or '-'!s:<26} "
            f"{agent.get('current_task_id') or '-'!s}"
        )
    return "\n".join(lines)


def format_banner(daemon: Daemon, port: int) -> str:
    return "\n".join(
        [
            "Prism 编排中控已启动",
            f"  pid:         {os.getpid()}",
            f"  listen:      {daemon.host}:{port}",
            f"  home:        {daemon.home}",
            f"  worker mode: {worker_mode()}",
            f"  started at:  {daemon.started_at}",
        ]
    )


def render_dashboard(daemon: Daemon, port: int) -> str:
    agents: list[dict[str, Any]] = daemon.status()["agents"]
    return format_banner(daemon, port) + "\n\n" + format_agents_table(agents)


def supports_ansi_clear() -> bool:
    """终端支持 ANSI 清屏时返回 True；否则降级为分隔线重打印。"""
    if not sys.stdout.isatty():
        return False
    if os.name != "nt":
        return True
    try:
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        enabled = 0x0004  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
        if mode.value & enabled:
            return True
        return bool(kernel32.SetConsoleMode(handle, mode.value | enabled))
    except (AttributeError, OSError):
        return False


def _render_agents_update(snapshot: dict[str, Any], clear: bool) -> None:
    if clear:
        sys.stdout.write("\033[2J\033[H")
    else:
        sys.stdout.write("\n" + "-" * 60 + "\n")
    sys.stdout.write(f"[{utc_now()}] 已注册 agent\n")
    sys.stdout.write(format_agents_table(snapshot["agents"]) + "\n")
    sys.stdout.flush()


def _dashboard_loop(daemon: Daemon, port: int, refresh: float, stop: threading.Event) -> None:
    del port  # 端口只在横幅里展示；周期刷新只重绘 agents 表
    clear = supports_ansi_clear()
    while not stop.wait(refresh):
        _render_agents_update(daemon.status(), clear)


def _agents_projection(snapshot: dict[str, Any]) -> tuple[tuple[str, str, bool, str], ...]:
    return tuple(
        sorted(
            (
                str(agent.get("role", "")),
                str(agent.get("status", "")),
                bool(agent.get("alive")),
                str(agent.get("current_task_id") or ""),
            )
            for agent in snapshot.get("agents", [])
        )
    )


def _watch_loop(daemon: Daemon, stop: threading.Event, show_heartbeat: bool) -> None:
    clear = supports_ansi_clear()
    previous = _agents_projection(daemon.status())
    last_seen = daemon.revision
    while not stop.is_set():
        revision = daemon.wait_for_change(last_seen)
        if stop.is_set():
            break
        if revision == last_seen:
            continue
        snapshot = daemon.status()
        projection = _agents_projection(snapshot)
        last_seen = revision
        if show_heartbeat or projection != previous:
            _render_agents_update(snapshot, clear)
            previous = projection


def _offer_latest(target: queue.Queue[dict[str, Any]], frame: dict[str, Any]) -> None:
    """订阅者队列只保留最新帧：慢客户端不阻塞发布方。"""
    try:
        target.put_nowait(frame)
        return
    except queue.Full:
        pass
    try:
        target.get_nowait()
    except queue.Empty:
        pass
    try:
        target.put_nowait(frame)
    except queue.Full:
        pass


def _decode_log_line(raw: bytes) -> str:
    """日志行解码：UTF-8 优先；兼容旧版 cp936/GBK 日志；最后用替换字符兜底。"""
    for encoding in ("utf-8", "gbk"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _split_log_chunk(pending: bytes, chunk: bytes) -> tuple[list[str], bytes]:
    """按换行切分日志块，返回已解码的完整行与未完成的尾行字节。"""
    parts = (pending + chunk).split(b"\n")
    rest = parts.pop()
    return [_decode_log_line(line.rstrip(b"\r")) for line in parts], rest


def _tail_lines(path: Path, limit: int) -> list[str]:
    if limit <= 0:
        return []
    try:
        with path.open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            size = stream.tell()
            read_size = min(size, LOG_CHUNK_BYTES)
            stream.seek(size - read_size)
            data = stream.read(read_size)
    except OSError:
        return []
    lines = data.split(b"\n")
    if read_size < size and lines:
        # 读窗口从文件中间开始，首段是残行，丢弃以免按 GBK 误判。
        lines.pop(0)
    if lines and lines[-1] == b"":
        lines.pop()
    return [_decode_log_line(line.rstrip(b"\r")) for line in lines][-limit:]


def default_home() -> Path:
    override = workbench_env.get("ORCH_HOME", "").strip()
    if override:
        return Path(override).resolve()
    return PROJECT_ROOT / ".claude" / "orchestrator"


def load_or_create_token(home: Path) -> str:
    home.mkdir(parents=True, exist_ok=True)
    path = home / "token"
    try:
        token = path.read_text(encoding="utf-8").strip()
    except OSError:
        token = ""
    if not token:
        token = secrets.token_hex(24)
        temp = path.with_name(path.name + ".tmp")
        temp.write_text(token + "\n", encoding="utf-8")
        os.replace(temp, path)
    return token


def write_daemon_info(home: Path, info: dict[str, Any]) -> None:
    path = home / "daemon.json"
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
    os.replace(temp, path)


def _read_daemon_info(home: Path) -> dict[str, Any] | None:
    try:
        data = json.loads((home / "daemon.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _port_in_use(host: str, port: int, timeout: float = 0.3) -> bool:
    """固定端口启动前的占用检测；端口 0（临时端口）不检查。"""
    if port <= 0:
        return False
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _current_process_identity() -> tuple[int, float]:
    pid = os.getpid()
    try:
        created = float(psutil.Process(pid).create_time())
    except (psutil.Error, OSError):
        created = 0.0
    return pid, created


def _lock_holder_alive(path: Path) -> bool:
    """锁文件记录 pid + create_time；PID 被复用（create_time 不同）视为残留。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(data, dict):
        return False
    pid = data.get("pid")
    created = data.get("create_time")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    if isinstance(created, bool) or not isinstance(created, (int, float)) or created <= 0:
        return False
    try:
        process = psutil.Process(pid)
        return bool(
            abs(process.create_time() - float(created)) <= 0.05
            and process.is_running()
            and process.status() != psutil.STATUS_ZOMBIE
        )
    except (psutil.Error, OSError):
        return False


def _acquire_instance_lock(home: Path) -> bool:
    """同一 home 只允许一个 daemon；残留锁按 pid + create_time 判定后回收。"""
    home.mkdir(parents=True, exist_ok=True)
    path = home / INSTANCE_LOCK_NAME
    pid, created = _current_process_identity()
    payload = json.dumps({"pid": pid, "create_time": created})
    for _ in range(3):
        try:
            handle = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if _lock_holder_alive(path):
                return False
            try:
                path.unlink()
            except OSError:
                return False
            continue
        try:
            os.write(handle, payload.encode("ascii"))
        finally:
            os.close(handle)
        return True
    return False


def _release_instance_lock(home: Path) -> None:
    path = home / INSTANCE_LOCK_NAME
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(data, dict) or data.get("pid") != os.getpid():
        return
    try:
        path.unlink()
    except OSError:
        return


def _pty_environment() -> dict[str, str]:
    """PTY 里必须是独立的 claude 会话，不能继承外层 Claude Code 的子会话标记。

    同时固定 prompt cache 相关环境：
    - ``ENABLE_PROMPT_CACHING_1H=1``：把默认 5 分钟 TTL 拉长到 1 小时，长命令/挂机后
      前缀仍可命中（CC >= 2.1.108）。
    - ``ENABLE_TOOL_SEARCH=true``：自定义 ``ANTHROPIC_BASE_URL``（如 cc-switch 代理）下
      CC 会停止延迟加载工具、每轮重新内联完整工具集，导致缓存前缀从 byte 0 失配。
    - 清除 ``FORCE_PROMPT_CACHING_5M`` / ``DISABLE_PROMPT_CACHING*``：用户环境里的
      这两个变量分别会强制 5 分钟 TTL 和彻底关闭缓存，必须让 1h 生效。
    - ``CLAUDE_CODE_DISABLE_GIT_INSTRUCTIONS=1``：从系统提示词中移除 git status
      快照（编辑文件即变化，会把 DeepSeek 的前缀缓存从该处整段打断）。
    """
    env = os.environ.copy()
    for key in list(env):
        if (
            key in {"CLAUDECODE", "CLAUDE_PID"}
            or key.startswith("CLAUDE_CODE_")
            and key != "CLAUDE_CODE_GIT_BASH_PATH"
        ):
            env.pop(key, None)
    for key in list(env):
        if key.startswith("DISABLE_PROMPT_CACHING") or key == "FORCE_PROMPT_CACHING_5M":
            env.pop(key, None)
    env["ENABLE_PROMPT_CACHING_1H"] = "1"
    env["ENABLE_TOOL_SEARCH"] = "true"
    env["CLAUDE_CODE_DISABLE_GIT_INSTRUCTIONS"] = "1"
    return env


def _should_sweep_workers() -> bool:
    """pytest 进程内禁用真实清扫，避免测试误杀宿主机进程。"""
    if workbench_env.get(SWEEP_WORKERS_ENV, "1").strip() == "0":
        return False
    return not os.environ.get("PYTEST_CURRENT_TEST")


def _role(payload: dict[str, Any]) -> str:
    value = payload.get("role")
    if not isinstance(value, str) or not value.strip():
        raise ValueError("role is required")
    role = value.strip().casefold()
    if _ROLE_PATTERN.fullmatch(role) is None:
        raise ValueError(f"invalid role: {role}")
    return role


def _identity_pid(record: AgentRecord) -> int:
    """身份锚点：优先宿主进程，旧记录/本地记录回退到当前 pid。"""
    return record.identity_pid if record.identity_pid > 0 else record.pid


def _identity_alive(record: AgentRecord | None) -> bool:
    if record is None:
        return False
    pid = _identity_pid(record)
    return pid > 0 and process_alive(pid)


_HOST_WRAPPER_NAMES = {
    "uv",
    "uv.exe",
    "python",
    "python.exe",
    "pythonw.exe",
    "python3.exe",
    "cmd",
    "cmd.exe",
    "powershell",
    "powershell.exe",
    "pwsh",
    "pwsh.exe",
    "conhost.exe",
}


def _host_pid(pid: int) -> int:
    """从 MCP/PTY 进程向上跳过包装链，找到跨 /clear 稳定的宿主进程。"""
    if pid <= 0:
        return 0
    try:
        proc = psutil.Process(pid)
        for _ in range(8):
            parent = proc.parent()
            if parent is None:
                return 0
            try:
                name = (parent.name() or "").casefold()
            except psutil.Error:
                name = ""
            if name not in _HOST_WRAPPER_NAMES:
                return int(parent.pid)
            proc = parent
    except (psutil.Error, OSError):
        return 0
    return 0


def _agent_id(record: AgentRecord) -> str:
    """注册期唯一标识：项目 + 角色 + 身份锚点 PID + 重建代数。

    不再混入 ``started_at``：``/clear`` 后 MCP 子进程重建、启动时间变化，
    但宿主进程未换人，身份证必须保持不变；只有身份进程死亡或角色被物理删除
    后才发新证。
    """
    raw = f"{record.project}|{record.role}|{_identity_pid(record)}|{record.generation}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _optional_role(payload: dict[str, Any]) -> str:
    value = payload.get("role")
    return value.strip().casefold() if isinstance(value, str) else ""


def _optional_agent_id(payload: dict[str, Any]) -> str:
    value = payload.get("agent_id")
    return value.strip() if isinstance(value, str) else ""


DEFAULT_TOKEN_BUDGET = 200_000
# 统一的离线状态口径：左侧灯、对话锁定、/status 的 alive 必须共用。
_OFFLINE_STATUSES = ("stopped", "lost", "failed", "pending")
DEFAULT_HANDOFF_PROMPT = "上下文即将满，请立即更新 STATUS 并准备交接。"
DEFAULT_HANDOFF_TIMEOUT_SECONDS = 180.0
HANDOFF_RELOCATE_SECONDS = 20.0


def handoff_prompt() -> str:
    return workbench_env.get("HANDOFF_PROMPT", "").strip() or DEFAULT_HANDOFF_PROMPT


def handoff_timeout_seconds() -> float:
    raw = workbench_env.get("HANDOFF_TIMEOUT_SECONDS", "").strip()
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_HANDOFF_TIMEOUT_SECONDS
    return value if value > 0 else DEFAULT_HANDOFF_TIMEOUT_SECONDS


def wake_inject_delay_seconds() -> float:
    raw = workbench_env.get("WAKE_INJECT_DELAY_SECONDS", "").strip()
    try:
        value = float(raw)
    except ValueError:
        return 8.0
    return value if value >= 0 else 8.0


def _transcript_offset(project: str, session: str) -> int:
    path = transcript_path(project, session)
    if path is None:
        return 0
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _inject_prompt(pty: Any, text: str) -> None:
    payload = bracketed_paste(text) if "\n" in text else text
    pty.write(payload + "\r")


def _query_role(query: dict[str, list[str]]) -> str:
    value = (query.get("role") or [""])[0]
    return value.strip().casefold() if isinstance(value, str) else ""


def _query_agent_id(query: dict[str, list[str]]) -> str:
    value = (query.get("agent_id") or [""])[0]
    return value.strip() if isinstance(value, str) else ""


def _int(value: object) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return 0


def _str(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _unlink(path: Path) -> bool:
    try:
        path.unlink()
    except OSError:
        return False
    return True


def _cache_expired(data: dict[str, Any], path: Path) -> bool:
    """角色缓存只在 TTL 到期后删除；进程死亡不删，留给 --resume 复用。"""
    raw = _str(data.get("updated_at"))
    timestamp: datetime | None = None
    if raw:
        try:
            timestamp = datetime.strptime(raw, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
        except ValueError:
            timestamp = None
    if timestamp is None:
        try:
            timestamp = datetime.fromtimestamp(path.stat().st_mtime, UTC)
        except OSError:
            return False
    return datetime.now(UTC) - timestamp > timedelta(seconds=AGENT_CACHE_TTL_SECONDS)


DEFAULT_TERMINAL_ID = "1"

# P0 PTY 池 Spike：feature flag 默认关，旧单终端行为完全不变。
TERMINAL_POOL_ENV = "PRISM_TERMINAL_POOL"
TERMINAL_POOL_LIMIT_ENV = "PRISM_TERMINAL_POOL_LIMIT"
TERMINAL_POOL_TTL_ENV = "PRISM_TERMINAL_POOL_TTL_SECONDS"
TERMINAL_POOL_MEMORY_ENV = "PRISM_TERMINAL_POOL_MEMORY_PERCENT"
SHELL_COMMAND_ENV = "PRISM_TERMINAL_SHELL"
DEFAULT_TERMINAL_POOL_LIMIT = 6
DEFAULT_TERMINAL_POOL_TTL_SECONDS = 3_600.0
DEFAULT_TERMINAL_POOL_MEMORY_PERCENT = 80.0
MAX_TERMINAL_POOL_LIMIT = 16


def terminal_pool_enabled() -> bool:
    return workbench_env.get(TERMINAL_POOL_ENV, "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = workbench_env.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(minimum, min(maximum, value))


def terminal_pool_limit() -> int:
    return _env_int(
        TERMINAL_POOL_LIMIT_ENV, DEFAULT_TERMINAL_POOL_LIMIT, 1, MAX_TERMINAL_POOL_LIMIT
    )


def terminal_pool_ttl_seconds() -> float:
    raw = workbench_env.get(TERMINAL_POOL_TTL_ENV, "").strip()
    if not raw:
        return DEFAULT_TERMINAL_POOL_TTL_SECONDS
    try:
        return max(30.0, float(raw))
    except ValueError:
        return DEFAULT_TERMINAL_POOL_TTL_SECONDS


def terminal_pool_memory_percent() -> float:
    raw = workbench_env.get(TERMINAL_POOL_MEMORY_ENV, "").strip()
    if not raw:
        return DEFAULT_TERMINAL_POOL_MEMORY_PERCENT
    try:
        return max(1.0, min(100.0, float(raw)))
    except ValueError:
        return DEFAULT_TERMINAL_POOL_MEMORY_PERCENT


def memory_percent() -> float:
    """系统内存占用；测试可 monkeypatch。"""
    try:
        return float(psutil.virtual_memory().percent)
    except Exception:  # noqa: BLE001 - 内存探测失败不应阻断终端
        return 0.0


def shell_command() -> tuple[str, ...]:
    raw = workbench_env.get(SHELL_COMMAND_ENV, "").strip()
    if raw:
        return tuple(shlex.split(raw))
    if os.name == "nt":
        return ("powershell.exe", "-NoLogo", "-NoProfile")
    return ("bash",)


def new_terminal_id() -> str:
    return f"t-{uuid.uuid4().hex[:8]}"


@dataclass
class TerminalMeta:
    """P0 池元数据（内存态；P1 才落盘成正式数据模型）。"""

    terminal_id: str
    kind: str = "agent"  # legacy | shell | agent
    role: str = ""
    agent_id: str = ""
    command: str = ""
    created_at: float = field(default_factory=time.monotonic)
    last_active_at: float = field(default_factory=time.monotonic)
    pinned: bool = False
    busy: bool = False
    failed: bool = False
    failed_reason: str = ""
    owns_agent: bool = False
    closed: bool = False

    def touch(self) -> None:
        self.last_active_at = time.monotonic()

    def state(self, pty: PtyManager) -> str:
        if self.failed:
            return "failed"
        status = pty.status()
        if status.get("running"):
            return "running"
        if self.closed:
            return "stopped"
        exit_status = status.get("exit_status")
        # 非 0 退出视为异常；主动 stop 会先置 closed，不会误报红色。
        if exit_status not in (None, 0):
            return "failed"
        return "stopped"

    def to_dict(self, pty: PtyManager) -> dict[str, Any]:
        status = pty.status()
        return {
            "terminal_id": self.terminal_id,
            "kind": self.kind,
            "role": self.role,
            "agent_id": self.agent_id,
            "command": self.command,
            "state": self.state(pty),
            "pid": status.get("pid") or 0,
            "session_id": status.get("session_id") or "",
            "created_at": self.created_at,
            "last_active_at": self.last_active_at,
            "pinned": self.pinned,
            "busy": self.busy,
            "failed_reason": self.failed_reason,
        }


class TerminalManager:
    """终端代理层：业务只通过它拿 PtyManager。

    v1 只管理一个终端（id="1"），但 API/标识已按多终端设计；未来扩成池时
    只替换本类内部实现，调用方保持不变。
    """

    def __init__(
        self,
        factory: Callable[[str, str | None, str], PtyManager],
        sessions_path: str | os.PathLike[str] | None = None,
    ) -> None:
        self._factory = factory
        self._lock = threading.RLock()
        self._terminals: dict[str, PtyManager] = {}
        self._active_id = DEFAULT_TERMINAL_ID
        self._sessions_path = str(sessions_path) if sessions_path is not None else None
        self._sessions: dict[str, str] = self._load_sessions()
        self._signature: tuple[Any, ...] = ()
        self._terminals[self._active_id] = self._factory("", None, "")
        self._meta: dict[str, TerminalMeta] = {
            self._active_id: TerminalMeta(self._active_id, kind="legacy", pinned=True)
        }

    def _load_sessions(self) -> dict[str, str]:
        """角色 → session 持久化；daemon 重启后切回仍能续接同一会话。"""
        if not self._sessions_path:
            return {}
        try:
            data = json.loads(Path(self._sessions_path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {
            str(role): str(session)
            for role, session in data.items()
            if isinstance(role, str) and isinstance(session, str) and session.strip()
        }

    def _save_sessions_locked(self) -> None:
        if not self._sessions_path:
            return
        path = Path(self._sessions_path)
        try:
            temp = f"{self._sessions_path}.tmp"
            Path(temp).write_text(
                json.dumps(self._sessions, ensure_ascii=False, sort_keys=True),
                encoding="utf-8",
            )
            os.replace(temp, path)
        except OSError:
            pass

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    @property
    def active_id(self) -> str:
        return self._active_id

    @property
    def signature(self) -> tuple[Any, ...]:
        return self._signature

    @signature.setter
    def signature(self, value: tuple[Any, ...]) -> None:
        self._signature = value

    def active(self) -> PtyManager:
        with self._lock:
            return self._terminals[self._active_id]

    def get(self, terminal_id: str | None = None) -> PtyManager | None:
        with self._lock:
            key = (terminal_id or "").strip() or self._active_id
            return self._terminals.get(key)

    def install(self, pty: PtyManager, terminal_id: str | None = None) -> PtyManager:
        with self._lock:
            key = (terminal_id or "").strip() or self._active_id
            self._terminals[key] = pty
            return pty

    def meta(self, terminal_id: str) -> TerminalMeta | None:
        with self._lock:
            return self._meta.get(terminal_id)

    def set_meta(self, meta: TerminalMeta) -> None:
        with self._lock:
            self._meta[meta.terminal_id] = meta

    def list_meta(self) -> list[TerminalMeta]:
        with self._lock:
            return list(self._meta.values())

    def activate(self, terminal_id: str) -> bool:
        with self._lock:
            pty = self._terminals.get(terminal_id)
            if pty is None:
                return False
            self._active_id = terminal_id
            pty.suppress_activity(1.0)
            meta = self._meta.get(terminal_id)
            if meta is not None:
                meta.touch()
            return True

    def touch(self, terminal_id: str) -> None:
        with self._lock:
            meta = self._meta.get(terminal_id)
            if meta is not None:
                meta.touch()

    def count(self) -> int:
        with self._lock:
            return len(self._terminals)

    def pool_count(self) -> int:
        """只统计受管池终端；旧默认终端占位不占上限。"""
        with self._lock:
            return sum(1 for meta in self._meta.values() if meta.kind in ("shell", "agent"))

    def remove(self, terminal_id: str) -> bool:
        """停止并删除一个池终端；默认终端 "1" 不允许删除。"""
        if terminal_id == DEFAULT_TERMINAL_ID:
            return False
        with self._lock:
            pty = self._terminals.pop(terminal_id, None)
            self._meta.pop(terminal_id, None)
            if pty is not None:
                pty.stop()
            if self._active_id == terminal_id:
                self._active_id = (
                    DEFAULT_TERMINAL_ID
                    if DEFAULT_TERMINAL_ID in self._terminals
                    else next(iter(self._terminals), DEFAULT_TERMINAL_ID)
                )
            return pty is not None

    def find_by_session(self, session_id: str) -> PtyManager | None:
        if not session_id:
            return None
        with self._lock:
            candidates = [
                pty for pty in self._terminals.values() if pty.session_id == session_id and pty.role
            ]
        for pty in candidates:
            if pty.status().get("running"):
                return pty
        return candidates[0] if candidates else None

    def find_by_role(self, role: str) -> PtyManager | None:
        role = role.strip()
        if not role:
            return None
        with self._lock:
            candidates = [pty for pty in self._terminals.values() if pty.role == role]
        for pty in candidates:
            if pty.status().get("running"):
                return pty
        return candidates[0] if candidates else None

    def terminals_for_role(self, role: str) -> list[tuple[str, PtyManager]]:
        role = role.strip()
        if not role:
            return []
        with self._lock:
            return [(tid, pty) for tid, pty in self._terminals.items() if pty.role == role]

    def status_for(self, terminal_id: str) -> dict[str, Any] | None:
        with self._lock:
            pty = self._terminals.get(terminal_id)
            meta = self._meta.get(terminal_id)
        if pty is None:
            return None
        data = pty.status()
        data["terminal_id"] = terminal_id
        if meta is not None:
            data.update(meta.to_dict(pty))
        else:
            data["state"] = "running" if data.get("running") else "stopped"
        return data

    def list_status(self) -> list[dict[str, Any]]:
        with self._lock:
            ids = list(self._terminals)
        ids.sort(key=lambda tid: (tid != DEFAULT_TERMINAL_ID, tid))
        statuses = [self.status_for(tid) for tid in ids]
        return [status for status in statuses if status is not None]

    def evict_lru(self, protected: str = "") -> str | None:
        """回收最久未活动的空闲 Shell；Agent 终端是 pin 的，不参与。"""
        with self._lock:
            candidates = [
                meta
                for tid, meta in self._meta.items()
                if tid != protected
                and tid != DEFAULT_TERMINAL_ID
                and meta.kind == "shell"
                and not meta.pinned
                and not meta.busy
                and tid in self._terminals
            ]
            if not candidates:
                return None
            victim = min(candidates, key=lambda meta: meta.last_active_at)
        self.remove(victim.terminal_id)
        return victim.terminal_id

    def sweep(self, ttl_seconds: float) -> list[str]:
        """回收空闲超时的 Shell；返回被回收的 terminal_id。"""
        now = time.monotonic()
        with self._lock:
            expired = [
                meta
                for tid, meta in self._meta.items()
                if tid != DEFAULT_TERMINAL_ID
                and meta.kind == "shell"
                and not meta.pinned
                and not meta.busy
                and tid in self._terminals
                and now - meta.last_active_at >= ttl_seconds
            ]
        removed: list[str] = []
        for meta in expired:
            if self.remove(meta.terminal_id):
                removed.append(meta.terminal_id)
        return removed

    def reset(self) -> PtyManager:
        with self._lock:
            for pty in self._terminals.values():
                pty.stop()
            self._terminals.clear()
            self._meta.clear()
            self._sessions.clear()
            self._save_sessions_locked()
            self._active_id = DEFAULT_TERMINAL_ID
            self._terminals[self._active_id] = self._factory("", None, "")
            self._meta[self._active_id] = TerminalMeta(self._active_id, kind="legacy", pinned=True)
            return self._terminals[self._active_id]

    def stop_all(self) -> None:
        with self._lock:
            for pty in self._terminals.values():
                pty.stop()

    def session_for(self, role: str) -> str | None:
        with self._lock:
            return self._sessions.get(role)

    def remember_session(self, role: str, session: str) -> None:
        with self._lock:
            if role and session:
                self._sessions[role] = session
                self._save_sessions_locked()

    def forget_role(self, role: str) -> bool:
        """物理删除角色时清掉 role→session 映射；重建必须拿全新 session。"""
        role = role.strip()
        if not role:
            return False
        with self._lock:
            if role not in self._sessions:
                return False
            del self._sessions[role]
            self._save_sessions_locked()
            return True

    def remember_current_session(self) -> None:
        pty = self.active()
        if pty.role and pty.session_id:
            self.remember_session(pty.role, pty.session_id)


class Daemon:
    def __init__(self, home: Path, host: str = DEFAULT_HOST, port: int = 0) -> None:
        self.home = home
        self.project = derive_project(home)
        self.host = host
        self.port = port
        self.token = load_or_create_token(home)
        self.registry = Registry(home / "registry.sqlite3")
        self.launcher = Launcher(home)
        self.store = TaskStore(home / "registry.sqlite3")
        self.router = Router(
            store=self.store,
            registry=self.registry,
            launcher=self.launcher,
            token=self.token,
            project=self.project,
        )
        self.wild_driver = CwdProcessDriver()
        pty_override = workbench_env.get("PTY_COMMAND", "").strip()
        self._pty_command = tuple(shlex.split(pty_override)) if pty_override else None
        self._pty_session_override = workbench_env.get("PTY_SESSION_ID", "").strip() or None
        self._pty_env = _pty_environment()
        self._hook_notice_lock = threading.Lock()
        self._hook_notice_seen: dict[tuple[str, str], float] = {}
        self._wake_lock = threading.Lock()
        self._wake_injected: set[str] = set()
        self._bound_workspace_id = _int(workbench_env.get("WORKSPACE_ID", ""))
        db_path = ensure_workspace_db(home)
        is_global_db = db_path == user_data_dir() / "workspaces.sqlite3"
        self.workspaces = WorkspaceStore(
            db_path,
            control_home=control_daemon_home() if is_global_db else None,
        )
        self.workspaces.seed_default(self.project)
        self.terminals = TerminalManager(
            lambda role, session, agent_id: self._new_pty(
                role,
                session if session is not None else self._pty_session_override,
                agent_id,
            ),
            sessions_path=self.home / "pty_sessions.json",
        )
        self._handoff_lock = threading.Lock()
        self._handoff: dict[str, Any] = {"active": False, "phase": "idle", "message": ""}
        # 配置角色（含 local）进入 registry，左侧列表按配置动态展示。
        self._register_workspace_agents()
        self.ack_timeout = ack_timeout_seconds()
        self.lost_seconds = lost_timeout_seconds()
        self.watchdog_seconds = watchdog_seconds()
        self._last_watchdog = 0.0
        self._last_agent_cache = 0.0
        self._agent_cache_lock = threading.Lock()
        self._agent_delete_lock = threading.Lock()
        # (目录 mtime, 读取时刻, [(路径, 解析后的缓存)])：避免每次快照重复读全部缓存。
        self._agent_cache_snapshot: tuple[int, float, list[tuple[Path, dict[str, Any]]]] = (
            -1,
            0.0,
            [],
        )
        self.server: ThreadingHTTPServer | None = None
        self.started_at = utc_now()
        self._stopping = threading.Event()
        self._stop_monitor = threading.Event()
        self._stop_dashboard = threading.Event()
        self._revision = 0
        self._condition = threading.Condition()
        self._subscribers: list[queue.Queue[dict[str, Any]]] = []
        self.router.startup_recovery()
        self.reconcile_pending_agents()

    def _register_workspace_agents(self) -> None:
        """把当前 Workspace 的配置角色（含 local）注册为本地 Agent。

        外部 / 托管 Agent 存活时优先，不覆盖；本地记录沿用 started_at，
        避免保存配置或重启后 agent_id 漂移。
        """
        workspace = self.active_workspace()
        workspace_id = int(workspace.get("id") or 0)
        roles = {
            str(agent.get("role") or "").strip().casefold()
            for agent in self.workspaces.list_agents(workspace_id)
        }
        if self._bound_workspace_id > 0:
            # 独立工作区自带本地终端 Agent，保证空 registry 也能直接跑 CLI。
            roles.add("local")
        roles.discard("")
        now = utc_now()
        for role in sorted(roles):
            existing = self.registry.get(role)
            if (
                existing is not None
                and existing.status not in _OFFLINE_STATUSES
                and existing.pid != os.getpid()
                and (_identity_alive(existing) or process_alive(existing.pid))
            ):
                # 外部 MCP / 托管 worker 存活，保持其身份。
                continue
            live_pty = any(
                pty.status().get("running") for _tid, pty in self.terminals.terminals_for_role(role)
            )
            started = (
                existing.started_at
                if existing is not None and existing.command == "workspace"
                else now
            )
            pid = os.getpid() if live_pty else 0
            self.registry.upsert(
                AgentRecord(
                    role=role,
                    pid=pid,
                    identity_pid=pid,
                    generation=self.registry.generation(role),
                    pgid=pid,
                    job_id="",
                    # 重启后默认灰显离线，等用户点击/发消息再懒加载，不假设进程还活着。
                    status="registered" if live_pty else "stopped",
                    started_at=started,
                    last_heartbeat=now,
                    command="workspace",
                    updated_at=now,
                    project=self.project,
                )
            )
        for record in self.registry.list():
            if (
                record.command == "workspace"
                and record.role not in roles
                and record.status != "stopped"
            ):
                self.registry.set_status(record.role, "stopped")

    def active_workspace(self) -> dict[str, Any]:
        if self._bound_workspace_id > 0:
            bound = self.workspaces.get(self._bound_workspace_id)
            if bound is not None:
                return bound
        workspace = self.workspaces.active()
        if workspace is None:
            workspace = self.workspaces.seed_default(self.project)
        return workspace

    @staticmethod
    def _workspace_signature(workspace: dict[str, Any], command_text: str = "") -> tuple[Any, ...]:
        return (
            int(workspace.get("id") or 0),
            str(workspace.get("project_path") or ""),
            command_text or str(workspace.get("command") or ""),
            str(workspace.get("engine") or ""),
        )

    def workspace_budget(self) -> int:
        raw = workbench_env.get("TOKEN_BUDGET", "").strip()
        if raw:
            try:
                value = int(raw)
            except ValueError:
                value = 0
            if value > 0:
                return value
        workspace = self.active_workspace()
        try:
            value = int(workspace.get("budget_tokens") or DEFAULT_TOKEN_BUDGET)
        except (TypeError, ValueError):
            value = DEFAULT_TOKEN_BUDGET
        return max(1, value)

    def transcript_project(self) -> str:
        return str(self.active_workspace().get("project_path") or self.project)

    def pty_preflight(self, role: str = "") -> dict[str, Any]:
        workspace = self.active_workspace()
        project = str(workspace.get("project_path") or self.project)
        if self._pty_command:
            return preflight(" ".join(self._pty_command), project)
        return preflight(self._effective_command(workspace, role), project)

    def reset_pty(self) -> None:
        with self.terminals.lock:
            self.terminals.reset()

    def activate_workspace(self, workspace_id: int) -> dict[str, Any]:
        workspace = self.workspaces.activate(workspace_id)
        # 池模式下工作区切换保留进程本身（P0 拍板），只切配置。
        if not self.pool_enabled():
            self.reset_pty()
        self._register_workspace_agents()
        self._publish()
        return workspace

    def _role_command(self, workspace: dict[str, Any], role: str) -> str:
        if not role:
            return ""
        config = self.workspaces.get_agent(int(workspace.get("id") or 0), role)
        if config is None:
            return ""
        return str(config.get("command") or "").strip()

    def _new_pty(
        self,
        role: str,
        session_id: str | None = None,
        agent_id: str = "",
        *,
        resume: bool = False,
        state_path: str | os.PathLike[str] | None = None,
        legacy: bool = True,
        command_text: str | None = None,
    ) -> PtyManager:
        workspace = self.active_workspace()
        adapter = adapter_for(str(workspace.get("engine") or "claude"))
        command_text = (command_text or "").strip() or self._effective_command(workspace, role)
        # 切回某个角色时必须用 --resume：复用 --session-id 会让 CC 报
        # “session already exists”，随后降级成一个全新的空会话。
        resume = resume and bool(adapter.resume_flag)
        if self._pty_command:
            factory = None
        elif resume:
            factory = lambda sid: adapter.build_resume_command(
                command_text, sid, self._claude_hook_args(adapter)
            )
        else:
            factory = lambda sid: adapter.build_command(
                command_text, sid, self._claude_hook_args(adapter)
            )
        manager = getattr(self, "terminals", None)
        if manager is not None and legacy:
            manager.signature = self._workspace_signature(workspace, command_text)
        return PtyManager(
            cwd=str(workspace.get("project_path") or self.project),
            command=self._pty_command,
            command_factory=factory,
            session_id=session_id,
            env=self._pty_env,
            state_path=state_path if state_path is not None else self.home / "pty.json",
            role=role,
            agent_id=agent_id,
            engine=adapter.id,
        )

    def _claude_hook_args(self, adapter: EngineAdapter) -> list[str]:
        """Claude 引擎追加 --settings（daemon 未绑定端口前跳过）。"""
        if adapter.id != "claude" or self.port <= 0:
            return []
        try:
            path = write_claude_hook_settings(self.home, self.host, self.port, self.token)
        except OSError as exc:
            print(f"claude hook settings failed: {exc}", file=sys.stderr, flush=True)
            return []
        return ["--settings", str(path)]

    def notify_claude_hook(self, session_id: str, pty: PtyManager | None = None) -> dict[str, Any]:
        """Claude Hook 回调 → PTY notice；同 session/kind 30s 去重，不记录消息内容。"""
        target = pty or self.terminals.active()
        now = time.monotonic()
        key = (session_id, PERMISSION_MATCHER)
        with self._hook_notice_lock:
            last = self._hook_notice_seen.get(key)
            if last is not None and now - last < HOOK_NOTICE_DEDUPE_SECONDS:
                return {"deduped": True, "notice": target.latest_notice()}
            self._hook_notice_seen[key] = now
            if len(self._hook_notice_seen) > 64:
                cutoff = now - HOOK_NOTICE_DEDUPE_SECONDS * 4
                for stale in [
                    entry for entry, seen in self._hook_notice_seen.items() if seen < cutoff
                ]:
                    del self._hook_notice_seen[stale]
        notice = target.notify(PERMISSION_MATCHER, CLAUDE_HOOK_NOTICE_MESSAGE)
        return {"deduped": False, "notice": notice}

    def resolve_agent_wake_prompt(self, workspace_id: int, role: str) -> str:
        """按 workspace_id + role 取该 Agent 的专属唤醒词。"""
        config = self.workspaces.get_agent(workspace_id, role)
        if config is None:
            raise ValueError(
                f"当前 Agent（{role}）未配置唤醒词，请在 设置/工作区 → Agent 唤醒词 中配置"
            )
        content = str(config.get("wake_prompt_content") or "").strip()
        if not content:
            raise ValueError(f"当前 Agent（{role}）的唤醒词为空，请先配置")
        if str(config.get("wake_prompt_type") or "text") == "file":
            try:
                text = Path(content).expanduser().read_text(encoding="utf-8")[:65536]
            except OSError as exc:
                raise ValueError(f"读取唤醒提示词文件失败：{exc}") from exc
            if not text.strip():
                raise ValueError(f"当前 Agent（{role}）的唤醒词文件为空")
            return text.strip()
        return content

    def handoff_status(self) -> dict[str, Any]:
        with self._handoff_lock:
            return dict(self._handoff)

    def _handoff_phase(self, phase: str, message: str) -> None:
        with self._handoff_lock:
            self._handoff.update({"phase": phase, "message": message})

    def start_handoff(self, role: str, agent_id: str) -> tuple[int, dict[str, Any]]:
        workspace = self.active_workspace()
        workspace_id = int(workspace.get("id") or 0)
        adapter = adapter_for(str(workspace.get("engine") or "claude"))
        if not adapter.supports_clear:
            return 400, {"error": "当前引擎不支持一键交接", "reason": "handoff_unsupported"}
        try:
            wake = self.resolve_agent_wake_prompt(workspace_id, role)
        except ValueError as exc:
            return 400, {"error": str(exc), "reason": "wake_prompt_required"}
        pty = self.terminals.active()
        if not pty.status().get("running"):
            return 409, {"error": "PTY 未运行，无法交接", "reason": "pty_not_running"}
        session = pty.session_id
        project = self.transcript_project()
        offset = _transcript_offset(project, session)
        with self._handoff_lock:
            if self._handoff.get("active"):
                return 409, {"error": "已有交接进行中", "reason": "handoff_busy"}
            self._handoff = {
                "active": True,
                "phase": "handoff",
                "message": "正在注入交接提示词…",
                "role": role,
                "agent_id": agent_id,
                "workspace_id": workspace_id,
                "session_before": session,
                "session_after": "",
                "started_at": utc_now(),
                "finished_at": "",
                "error": "",
                "warning": "",
            }
        threading.Thread(
            target=self._run_handoff,
            args=(role, session, project, offset, wake),
            daemon=True,
        ).start()
        return 202, self.handoff_status()

    def _run_handoff(self, role: str, session: str, project: str, offset: int, wake: str) -> None:
        pty = self.terminals.active()
        try:
            _inject_prompt(pty, handoff_prompt())
            self._handoff_phase("waiting", "等待 Agent 完成…")
            if not self._wait_handoff_reply(pty, project, session, offset):
                raise RuntimeError("等待 Agent 完成超时")
            self._handoff_phase("clearing", "正在清空会话…")
            clear_at = time.time()
            pty.write("/clear\r")
            time.sleep(0.8)
            self._handoff_phase("wake", "正在注入唤醒提示词…")
            _inject_prompt(pty, wake)
            self._handoff_phase("relocating", "正在定位新会话…")
            new_session = self._wait_new_session(project, clear_at, exclude=session)
            warning = ""
            if new_session:
                pty.set_session(new_session)
                self.terminals.remember_session(role, new_session)
            else:
                warning = "未能定位新会话 JSONL，对话页可能仍显示旧会话"
            with self._handoff_lock:
                self._handoff.update(
                    {
                        "active": False,
                        "phase": "done",
                        "message": "交接完成，已进入新会话",
                        "session_after": new_session,
                        "warning": warning,
                        "finished_at": utc_now(),
                    }
                )
        except (OSError, RuntimeError, ValueError) as exc:
            with self._handoff_lock:
                self._handoff.update(
                    {
                        "active": False,
                        "phase": "error",
                        "message": f"交接失败：{exc}",
                        "error": str(exc),
                        "finished_at": utc_now(),
                    }
                )

    def _wait_handoff_reply(self, pty: PtyManager, project: str, session: str, offset: int) -> bool:
        deadline = time.monotonic() + handoff_timeout_seconds()
        current = offset
        while time.monotonic() < deadline:
            if not pty.status().get("running"):
                raise RuntimeError("PTY 已退出")
            if pty.session_id != session:
                return False
            result = read_appended(project, session, current)
            if result is not None:
                current = int(result.get("offset", current))
                for message in result.get("messages", []):
                    if message.get("role") == "assistant" and any(
                        block.get("kind") == "text" for block in message.get("blocks", [])
                    ):
                        return True
            time.sleep(0.5)
        return False

    def _wait_new_session(self, project: str, after: float, exclude: str) -> str:
        deadline = time.monotonic() + HANDOFF_RELOCATE_SECONDS
        while time.monotonic() < deadline:
            session = newest_session(project, after=after - 0.5, exclude=exclude)
            if session:
                return session
            time.sleep(0.5)
        return ""

    def _effective_command(self, workspace: dict[str, Any], role: str) -> str:
        adapter = adapter_for(str(workspace.get("engine") or "claude"))
        return (
            self._role_command(workspace, role)
            or str(workspace.get("command") or "")
            or adapter.default_command
        )

    def pty_for(self, role: str, agent_id: str = "") -> PtyManager:
        """返回绑定该 Agent 的唯一实例；role 相同但 agent_id 变化视为换人。"""
        role = role.strip()
        agent_id = agent_id.strip()
        workspace = self.active_workspace()
        signature = self._workspace_signature(workspace, self._effective_command(workspace, role))
        with self.terminals.lock:
            current = self.terminals.active()
            if (
                current.role == role
                and current.agent_id == agent_id
                and self.terminals.signature == signature
            ):
                return current
            # 冲突重试可能换了 session id，切走前记住最新值。
            self.terminals.remember_current_session()
            current.stop()
            project = str(workspace.get("project_path") or self.project)
            adapter = adapter_for(str(workspace.get("engine") or "claude"))
            session = self.terminals.session_for(role)
            resume = False
            if session and adapter.resume_flag and not self._pty_command:
                # 只有 JSONL 真实存在时才续接；否则换新 id，避免 --resume 失败。
                if transcript_path(project, session) is not None:
                    resume = True
                else:
                    session = None
            if not session:
                session = (
                    self._pty_session_override
                    if role == "" and self._pty_session_override
                    else str(uuid.uuid4())
                )
                self.terminals.remember_session(role, session)
            pty = self.terminals.install(self._new_pty(role, session, agent_id, resume=resume))
        self._maybe_inject_wake(role, session, pty)
        return pty

    # ---- P0 PTY 池 Spike（PRISM_TERMINAL_POOL，默认关） ----

    def pool_enabled(self) -> bool:
        return terminal_pool_enabled()

    def pool_limit(self) -> int:
        return terminal_pool_limit()

    def pool_ttl_seconds(self) -> float:
        return terminal_pool_ttl_seconds()

    def pool_memory_threshold(self) -> float:
        return terminal_pool_memory_percent()

    def _pool_state_path(self, terminal_id: str) -> Path:
        if terminal_id == DEFAULT_TERMINAL_ID:
            return self.home / "pty.json"
        return self.home / f"pty-{terminal_id}.json"

    def _ensure_pool_capacity(self, protected: str = "") -> tuple[int, dict[str, Any]] | None:
        """先 LRU 空闲 Shell，再拒绝新建；Agent 终端 pin，不参与回收。"""
        limit = self.pool_limit()
        threshold = self.pool_memory_threshold()
        for _ in range(limit + 1):
            over_limit = self.terminals.pool_count() >= limit
            over_memory = memory_percent() > threshold
            if not over_limit and not over_memory:
                return None
            if self.terminals.evict_lru(protected) is None:
                reason = "pool_limit" if over_limit else "memory_pressure"
                return (
                    (429 if over_limit else 507),
                    {
                        "error": (
                            f"terminal pool limit {limit} reached"
                            if over_limit
                            else f"memory above {threshold:.0f}%"
                        ),
                        "reason": reason,
                    },
                )
        return 429, {"error": "terminal pool capacity exhausted", "reason": "pool_limit"}

    def _ensure_pool_agent(self, role: str) -> tuple[str, bool]:
        """返回 (agent_id, 是否本次新建 registry 记录)。Shell 不调用。"""
        record = self.registry.get(role)
        if (
            record is not None
            and record.status not in _OFFLINE_STATUSES
            and process_alive(record.pid)
        ):
            return _agent_id(record), False
        now = utc_now()
        record = AgentRecord(
            role=role,
            pid=os.getpid(),
            identity_pid=os.getpid(),
            generation=self.registry.generation(role),
            pgid=os.getpid(),
            job_id="",
            status="registered",
            started_at=now,
            last_heartbeat=now,
            command="workspace",
            updated_at=now,
            project=self.project,
        )
        self.registry.upsert(record)
        return _agent_id(record), True

    def _pool_session(self, role: str, project: str) -> tuple[str, bool]:
        session = self.terminals.session_for(role) or str(uuid.uuid4())
        self.terminals.remember_session(role, session)
        resume = transcript_path(project, session) is not None
        return session, resume

    def _create_shell_terminal(self, terminal_id: str) -> tuple[int, dict[str, Any]]:
        workspace = self.active_workspace()
        command = shell_command()
        meta = TerminalMeta(
            terminal_id,
            kind="shell",
            command=" ".join(command),
            pinned=False,
        )
        pty = PtyManager(
            cwd=str(workspace.get("project_path") or self.project),
            command=command,
            env=self._pty_env,
            state_path=self._pool_state_path(terminal_id),
            engine="shell",
        )
        self.terminals.install(pty, terminal_id)
        self.terminals.set_meta(meta)
        try:
            pty.start()
        except (OSError, RuntimeError) as exc:
            self.terminals.remove(terminal_id)
            self._publish()
            return 500, {"error": f"shell start failed: {exc}"}
        self._publish()
        return 201, {"terminal": self.terminals.status_for(terminal_id)}

    def _create_agent_terminal(
        self,
        terminal_id: str,
        role: str,
        payload: dict[str, Any],
    ) -> tuple[int, dict[str, Any]]:
        workspace = self.active_workspace()
        project = str(workspace.get("project_path") or self.project)
        command_text = str(payload.get("command") or "").strip() or self._effective_command(
            workspace, role
        )
        check = preflight(command_text, project)
        if not check.get("ok"):
            return 400, check
        agent_id, owns_agent = self._ensure_pool_agent(role)
        session, resume = self._pool_session(role, project)
        meta = TerminalMeta(
            terminal_id,
            kind="agent",
            role=role,
            agent_id=agent_id,
            command=command_text,
            pinned=True,
            owns_agent=owns_agent,
        )
        pty = self._new_pty(
            role,
            session,
            agent_id,
            resume=resume,
            state_path=self._pool_state_path(terminal_id),
            legacy=False,
            command_text=command_text,
        )
        self.terminals.install(pty, terminal_id)
        self.terminals.set_meta(meta)
        try:
            pty.start()
        except (OSError, RuntimeError) as exc:
            self.terminals.remove(terminal_id)
            if owns_agent:
                self.registry.set_status(role, "stopped")
            self._publish()
            return 500, {"error": f"agent terminal start failed: {exc}"}
        self._publish()
        return 201, {"terminal": self.terminals.status_for(terminal_id)}

    def create_terminal(self, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        if not self.pool_enabled():
            return 404, {"error": "terminal pool disabled", "reason": "pool_disabled"}
        kind = str(payload.get("kind") or "shell").strip().lower()
        if kind not in {"shell", "agent"}:
            return 400, {"error": "kind must be shell or agent"}
        role = str(payload.get("role") or "").strip().casefold()
        if kind == "agent":
            try:
                role = _role({"role": role})
            except ValueError as exc:
                return 400, {"error": str(exc)}
            if self.terminals.find_by_role(role) is not None:
                return 409, {
                    "error": f"role already has a terminal: {role}",
                    "reason": "role_bound",
                }
        capacity_error = self._ensure_pool_capacity()
        if capacity_error is not None:
            return capacity_error
        terminal_id = new_terminal_id()
        if kind == "shell":
            return self._create_shell_terminal(terminal_id)
        return self._create_agent_terminal(terminal_id, role, payload)

    def terminal_view(self, terminal_id: str) -> dict[str, Any] | None:
        """终端状态 + 本地活跃度（JSONL mtime / PTY 输入输出 / Hook 等待）。"""
        status = self.terminals.status_for(terminal_id)
        if status is None:
            return None
        now = time.time()
        session = str(status.get("session_id") or "")
        activity: dict[str, Any] = (
            session_activity(self.transcript_project(), session)
            if session
            else {
                "state": "unknown",
                "stop_reason": "",
                "pending_tools": 0,
                "last_message_at": 0.0,
            }
        )
        jsonl_at = float(activity.get("last_assistant_at") or 0.0)
        output_at = float(status.get("last_output_at") or 0.0)
        submit_at = float(status.get("last_submit_at") or 0.0)
        # 只有底层输出才算活跃：assistant JSONL + PTY stdout；用户按键/视图切换不算。
        last_activity = max(jsonl_at, output_at)
        role = str(status.get("role") or "")
        has_task = False
        if role:
            record = self.registry.get(role)
            has_task = bool(record is not None and record.current_task_id)
        notice_at = float(status.get("notice_at") or 0.0)
        # 等待用户处理：没有时间上限；只有输入回车/Ctrl+C（或菜单选择）才解除。
        waiting = (
            str(status.get("notice_kind") or "") == PERMISSION_MATCHER
            and notice_at > 0
            and submit_at < notice_at
        )
        status.update(
            {
                "last_activity_at": last_activity,
                "activity_state": str(activity.get("state") or "unknown"),
                "stop_reason": str(activity.get("stop_reason") or ""),
                "pending_tools": int(activity.get("pending_tools") or 0),
                "has_task": has_task,
                "waiting": waiting,
                "now": now,
            }
        )
        return status

    def terminal_views(self) -> list[dict[str, Any]]:
        views = [self.terminal_view(item["terminal_id"]) for item in self.terminals.list_status()]
        return [view for view in views if view is not None]

    def terminal_status(self, terminal_id: str) -> dict[str, Any] | None:
        return self.terminal_view(terminal_id)

    def pool_status(self) -> dict[str, Any]:
        return {
            "enabled": self.pool_enabled(),
            "limit": self.pool_limit(),
            "ttl_seconds": self.pool_ttl_seconds(),
            "memory_percent": memory_percent(),
            "memory_threshold": self.pool_memory_threshold(),
            "active_terminal_id": self.terminals.active_id,
            "terminals": self.terminal_views(),
        }

    def activate_terminal(self, terminal_id: str) -> tuple[int, dict[str, Any]]:
        if not self.pool_enabled():
            return 404, {"error": "terminal pool disabled", "reason": "pool_disabled"}
        if not self.terminals.activate(terminal_id):
            return 404, {"error": "unknown terminal"}
        self._publish()
        return 200, {"terminal": self.terminals.status_for(terminal_id)}

    def _delete_pending_locks(self, role: str) -> int:
        """物理删除该角色的 pending 锁，避免下次扫描把已删除角色“复活”。"""
        pending_dir = self.home / PENDING_DIR
        if not pending_dir.is_dir():
            return 0
        removed = 0
        for path in pending_dir.glob(f"*{LOCK_SUFFIX}"):
            parsed = parse_lock_file_name(path.name)
            if parsed is None:
                continue
            lock_role, _pid, _created = parsed
            if _str(lock_role).strip().casefold() != role.casefold():
                continue
            removed += int(unlink_with_retry(path))
        return removed

    def delete_agent_completely(self, workspace_id: int, role: str) -> dict[str, Any]:
        """安全物理删除：配置 + PTY/会话映射 + 注册记录 + pending 锁 + 重建代数。

        历史 JSONL 不删除；重建时 session_id/agent_id 都是全新的（代数 +1）。
        """
        role = role.strip().casefold()
        if not role:
            raise ValueError("role is required")
        with self._agent_delete_lock:
            deleted_config = self.workspaces.delete_agent(workspace_id, role)
            removed_terminals = self.remove_terminal_for_role(role)
            removed_session = self.terminals.forget_role(role)
            removed_record = self.registry.delete(role)
            generation = self.registry.bump_generation(role)
            removed_locks = self._delete_pending_locks(role)
            self._publish_event("agent_removed", {"role": role, "generation": generation})
        return {
            "role": role,
            "deleted": bool(deleted_config or removed_terminals or removed_record),
            "removed_config": deleted_config,
            "removed_terminals": removed_terminals,
            "removed_session": removed_session,
            "removed_record": removed_record,
            "removed_locks": removed_locks,
            "generation": generation,
        }

    def remove_terminal_for_role(self, role: str) -> bool:
        """彻底删除某角色的终端；默认终端只停止并标记离线。"""
        role = role.strip()
        if not role:
            return False
        removed = False
        for terminal_id, pty in self.terminals.terminals_for_role(role):
            if terminal_id == DEFAULT_TERMINAL_ID:
                pty.stop()
                meta = self.terminals.meta(terminal_id)
                if meta is not None:
                    meta.closed = True
                removed = True
            else:
                removed = self.terminals.remove(terminal_id) or removed
        return removed

    def remove_terminal(
        self, terminal_id: str, *, confirm: bool = False
    ) -> tuple[int, dict[str, Any]]:
        if not self.pool_enabled():
            return 404, {"error": "terminal pool disabled", "reason": "pool_disabled"}
        meta = self.terminals.meta(terminal_id)
        if meta is None or self.terminals.get(terminal_id) is None:
            return 404, {"error": "unknown terminal"}
        if terminal_id == DEFAULT_TERMINAL_ID:
            return 409, {"error": "default terminal cannot be removed"}
        if meta.kind == "agent" and not confirm:
            return 409, {
                "error": "agent terminal requires confirm=true",
                "reason": "confirm_required",
            }
        if meta.owns_agent and meta.role:
            self.registry.set_status(meta.role, "stopped")
        self.terminals.remove(terminal_id)
        self._publish()
        return 200, {
            "removed": terminal_id,
            "active_terminal_id": self.terminals.active_id,
        }

    def convert_terminal(
        self, terminal_id: str, payload: dict[str, Any]
    ) -> tuple[int, dict[str, Any]]:
        """转换 A：预检 → 停 Shell → 同 terminal_id 起 CLI；失败标 failed，不重开 Shell。"""
        if not self.pool_enabled():
            return 404, {"error": "terminal pool disabled", "reason": "pool_disabled"}
        pty = self.terminals.get(terminal_id)
        meta = self.terminals.meta(terminal_id)
        if pty is None or meta is None:
            return 404, {"error": "unknown terminal"}
        if meta.kind != "shell":
            return 409, {"error": "only shell terminals can be converted"}
        try:
            role = _role({"role": payload.get("role")})
        except ValueError as exc:
            return 400, {"error": str(exc)}
        workspace = self.active_workspace()
        project = str(workspace.get("project_path") or self.project)
        command_text = str(payload.get("command") or "").strip() or self._effective_command(
            workspace, role
        )
        check = preflight(command_text, project)
        if not check.get("ok"):
            return 400, check

        agent_id, owns_agent = self._ensure_pool_agent(role)
        session, resume = self._pool_session(role, project)
        next_meta = TerminalMeta(
            terminal_id,
            kind="agent",
            role=role,
            agent_id=agent_id,
            command=command_text,
            created_at=meta.created_at,
            pinned=True,
            owns_agent=owns_agent,
        )
        pty.stop()
        try:
            next_pty = self._new_pty(
                role,
                session,
                agent_id,
                resume=resume,
                state_path=self._pool_state_path(terminal_id),
                legacy=False,
                command_text=command_text,
            )
        except (OSError, RuntimeError) as exc:
            next_meta.failed = True
            next_meta.failed_reason = str(exc)
            self.terminals.set_meta(next_meta)
            if owns_agent:
                self.registry.set_status(role, "stopped")
            self._publish()
            return 500, {
                "state": "failed",
                "error": f"convert failed: {exc}",
                "terminal": self.terminals.status_for(terminal_id),
            }
        self.terminals.install(next_pty, terminal_id)
        self.terminals.set_meta(next_meta)
        try:
            next_pty.start()
        except (OSError, RuntimeError) as exc:
            next_meta.failed = True
            next_meta.failed_reason = str(exc)
            self.terminals.set_meta(next_meta)
            if owns_agent:
                self.registry.set_status(role, "stopped")
            self._publish()
            return 500, {
                "state": "failed",
                "error": f"convert failed: {exc}",
                "terminal": self.terminals.status_for(terminal_id),
            }
        self._publish()
        return 200, {"terminal": self.terminals.status_for(terminal_id)}

    def sweep_pool(self) -> list[str]:
        if not self.pool_enabled():
            return []
        removed = self.terminals.sweep(self.pool_ttl_seconds())
        if removed:
            self._publish()
        return removed

    def _maybe_inject_wake(self, role: str, session: str, pty: PtyManager) -> None:
        """新会话首次启动时注入该角色唤醒词；已有历史或主动交接时不重复注入。"""
        if not role or not session:
            return
        workspace = self.active_workspace()
        try:
            wake = self.resolve_agent_wake_prompt(int(workspace.get("id") or 0), role)
        except ValueError:
            return
        project = str(workspace.get("project_path") or self.project)
        history = read_history(project, session, limit=1)
        if history is not None and history.get("messages"):
            return
        key = f"{role}:{session}"
        with self._wake_lock:
            if key in self._wake_injected:
                return
            self._wake_injected.add(key)
        threading.Thread(
            target=self._inject_wake_when_ready,
            args=(role, session, pty, wake),
            daemon=True,
        ).start()

    def _inject_wake_when_ready(self, role: str, session: str, pty: PtyManager, wake: str) -> None:
        key = f"{role}:{session}"
        try:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if self.terminals.active() is not pty or pty.session_id != session:
                    return
                if pty.status().get("running"):
                    break
                time.sleep(0.2)
            else:
                return
            time.sleep(wake_inject_delay_seconds())
            if (
                self.terminals.active() is not pty
                or pty.session_id != session
                or not pty.status().get("running")
            ):
                return
            _inject_prompt(pty, wake)
        except (OSError, RuntimeError, ValueError):
            with self._wake_lock:
                self._wake_injected.discard(key)

    def stop_pty_for_role(self, role: str, agent_id: str = "") -> bool:
        role = role.strip()
        agent_id = agent_id.strip()
        with self.terminals.lock:
            pty = self.terminals.active()
            if pty.role != role:
                return False
            if agent_id and pty.agent_id != agent_id:
                # 旧句柄（同 role、旧 agent_id）不得误杀新 Agent 的 PTY。
                return False
            self.terminals.remember_current_session()
            if pty.status().get("running"):
                pty.stop()
            return True

    def check_agent_id(self, role: str, agent_id: str) -> bool:
        role = role.strip()
        agent_id = agent_id.strip()
        if not role or not agent_id:
            return False
        record = self.registry.get(role)
        return record is not None and _agent_id(record) == agent_id

    def role_online(self, role: str) -> bool:
        role = role.strip()
        if not role:
            return True
        record = self.registry.get(role)
        if record is None or record.status in _OFFLINE_STATUSES:
            return False
        return process_alive(record.pid)

    def enforce_pty_binding(self) -> bool:
        """PTY 绑定的 Agent 离线/停止/被同角色新注册顶替时，立即回收 PTY 与 claude。"""
        pty = self.terminals.active()
        role = pty.role
        if not role:
            return False
        record = self.registry.get(role)
        if record is None:
            return self.stop_pty_for_role(role)
        if record.status in _OFFLINE_STATUSES:
            return self.stop_pty_for_role(role)
        if not process_alive(record.pid) or _agent_id(record) != pty.agent_id:
            return self.stop_pty_for_role(role)
        return False

    def _agent_cache_entries(self) -> list[tuple[Path, dict[str, Any]]]:
        """按目录 mtime 缓存 agents/*.json 的解析结果。

        状态快照每 2s 生成，旧实现每个 agent 都要重新 glob + 读取 + 解析全部缓存，
        连续切换时叠成明显卡顿；目录 mtime 在注册/替换缓存时变化，足以失效。
        """
        cache_dir = self.home / AGENT_CACHE_DIR
        try:
            mtime = cache_dir.stat().st_mtime_ns
        except OSError:
            return []
        now = time.monotonic()
        with self._agent_cache_lock:
            cached_mtime, cached_at, entries = self._agent_cache_snapshot
            if mtime == cached_mtime and now - cached_at < 0.5:
                return entries
            parsed_entries: list[tuple[Path, dict[str, Any]]] = []
            for path in sorted(cache_dir.glob("*.json")):
                try:
                    parsed = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                if isinstance(parsed, dict):
                    parsed_entries.append((path, parsed))
            self._agent_cache_snapshot = (mtime, now, parsed_entries)
            return parsed_entries

    def _role_activity(self, role: str, session: str | None) -> dict[str, Any]:
        """同一角色可能有多个历史 session；取最近有 JSONL 活动的那个。

        外部会话的 cwd 可能是项目子目录，所以优先用它缓存里的 project 解析 JSONL，
        找不到再回退到当前工作区 project。
        """
        empty: dict[str, Any] = {
            "state": "unknown",
            "stop_reason": "",
            "pending_tools": 0,
            "last_message_at": 0.0,
        }
        active_project = self.transcript_project()
        candidates: list[tuple[str, str]] = [(session, active_project)] if session else []
        for _path, parsed in self._agent_cache_entries():
            if _str(parsed.get("role")).strip().casefold() != role.casefold():
                continue
            sid = _str(parsed.get("session_id")).strip()
            if re.fullmatch(r"[A-Za-z0-9-]{8,64}", sid) is None:
                continue
            cached_project = _str(parsed.get("project")).strip() or active_project
            candidates.append((sid, cached_project))
        best = empty
        best_at = -1.0
        seen: set[str] = set()
        for sid, project in candidates:
            if sid in seen:
                continue
            seen.add(sid)
            activity = session_activity(project, sid)
            if float(activity.get("last_message_at") or 0.0) <= 0 and project != active_project:
                activity = session_activity(active_project, sid)
            at = float(activity.get("last_message_at") or 0.0)
            if at > best_at:
                best = activity
                best_at = at
        return best

    def _agent_dict(self, record: AgentRecord) -> dict[str, Any]:
        data = record.to_dict()
        # stopped/lost/failed/pending 一律不算在线，避免 PID 复用造成“假活”。
        data["alive"] = record.status not in _OFFLINE_STATUSES and process_alive(record.pid)
        data["agent_id"] = _agent_id(record)
        session = self.external_session(record.role)
        data["external_session_id"] = session
        activity = self._role_activity(record.role, session)
        data["last_activity_at"] = float(activity.get("last_assistant_at") or 0.0)
        data["activity_state"] = str(activity.get("state") or "unknown")
        data["stop_reason"] = str(activity.get("stop_reason") or "")
        data["pending_tools"] = int(activity.get("pending_tools") or 0)
        data["has_task"] = bool(record.current_task_id)
        if record.command == "workspace" or record.job_id:
            data["source"] = "local"
        elif record.command.startswith("mcp_server"):
            data["source"] = "external"
        else:
            data["source"] = "external"
        return data

    def external_session(self, role: str) -> str | None:
        """读取该角色最新且仍存活的 MCP 缓存，返回其真实 Claude session id。"""
        best: tuple[str, str] | None = None
        for path, parsed in self._agent_cache_entries():
            if _str(parsed.get("role")).strip().casefold() != role.casefold():
                continue
            project = _str(parsed.get("project")).strip()
            if project and _normalize_project(project) != self.project:
                continue
            mcp_pid = _int(parsed.get("mcp_pid"))
            if mcp_pid <= 0 or not process_alive(mcp_pid):
                continue
            session_id = _str(parsed.get("session_id")).strip()
            if re.fullmatch(r"[A-Za-z0-9-]{8,64}", session_id) is None:
                continue
            updated = _str(parsed.get("updated_at")).strip()
            if not updated:
                try:
                    updated = datetime.fromtimestamp(path.stat().st_mtime, UTC).isoformat()
                except OSError:
                    updated = ""
            if best is None or updated > best[0]:
                best = (updated, session_id)
        return best[1] if best is not None else None

    @property
    def revision(self) -> int:
        with self._condition:
            return self._revision

    @property
    def is_stopping(self) -> bool:
        return self._stopping.is_set()

    def wait_for_change(self, last_seen: int) -> int:
        """空闲时无超时阻塞（零唤醒）；被 notify_all 唤醒后去抖并返回最新 revision。"""
        with self._condition:
            self._condition.wait()
            if self._revision == last_seen:
                return last_seen
            self._condition.wait(WATCH_DEBOUNCE_SECONDS)
            return self._revision

    def subscribe(self) -> queue.Queue[dict[str, Any]]:
        subscriber: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1)
        with self._condition:
            self._subscribers.append(subscriber)
        return subscriber

    def unsubscribe(self, subscriber: queue.Queue[dict[str, Any]]) -> None:
        with self._condition:
            if subscriber in self._subscribers:
                self._subscribers.remove(subscriber)

    def _publish(self, event: str = "snapshot") -> None:
        """写事务提交后调用：先取快照，再短暂持锁递增 revision 并通知。"""
        snapshot = self.status()
        with self._condition:
            self._revision += 1
            revision = self._revision
            subscribers = tuple(self._subscribers)
            self._condition.notify_all()
        frame = {"revision": revision, "event": event, "data": snapshot}
        for subscriber in subscribers:
            _offer_latest(subscriber, frame)

    def _publish_event(self, event: str, data: dict[str, Any]) -> None:
        """轻量定向事件（如 agent_removed）：不重建全量快照，立即推送。"""
        with self._condition:
            self._revision += 1
            revision = self._revision
            subscribers = tuple(self._subscribers)
            self._condition.notify_all()
        frame = {"revision": revision, "event": event, "data": data}
        for subscriber in subscribers:
            _offer_latest(subscriber, frame)

    def _publish_shutdown(self) -> None:
        with self._condition:
            self._revision += 1
            revision = self._revision
            subscribers = tuple(self._subscribers)
            self._condition.notify_all()
        frame = {"revision": revision, "event": "shutdown", "data": {"stopping": True}}
        for subscriber in subscribers:
            _offer_latest(subscriber, frame)

    def _wait_subscribers(self, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._condition:
                if not self._subscribers:
                    return
            time.sleep(0.02)

    def register(self, payload: dict[str, Any]) -> dict[str, Any]:
        role = _role(payload)
        reported_project = _str(payload.get("project")).strip()
        if reported_project and _normalize_project(reported_project) != self.project:
            raise ProjectMismatch(
                f"project mismatch: daemon={self.project} agent={_normalize_project(reported_project)}"
            )
        now = utc_now()
        existing = self.registry.get(role)
        incoming_pid = _int(payload.get("pid"))
        incoming_pgid = _int(payload.get("pgid")) or incoming_pid
        incoming_identity = _int(payload.get("claude_pid"))
        if incoming_identity <= 0 or not process_alive(incoming_identity):
            # 旧版 MCP 不报 claude_pid：服务端自行穿透 uv/python 包装链找宿主。
            incoming_identity = _host_pid(incoming_pid) or incoming_pid
        # 人没换，身份证就不准换：旧身份进程还活着时，无论 MCP 报什么新 PID，
        # 都复用旧 agent_id 与 started_at，只刷新 pid/心跳等运行态字段。
        # 只有旧身份进程彻底死亡，新进程才允许拿到全新 agent_id。
        if _identity_alive(existing):
            assert existing is not None
            identity_pid = _identity_pid(existing)
            started_at = existing.started_at
            pid = incoming_pid or existing.pid
            pgid = incoming_pgid or existing.pgid
        else:
            identity_pid = incoming_identity
            requested_started_at = _str(payload.get("started_at")).strip()
            started_at = requested_started_at or now
            pid = incoming_pid
            pgid = incoming_pgid
        record = AgentRecord(
            role=role,
            pid=pid,
            identity_pid=identity_pid,
            generation=self.registry.generation(role),
            pgid=pgid,
            job_id=_str(payload.get("job_id")),
            status=_str(payload.get("status")) or "registered",
            started_at=started_at,
            last_heartbeat=now,
            command=_str(payload.get("command")),
            updated_at=now,
            current_task_id=_str(payload.get("current_task_id")),
            cost_tokens=_int(payload.get("cost_tokens")),
            cost_budget=(
                _int(payload.get("cost_budget")) if payload.get("cost_budget") is not None else None
            ),
            project=self.project,
        )
        self.registry.upsert(record)
        self._sync_terminal_binding(role, _agent_id(record))
        self._publish()
        return self._agent_dict(record)

    def _sync_terminal_binding(self, role: str, agent_id: str) -> bool:
        """角色重新注册后，把池内同角色运行中终端的旧句柄对齐到当前 agent_id。

        仅同步仍存活的终端；已停止的终端保留旧绑定，避免把旧会话“复活”。
        """
        if not role or not agent_id:
            return False
        changed = False
        for meta in self.terminals.list_meta():
            if meta.kind != "agent" or meta.role != role or meta.agent_id == agent_id:
                continue
            pty = self.terminals.get(meta.terminal_id)
            if pty is None or not pty.status().get("running"):
                continue
            # PTY 对象与 meta 都持有绑定量；只改 meta 会让输入守卫继续报旧句柄。
            pty.agent_id = agent_id
            meta.agent_id = agent_id
            changed = True
        return changed

    def is_local_role(self, role: str) -> bool:
        role = role.strip().casefold()
        if not role:
            return False
        existing = self.registry.get(role)
        if existing is not None and existing.command == "workspace":
            return True
        workspace = self.active_workspace()
        if self.workspaces.get_agent(int(workspace.get("id") or 0), role) is not None:
            return True
        return any(
            meta.kind == "agent" and meta.role == role for meta in self.terminals.list_meta()
        )

    def revive_local_agent(self, role: str) -> dict[str, Any] | None:
        """懒加载：只对本地角色重建注册记录；外部离线角色不擅自拉起本地 CLI。"""
        role = role.strip().casefold()
        if not role or not self.is_local_role(role):
            return None
        existing = self.registry.get(role)
        if existing is not None and self.role_online(role):
            return self._agent_dict(existing)
        now = utc_now()
        started_at = (
            existing.started_at if existing is not None and existing.command == "workspace" else now
        )
        record = AgentRecord(
            role=role,
            pid=os.getpid(),
            identity_pid=os.getpid(),
            generation=self.registry.generation(role),
            pgid=os.getpid(),
            job_id="",
            status="registered",
            started_at=started_at,
            last_heartbeat=now,
            command="workspace",
            updated_at=now,
            project=self.project,
        )
        self.registry.upsert(record)
        self._publish()
        return self._agent_dict(record)

    def resolve(self, role: str) -> dict[str, Any] | None:
        record = self.registry.get(role)
        return self._agent_dict(record) if record is not None else None

    def _reconcile_agent_cache(self) -> tuple[bool, int, int]:
        """home/agents/<session>.json：存活会话恢复登记；死缓存保留给 --resume，按 TTL 清理。"""
        cache_dir = self.home / AGENT_CACHE_DIR
        if not cache_dir.is_dir():
            return False, 0, 0
        changed = False
        recovered = 0
        pruned = 0
        for path in sorted(cache_dir.glob("*.json")):
            try:
                parsed = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                parsed = None
            data = dict(parsed) if isinstance(parsed, dict) else None
            role = _str(data.get("role")).strip() if data else ""
            project = _str(data.get("project")).strip() if data else ""
            if data is None or not role:
                pruned += int(_unlink(path))
                continue
            # 共享 home 时其它项目的缓存只跳过、不删除，避免跨项目互删。
            if project and _normalize_project(project) != self.project:
                continue
            if _cache_expired(data, path):
                pruned += int(_unlink(path))
                continue
            mcp_pid = _int(data.get("mcp_pid"))
            claude_pid = _int(data.get("claude_pid"))
            if (
                mcp_pid <= 0
                or not process_alive(mcp_pid)
                or (claude_pid > 0 and not process_alive(claude_pid))
            ):
                # 进程已死也保留角色缓存：--resume 复用同一 session id 时需要它。
                continue
            existing = self.registry.get(role)
            now = utc_now()
            session = _str(data.get("session")).strip() if data else ""
            ref = _str(data.get("ref")).strip() if data else ""
            cached_identity = (
                claude_pid
                if claude_pid > 0 and process_alive(claude_pid)
                else (_host_pid(mcp_pid) or mcp_pid)
            )
            # 缓存巡检同样必须复用：旧身份仍存活时不得用缓存里的新 PID 换证。
            if _identity_alive(existing) and existing is not None:
                identity_pid = _identity_pid(existing)
                started_at = existing.started_at
            else:
                identity_pid = cached_identity
                started_at = _str(data.get("started_at")).strip() if data else ""
                if not started_at:
                    started_at = now
            suffix = f" session={session}" if session else ""
            suffix += f" ref={ref}" if ref else ""
            self.registry.upsert(
                AgentRecord(
                    role=role,
                    pid=mcp_pid,
                    identity_pid=identity_pid,
                    generation=self.registry.generation(role),
                    pgid=mcp_pid,
                    job_id="",
                    status="registered",
                    started_at=started_at,
                    last_heartbeat=now,
                    command=f"mcp_server{suffix}".strip(),
                    updated_at=now,
                    project=self.project,
                )
            )
            recovered += 1
            changed = True
        return changed, recovered, pruned

    def reconcile_pending_agents(self) -> tuple[bool, int, int]:
        """pending/*.lock：锁被持有=进程存活；能拿到锁=残留并清理。

        锁由 Agent 进程持有，进程退出/崩溃时由操作系统释放，因此 Daemon 无论何时
        启动都能立即识别存量 Agent，无需网络心跳。探测一律非阻塞、绝不等待。
        """
        pending_dir = self.home / PENDING_DIR
        if not pending_dir.is_dir():
            return False, 0, 0
        changed = False
        registered = 0
        pruned = 0
        live: dict[str, tuple[float, Path, int]] = {}
        counts: dict[str, int] = {}
        for path in sorted(pending_dir.glob(f"*{LOCK_SUFFIX}")):
            parsed = parse_lock_file_name(path.name)
            if parsed is None:
                if probe_lock(path) != "busy":
                    pruned += int(unlink_with_retry(path))
                continue
            role, pid, created = parsed
            state = probe_lock(path)
            if state == "busy":
                counts[role] = counts.get(role, 0) + 1
                current = live.get(role)
                if current is None or created >= current[0]:
                    live[role] = (created, path, pid)
            elif state == "free":
                pruned += int(unlink_with_retry(path))
        # 旧版 pending/*.json 已废弃：不再参与发现，直接清理。
        for legacy in sorted(pending_dir.glob("*.json")):
            pruned += int(unlink_with_retry(legacy))
        for role, (created, path, pid) in sorted(live.items()):
            existing = self.registry.get(role)
            if counts.get(role, 0) > 1 and (
                existing is None or existing.pid != pid or existing.status == "stopped"
            ):
                print(
                    f"pending: duplicate live role {role!r}; keeping pid {pid} ({path.name})",
                    file=sys.stderr,
                    flush=True,
                )
            if existing is not None and existing.pid == pid and existing.status != "stopped":
                continue
            now = utc_now()
            started_at = (
                existing.started_at
                if existing is not None and existing.pid == pid
                else datetime.fromtimestamp(created, UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            )
            self.registry.upsert(
                AgentRecord(
                    role=role,
                    pid=pid,
                    generation=self.registry.generation(role),
                    pgid=pid,
                    job_id="",
                    status="registered",
                    started_at=started_at,
                    last_heartbeat=now,
                    command=f"lockfile {path.name}",
                    updated_at=now,
                    project=self.project,
                )
            )
            registered += 1
            changed = True
        return changed, registered, pruned

    def discover(self) -> dict[str, Any]:
        """手动检测：恢复存活会话缓存、清理死缓存，并重新探测已登记 agent 的存活。"""
        changed, recovered, pruned = self._reconcile_agent_cache()
        pending_changed, pending_registered, pending_pruned = self.reconcile_pending_agents()
        changed = pending_changed or changed
        recovered += pending_registered
        pruned += pending_pruned
        agents = self.registry.list()
        for record in agents:
            alive = process_alive(record.pid)
            if alive and record.status in ("stopped", "lost"):
                changed = self.registry.set_status(record.role, "registered") or changed
            elif not alive and record.status != "stopped":
                changed = self.registry.set_status(record.role, "stopped") or changed
                self.stop_pty_for_role(record.role)
        if changed:
            self._publish()
        agents = self.registry.list()
        excluded_pids = {os.getpid()}
        excluded_pids.update(record.pid for record in agents if record.status != "stopped")
        wild = [
            candidate.to_dict()
            for candidate in self.wild_driver.discover(self.project, excluded_pids)
        ]
        return {
            "discovered": True,
            "recovered": recovered,
            "pruned": pruned,
            "pending_registered": pending_registered,
            "pending_pruned": pending_pruned,
            "agents": [self._agent_dict(record) for record in agents],
            "wild": wild,
        }

    def cleanup_wild(
        self,
        payload: dict[str, Any],
        *,
        process_factory: Callable[[int], Any] | None = None,
    ) -> tuple[int, dict[str, Any]]:
        """强制清理未注册的 node/python 残留进程（需 create_time 防 PID 复用）。"""
        pid = _int(payload.get("pid"))
        if pid <= 0:
            return 400, {"error": "pid is required"}
        raw_create = payload.get("create_time")
        if isinstance(raw_create, bool) or not isinstance(raw_create, (int, float)):
            return 400, {"error": "create_time is required", "reason": "missing_create_time"}
        create_time = float(raw_create)
        if create_time <= 0:
            return 400, {"error": "create_time is required", "reason": "missing_create_time"}

        factory = process_factory or psutil.Process
        protected = {record.pid for record in self.registry.list() if record.pid > 0}
        protected.add(os.getpid())
        ancestor = os.getpid()
        seen: set[int] = set()
        while ancestor > 0 and ancestor not in seen:
            seen.add(ancestor)
            protected.add(ancestor)
            try:
                ancestor = int(factory(ancestor).ppid())
            except (psutil.Error, OSError):
                break
        if pid in protected:
            return 409, {"error": "process is protected", "reason": "protected_process"}

        try:
            process = factory(pid)
            name = str(process.name()).casefold()
        except psutil.NoSuchProcess:
            return 404, {"error": "process not found", "reason": "not_found"}
        except (psutil.Error, OSError) as exc:
            return 500, {"error": f"cannot inspect process: {exc}"}
        if name not in WILD_CLEANUP_ALLOWED_EXES:
            return 400, {"error": f"process not cleanable: {name}", "reason": "not_cleanable"}
        try:
            actual_create = float(process.create_time())
        except (psutil.Error, OSError) as exc:
            return 500, {"error": f"cannot inspect process: {exc}"}
        if abs(actual_create - create_time) > 0.05:
            return 409, {"error": "process identity changed", "reason": "stale_process"}
        try:
            cwd = str(process.cwd())
        except (psutil.Error, OSError):
            cwd = ""
        if not _within(cwd, _normalize_path(self.project)):
            return 403, {"error": "process is outside the project", "reason": "outside_project"}

        killed = kill_process_tree(pid, create_time, process_factory=factory)
        self.wild_driver.clear_cache()
        if not killed:
            return 409, {"error": "process already exited", "reason": "stale_process"}
        return 200, {"cleaned": True, "pid": pid, "killed": killed}

    def _sweep_leftover_workers(self) -> None:
        try:
            excluded = {record.pid for record in self.registry.list() if record.pid > 0}
            killed = sweep_leftover_real_workers(Path(self.project), excluded_pids=excluded)
        except (psutil.Error, OSError, RuntimeError) as exc:
            print(f"leftover worker sweep skipped: {exc}", file=sys.stderr, flush=True)
            return
        if killed:
            print(f"leftover claude -p workers reclaimed: {killed}", file=sys.stderr, flush=True)

    def status(self) -> dict[str, Any]:
        port = self.port
        if self.server is not None:
            port = int(self.server.server_address[1])
        return {
            "daemon": {
                "pid": os.getpid(),
                "host": self.host,
                "port": port,
                "started_at": self.started_at,
                "project": self.project,
            },
            "agents": [self._agent_dict(record) for record in self.registry.list()],
            "active_terminal_id": self.terminals.active_id,
            "terminal_pool": self.pool_enabled(),
            "terminal_pool_limit": self.pool_limit(),
            "terminals": self.terminal_views(),
        }

    def start(self, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        role = _role(payload)
        if not bool(payload.get("dummy")):
            return 400, {"error": "P1 only supports --dummy workers"}
        existing = self.registry.get(role)
        if existing is not None and existing.status == "running" and process_alive(existing.pid):
            return 409, {
                "error": f"role already running: {role}",
                "agent": self._agent_dict(existing),
            }
        worker = self.launcher.spawn_dummy(role)
        now = utc_now()
        record = AgentRecord(
            role=role,
            pid=worker.pid,
            generation=self.registry.generation(role),
            pgid=worker.pgid,
            job_id=worker.job_id,
            status="running",
            started_at=now,
            last_heartbeat=now,
            command=f"dummy_worker --role {role}",
            updated_at=now,
            project=self.project,
        )
        self.registry.upsert(record)
        self._publish()
        return 200, {"agent": self._agent_dict(record)}

    def stop(self, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        role = _role(payload)
        record = self.registry.get(role)
        if record is None:
            return 404, {"error": f"unknown role: {role}"}
        if not self.role_online(role):
            # 幂等：已经离线的人再点 X 不能报错，释放残留 PTY 后直接成功。
            self.stop_pty_for_role(role)
            return 200, {"stopped": True, "already_stopped": True, "role": role}
        agent_id = _str(payload.get("agent_id"))
        if agent_id and _agent_id(record) != agent_id:
            # 旧前端句柄（同 role、旧 agent_id）不得误杀新注册的同名 Agent。
            return 409, {
                "error": f"stale agent id for {role}",
                "reason": "stale_agent",
                "agent": self._agent_dict(record),
            }
        if not record.job_id:
            # 外部会话不归中控杀，但必须释放绑定在它名下的 PTY（工作台幽灵终端）。
            self.stop_pty_for_role(role, agent_id)
            return 409, {
                "error": f"external agent cannot be stopped: {role}",
                "reason": "external_agent",
                "agent": self._agent_dict(record),
            }
        try:
            reclaim = self.launcher.stop(role)
        except KeyError:
            if process_alive(record.pid):
                self.stop_pty_for_role(role, agent_id)
                return 409, {
                    "error": f"agent is not managed by this daemon: {role}",
                    "reason": "external_agent",
                    "agent": self._agent_dict(record),
                }
            reclaim = {"role": role, "note": "worker already exited"}
        self.registry.set_status(role, "stopped")
        self.stop_pty_for_role(role, agent_id)
        self._publish()
        return 200, {"stopped": True, "role": role, "reclaim": reclaim}

    def need(self, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        result = self.router.need(payload)
        self._publish()
        return result

    def ack(self, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        result = self.router.ack(payload)
        self._publish()
        return result

    def completed(self, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        result = self.router.completed(payload)
        self._publish()
        return result

    def tasks(self, *, role: str = "", status: str = "") -> dict[str, Any]:
        return self.router.tasks(role=role, status=status)

    def task_detail(self, task_id: str) -> dict[str, Any]:
        return self.router.task_detail(task_id)

    def usage(self, *, task_id: str = "", role: str = "", day: str = "") -> dict[str, Any]:
        return self.router.usage(task_id=task_id, role=role, day=day)

    def observability(self) -> dict[str, Any]:
        data = self.router.observability()
        port = self.port
        if self.server is not None:
            port = int(self.server.server_address[1])
        data["daemon"] = {
            "pid": os.getpid(),
            "host": self.host,
            "port": port,
            "started_at": self.started_at,
        }
        return data

    def heartbeat(self, payload: dict[str, Any]) -> dict[str, Any]:
        role = _role(payload)
        raw_task_id = payload.get("task_id")
        task_id = raw_task_id.strip() if isinstance(raw_task_id, str) else ""
        result = {"heartbeat": self.router.heartbeat(role, task_id)}
        self._publish("heartbeat")
        return result

    def step(self, payload: dict[str, Any]) -> dict[str, Any]:
        result = self.router.upsert_step(payload)
        self._publish()
        return result

    def effect(self, payload: dict[str, Any]) -> dict[str, Any]:
        result = self.router.add_effect(payload)
        self._publish()
        return result

    def steps(self, task_id: str) -> dict[str, Any]:
        return self.router.steps(task_id)

    def effects(self, task_id: str) -> dict[str, Any]:
        return self.router.effects(task_id)

    def effect_get(self, idem_key: str) -> dict[str, Any]:
        return self.router.effect(idem_key)

    def request_shutdown(self) -> None:
        self._stopping.set()
        server = self.server
        if server is not None:
            threading.Thread(target=server.shutdown, daemon=True).start()

    def _monitor_loop(self) -> None:
        interval = max(
            0.05,
            min(self.ack_timeout / 5.0, self.lost_seconds / 4.0, 1.0),
        )
        while not self._stop_monitor.wait(interval):
            try:
                changed = bool(self.router.check_timeouts(self.ack_timeout))
                lost = self.router.check_heartbeats(self.lost_seconds)
                for action in lost:
                    self.stop_pty_for_role(str(action.get("role", "")))
                changed = bool(lost) or changed
                changed = bool(self.router.check_task_budgets()) or changed
                changed = self.enforce_pty_binding() or changed
                changed = bool(self.sweep_pool()) or changed
                now = time.monotonic()
                if now - self._last_watchdog >= self.watchdog_seconds:
                    self._last_watchdog = now
                    changed = bool(self.router.watch_workers()) or changed
                if now - self._last_agent_cache >= agent_cache_seconds():
                    self._last_agent_cache = now
                    changed = self._reconcile_agent_cache()[0] or changed
                    changed = self.reconcile_pending_agents()[0] or changed
                if changed:
                    self._publish()
            except Exception:  # noqa: BLE001, S112 - 监控线程不因单次异常退出
                continue

    def serve(
        self,
        *,
        dashboard: bool = True,
        watch: bool = False,
        show_heartbeat: bool = False,
        sweep_workers: bool = False,
    ) -> None:
        if not _acquire_instance_lock(self.home):
            raise OSError(f"orchestrator already running for {self.home}")
        try:
            if _port_in_use(self.host, self.port):
                raise OSError(f"orchestrator port {self.port} already in use")
            if sweep_workers:
                self._sweep_leftover_workers()
            self._serve_forever(
                dashboard=dashboard,
                watch=watch,
                show_heartbeat=show_heartbeat,
            )
        finally:
            _release_instance_lock(self.home)

    def _serve_forever(
        self,
        *,
        dashboard: bool,
        watch: bool,
        show_heartbeat: bool,
    ) -> None:
        server = _Server((self.host, self.port), _Handler)
        server.daemon = self
        self.server = server
        port = int(server.server_address[1])
        self.port = port
        self.router.orchestrator_url = f"http://{self.host}:{port}"
        write_daemon_info(
            self.home,
            {
                "pid": os.getpid(),
                "host": self.host,
                "port": port,
                "started_at": self.started_at,
            },
        )
        self._stop_monitor.clear()
        self._stop_dashboard.clear()
        monitor = threading.Thread(target=self._monitor_loop, daemon=True)
        monitor.start()
        dashboard_thread: threading.Thread | None = None
        watch_thread: threading.Thread | None = None
        if dashboard:
            print(render_dashboard(self, port), flush=True)
            if watch:
                watch_thread = threading.Thread(
                    target=_watch_loop,
                    args=(self, self._stop_dashboard, show_heartbeat),
                    daemon=True,
                )
                watch_thread.start()
            else:
                refresh = dashboard_refresh_seconds()
                if refresh > 0:
                    dashboard_thread = threading.Thread(
                        target=_dashboard_loop,
                        args=(self, port, refresh, self._stop_dashboard),
                        daemon=True,
                    )
                    dashboard_thread.start()
        try:
            server.serve_forever(poll_interval=0.2)
        finally:
            self._stop_monitor.set()
            self._stop_dashboard.set()
            self._publish_shutdown()
            monitor.join(timeout=5)
            if dashboard_thread is not None:
                dashboard_thread.join(timeout=5)
            if watch_thread is not None:
                watch_thread.join(timeout=5)
            self._wait_subscribers(timeout=2.0)
            self.terminals.stop_all()
            self.launcher.stop_all()
            server.server_close()
            (self.home / "daemon.json").unlink(missing_ok=True)


class _Server(ThreadingHTTPServer):
    daemon: Daemon

    def handle_error(self, request: Any, client_address: Any) -> None:
        # SSE / keep-alive 客户端主动断开是正常情况，不打印 traceback。
        if isinstance(sys.exc_info()[1], (ConnectionError, TimeoutError)):
            return
        super().handle_error(request, client_address)


class _Handler(BaseHTTPRequestHandler):
    server_version = "SentinelOrchestrator/0.1"
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:
        return

    @property
    def daemon(self) -> Daemon:
        server = self.server
        if not isinstance(server, _Server):
            raise TypeError("unexpected server type")
        return server.daemon

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_DELETE(self) -> None:
        self._dispatch("DELETE")

    def _normalize_terminal_path(self, path: str) -> tuple[str | None, str]:
        """`/pty/<terminal_id>/<action>` → (旧内部路由, terminal_id)。

        未知终端返回 (None, id)；不带 id 的旧路径以空串表示“使用当前 active 终端”。
        """
        if not path.startswith("/pty/"):
            return path, ""
        parts = [part for part in path.split("/") if part]
        if len(parts) < 3:
            return path, ""
        if parts[1] == "chat" and len(parts) == 3:
            return path, ""
        if parts[1] == "terminals":
            return path, ""
        terminal_id = parts[1]
        if self.daemon.terminals.get(terminal_id) is None:
            return None, terminal_id
        if len(parts) == 3:
            return f"/pty/{parts[2]}", terminal_id
        if len(parts) == 4 and parts[2] == "chat" and parts[3] == "stream":
            return "/pty/chat/stream", terminal_id
        return None, terminal_id

    def _terminal_target(self) -> PtyManager | None:
        return self.daemon.terminals.get(self._terminal_id)

    def _pool_binding(self, role: str, agent_id: str, pty: PtyManager | None) -> tuple[str, str]:
        """池终端必须按 meta 绑定校验，不能只信客户端传的 role/agent。"""
        if pty is None or not self.daemon.pool_enabled():
            return role, agent_id
        meta = self.daemon.terminals.meta(self._terminal_id)
        if meta is None or not meta.role:
            return role, agent_id
        return role or meta.role, agent_id or meta.agent_id

    def _authorized(self, path: str) -> bool:
        if path == "/health":
            return True
        header = self.headers.get("Authorization", "")
        return hmac.compare_digest(header, f"Bearer {self.daemon.token}")

    def _read_json(self) -> dict[str, Any] | None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        raw = self.rfile.read(length) if length > 0 else b"{}"
        try:
            parsed = json.loads(raw.decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send(400, {"error": "invalid JSON"})
            return None
        if not isinstance(parsed, dict):
            self._send(400, {"error": "JSON object required"})
            return None
        return dict(parsed)

    def _send(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (ConnectionResetError, BrokenPipeError):
            # 客户端已断开（常见于前端取消/刷新），不是 daemon 错误。
            return

    def _agent_guard(self, role: str, agent_id: str) -> dict[str, Any] | None:
        """角色请求必须携带注册期唯一 agent_id；空 role 保留旧版全局行为。"""
        if not role:
            return None
        if not agent_id:
            return {"error": "agent_id is required", "reason": "agent_id_required"}
        if not self.daemon.check_agent_id(role, agent_id):
            return {"error": f"stale agent id for {role}", "reason": "stale_agent"}
        if not self.daemon.role_online(role):
            return {"error": f"agent offline: {role}", "reason": "agent_offline"}
        return None

    def _pty_preflight_error(self, role: str = "") -> dict[str, Any] | None:
        check = self.daemon.pty_preflight(role)
        return None if check.get("ok") else check

    def _workspaces_payload(self) -> dict[str, Any]:
        active = self.daemon.active_workspace()
        return {
            "workspaces": self.daemon.workspaces.list(),
            "active_id": active.get("id"),
            "active_preflight": self.daemon.pty_preflight(),
            "engines": [
                {
                    "id": adapter.id,
                    "label": adapter.label,
                    "default_command": adapter.default_command,
                    **adapter.capabilities(),
                }
                for adapter in ENGINES.values()
            ],
        }

    def _pty_role_guard(
        self, role: str, agent_id: str, pty: PtyManager | None = None
    ) -> dict[str, Any] | None:
        """输入/缩放必须命中当前绑定的角色与 agent_id；空 role 保留旧版全局行为。"""
        if not role:
            return None
        error = self._agent_guard(role, agent_id)
        if error is not None:
            return error
        bound = pty or self.daemon.terminals.active()
        if bound.role != role or bound.agent_id != agent_id:
            return {
                "error": f"pty bound to {bound.role or 'global'}/{bound.agent_id or '-'}",
                "reason": "role_mismatch",
                "bound_role": bound.role,
                "bound_agent_id": bound.agent_id,
            }
        return None

    def _stream_pty_chat(
        self,
        offset: int,
        role: str = "",
        agent_id: str = "",
        pty: PtyManager | None = None,
    ) -> None:
        """JSONL 旁路：只读轮询 PTY 会话 transcript，增量推送问答。"""
        pty = pty or self.daemon.pty_for(role, agent_id)
        try:
            pty.ensure_started()
        except (OSError, RuntimeError) as exc:
            self._send(500, {"error": f"pty start failed: {exc}"})
            return
        session = pty.session_id
        current = offset
        self.close_connection = True
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
        except (ConnectionResetError, BrokenPipeError):
            return
        last_ping = time.monotonic()
        notice_seq = 0
        while True:
            notice = pty.latest_notice()
            if notice and int(notice.get("seq", 0)) > notice_seq:
                notice_seq = int(notice["seq"])
                self._write_bytes(
                    f"event: notice\ndata: {json.dumps(notice, ensure_ascii=False)}\n\n".encode()
                )
            if pty.session_id != session:
                self._write_bytes(
                    f"event: session\ndata: {json.dumps({'session_id': pty.session_id})}\n\n".encode()
                )
                return
            result = read_appended(self.daemon.transcript_project(), session, current)
            if result is not None:
                if result["reset"]:
                    current = 0
                    self._write_bytes(b"event: reset\ndata: {}\n\n")
                else:
                    if result["context_tokens"]:
                        self._write_bytes(
                            f"event: context\ndata: {json.dumps({'context_tokens': result['context_tokens']})}\n\n".encode()
                        )
                    usage = result.get("usage") or {}
                    if any(usage.values()):
                        self._write_bytes(
                            f"event: usage\ndata: {json.dumps({'usage': usage}, ensure_ascii=False)}\n\n".encode()
                        )
                    for message in result["messages"]:
                        frame = json.dumps({"message": message}, ensure_ascii=False)
                        if not self._write_bytes(f"event: message\ndata: {frame}\n\n".encode()):
                            return
                    current = result["offset"]
            now = time.monotonic()
            if now - last_ping >= pty.ping_seconds():
                if not self._write_bytes(b": ping\n\n"):
                    return
                last_ping = now
            if not pty.status().get("running"):
                self._write_bytes(b"event: exit\ndata: {}\n\n")
                return
            time.sleep(0.6)

    def _stream_pty(
        self,
        role: str = "",
        agent_id: str = "",
        pty: PtyManager | None = None,
        *,
        replay: str = "tail",
    ) -> None:
        pty = pty or self.daemon.pty_for(role, agent_id)
        try:
            pty.ensure_started()
        except (OSError, RuntimeError) as exc:
            self._send(500, {"error": f"pty start failed: {exc}"})
            return
        subscriber, backlog = pty.subscribe()
        self.close_connection = True
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
        except (ConnectionResetError, BrokenPipeError):
            pty.unsubscribe(subscriber)
            return
        try:
            if replay == "raw":
                for seq, chunk in backlog:
                    frame = {"seq": seq, "data": base64.b64encode(chunk).decode("ascii")}
                    if not self._write_bytes(
                        f"event: output\ndata: {json.dumps(frame)}\n\n".encode()
                    ):
                        return
            elif replay == "tail":
                # 只回放最近的纯文本尾部（最多 200 行 / 20KB），保留可回滚历史且不卡死。
                frame = {"text": pty.plain_tail()}
                if not self._write_bytes(
                    f"event: replay\ndata: {json.dumps(frame, ensure_ascii=False)}\n\n".encode()
                ):
                    return
            notice = pty.latest_notice()
            if notice:
                self._write_bytes(
                    f"event: notice\ndata: {json.dumps(notice, ensure_ascii=False)}\n\n".encode()
                )
            if not pty.status().get("running"):
                self._write_bytes(
                    f"event: exit\ndata: {json.dumps({'exit_status': None})}\n\n".encode()
                )
                return
            while True:
                try:
                    event, data = subscriber.get(timeout=pty.ping_seconds())
                except queue.Empty:
                    if not self._write_bytes(b": ping\n\n"):
                        return
                    continue
                if event == "output":
                    frame = {
                        "seq": data.get("seq"),
                        "data": base64.b64encode(data.get("data", b"")).decode("ascii"),
                    }
                    if not self._write_bytes(
                        f"event: output\ndata: {json.dumps(frame)}\n\n".encode()
                    ):
                        return
                elif event == "notice":
                    self._write_bytes(
                        f"event: notice\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode()
                    )
                elif event == "exit":
                    self._write_bytes(f"event: exit\ndata: {json.dumps(data)}\n\n".encode())
                    return
        finally:
            pty.unsubscribe(subscriber)

    def _write_bytes(self, data: bytes) -> bool:
        """写 SSE 字节；客户端断开时返回 False，由调用方结束流。"""
        try:
            self.wfile.write(data)
            self.wfile.flush()
        except (ConnectionResetError, BrokenPipeError):
            return False
        return True

    def _write_event(self, revision: int, event: str, data: dict[str, Any]) -> bool:
        body = json.dumps(data, ensure_ascii=False)
        frame = f"id: {revision}\nevent: {event}\ndata: {body}\n\n"
        return self._write_bytes(frame.encode("utf-8"))

    def _stream_events(self) -> None:
        daemon = self.daemon
        subscriber = daemon.subscribe()
        self.close_connection = True
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            sent = daemon.revision
            if not self._write_event(sent, "snapshot", daemon.status()):
                return
            ping = sse_ping_seconds()
            # 池模式下状态包含“最近 5 秒活跃”，每 2s 推一次快照；单终端保持旧心跳。
            wait_seconds = min(ping, 2.0) if daemon.pool_enabled() else ping
            while True:
                try:
                    frame = subscriber.get(timeout=wait_seconds)
                except queue.Empty:
                    if daemon.is_stopping:
                        break
                    if daemon.pool_enabled():
                        if not self._write_event(daemon.revision, "snapshot", daemon.status()):
                            return
                    elif not self._write_bytes(b": ping\n\n"):
                        return
                    continue
                revision = int(frame["revision"])
                if revision <= sent:  # 并发发布可能乱序到达，丢弃旧帧
                    continue
                if not self._write_event(revision, str(frame["event"]), frame["data"]):
                    return
                sent = revision
                if frame["event"] == "shutdown":
                    break
        except OSError:
            return
        finally:
            daemon.unsubscribe(subscriber)

    def _write_log_frame(self, seq: int, role: str, line: str, event: str = "log") -> bool:
        payload = (
            {"role": role, "line": line, "ts": utc_now()} if event == "log" else {"stopping": True}
        )
        frame = f"id: {seq}\nevent: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
        return self._write_bytes(frame.encode("utf-8"))

    def _report_log(self, payload: dict[str, Any]) -> None:
        """外部会话上报（POST /logs/report）：追加到 logs/<role>.log，供 /logs/stream 跟随。"""
        role = str(payload.get("role", "")).strip()
        line = " ".join(str(payload.get("line", "")).split())[:REPORT_LINE_MAX]
        if role != "daemon" and not _ROLE_PATTERN.match(role):
            self._send(400, {"error": "invalid role"})
            return
        if not line:
            self._send(400, {"error": "line required"})
            return
        logs_dir = self.daemon.home / "logs"
        try:
            logs_dir.mkdir(parents=True, exist_ok=True)
            with (logs_dir / f"{role}.log").open("a", encoding="utf-8") as stream:
                stream.write(f"{line}\n")
        except OSError as exc:
            self._send(500, {"error": f"write failed: {exc}"})
            return
        self._send(200, {"reported": True, "role": role, "chars": len(line)})

    def _log_targets(self) -> list[tuple[str, Path]] | None:
        """解析 ?role= 或列出 logs/*.log；非法 role 返回 None（已发送 400）。"""
        logs_dir = self.daemon.home / "logs"
        raw = parse_qs(urlparse(self.path).query).get("role", [""])[0].strip()
        if raw:
            if raw != "daemon" and not _ROLE_PATTERN.match(raw):
                self._send(400, {"error": "invalid role"})
                return None
            return [(raw, logs_dir / f"{raw}.log")]
        if not logs_dir.is_dir():
            return []
        targets: list[tuple[str, Path]] = []
        for path in sorted(logs_dir.glob("*.log")):
            role = path.stem
            if role == "daemon" or _ROLE_PATTERN.match(role):
                targets.append((role, path))
        return targets

    def _stream_logs(self) -> None:
        """SSE 日志流：先回放每个文件最后 tail 行，再按文件增长持续推送。"""
        targets = self._log_targets()
        if targets is None:
            return
        query = parse_qs(urlparse(self.path).query)
        try:
            tail = int(query.get("tail", [str(LOG_TAIL_DEFAULT)])[0])
        except ValueError:
            tail = LOG_TAIL_DEFAULT
        tail = max(0, min(tail, LOG_TAIL_MAX))
        specific_role = bool(query.get("role", [""])[0].strip())
        logs_dir = self.daemon.home / "logs"
        daemon = self.daemon
        self.close_connection = True
        seq = 0
        offsets: dict[Path, int] = {}
        pending: dict[Path, bytes] = {}
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            for role, path in targets:
                for line in _tail_lines(path, tail):
                    seq += 1
                    if not self._write_log_frame(seq, role, line):
                        return
                try:
                    offsets[path] = path.stat().st_size
                except OSError:
                    offsets[path] = 0
            ping = sse_ping_seconds()
            last_emit = time.monotonic()
            rescan_at = last_emit + 2.0
            while True:
                if daemon.is_stopping:
                    seq += 1
                    self._write_log_frame(seq, "daemon", "", event="shutdown")
                    break
                emitted = False
                for role, path in targets:
                    try:
                        size = path.stat().st_size
                    except OSError:
                        continue
                    offset = offsets.get(path, 0)
                    if size < offset:  # 文件被截断/轮转
                        offset = 0
                        pending[path] = b""
                    if size <= offset:
                        continue
                    try:
                        with path.open("rb") as stream:
                            stream.seek(offset)
                            chunk = stream.read(size - offset)
                    except OSError:
                        continue
                    offsets[path] = offset + len(chunk)
                    lines, pending[path] = _split_log_chunk(pending.get(path, b""), chunk)
                    for line in lines:
                        seq += 1
                        if not self._write_log_frame(seq, role, line):
                            return
                        emitted = True
                now = time.monotonic()
                if not specific_role and now >= rescan_at:
                    rescan_at = now + 2.0
                    known = {path for _, path in targets}
                    for path in sorted(logs_dir.glob("*.log")):
                        role = path.stem
                        if path in known or not (role == "daemon" or _ROLE_PATTERN.match(role)):
                            continue
                        targets.append((role, path))
                        offsets[path] = 0
                if emitted:
                    last_emit = now
                elif now - last_emit >= ping:
                    if not self._write_bytes(b": ping\n\n"):
                        return
                    last_emit = now
                time.sleep(LOG_POLL_SECONDS)
        except OSError:
            return

    def _dispatch(self, method: str) -> None:
        path = urlparse(self.path).path
        if not self._authorized(path):
            self._send(401, {"error": "unauthorized"})
            return
        normalized, terminal_id = self._normalize_terminal_path(path)
        if normalized is None:
            self._send(404, {"error": "unknown terminal"})
            return
        self._explicit_terminal = bool(terminal_id)
        self._terminal_id = terminal_id or self.daemon.terminals.active_id
        if method == "DELETE" and path.startswith("/pty/"):
            parts = [part for part in path.split("/") if part]
            if len(parts) == 2 and parts[1] != "terminals":
                self._explicit_terminal = True
                self._terminal_id = parts[1]
                normalized = "/pty/delete"
            elif normalized == "/pty/delete":
                pass
            else:
                self._send(404, {"error": "unknown terminal"})
                return
        path = normalized
        if method == "GET" and path == "/pty/terminals":
            self._send(200, self.daemon.pool_status())
            return
        if method == "POST" and path == "/pty/terminals":
            body = self._read_json()
            if body is None:
                return
            self._send(*self.daemon.create_terminal(body))
            return
        if method == "POST" and path == "/pty/activate":
            self._send(*self.daemon.activate_terminal(self._terminal_id))
            return
        if method == "POST" and path == "/pty/convert":
            body = self._read_json()
            if body is None:
                return
            self._send(*self.daemon.convert_terminal(self._terminal_id, body))
            return
        if method == "DELETE" and path == "/pty/delete":
            body = self._read_json() or {}
            confirm = bool(body.get("confirm"))
            self._send(*self.daemon.remove_terminal(self._terminal_id, confirm=confirm))
            return
        if method == "POST" and path == CLAUDE_HOOK_NOTIFICATION_PATH:
            body = self._read_json()
            if body is None:
                return
            notification_type = body.get("notification_type")
            if notification_type != PERMISSION_MATCHER:
                self._send(400, {"error": "unsupported notification_type"})
                return
            session_id = body.get("session_id")
            if not isinstance(session_id, str) or not session_id.strip():
                self._send(400, {"error": "session_id is required"})
                return
            hook_pty = self.daemon.terminals.find_by_session(session_id)
            if hook_pty is None:
                active_pty = self.daemon.terminals.active()
                if active_pty.session_id == session_id:
                    hook_pty = active_pty
            if hook_pty is None:
                self._send(409, {"error": "session mismatch"})
                return
            self._send(200, self.daemon.notify_claude_hook(session_id, pty=hook_pty))
            return
        if method == "GET" and path == "/sessions/history":
            query = parse_qs(urlparse(self.path).query)
            raw_role = (query.get("role") or [""])[0]
            try:
                role = _role({"role": raw_role})
            except ValueError as exc:
                self._send(400, {"error": str(exc)})
                return
            try:
                limit = int((query.get("limit") or ["200"])[0])
            except ValueError:
                limit = 200
            session = self.daemon.external_session(role)
            if not session:
                self._send(404, {"error": "no terminal session", "role": role})
                return
            history = read_history(self.daemon.transcript_project(), session, limit=limit)
            if history is None:
                self._send(404, {"error": "transcript not found", "role": role})
                return
            self._send(
                200,
                {
                    "role": role,
                    "session_id": session,
                    "source": "terminal",
                    **history,
                },
            )
            return

        if method == "GET" and path == "/pty/history":
            query = parse_qs(urlparse(self.path).query)
            role = _query_role(query)
            agent_id = _query_agent_id(query)
            pool_pty: PtyManager | None = None
            if self.daemon.pool_enabled():
                pool_pty = self._terminal_target()
                meta = self.daemon.terminals.meta(self._terminal_id)
                if pool_pty is None or meta is None:
                    self._send(404, {"error": "unknown terminal"})
                    return
                if meta.kind == "shell":
                    self._send(
                        400,
                        {"error": "shell terminal has no chat session", "reason": "not_agent"},
                    )
                    return
                role = role or meta.role
                agent_id = agent_id or meta.agent_id
            error = self._agent_guard(role, agent_id)
            if error is not None:
                self._send(409 if error.get("reason") != "agent_id_required" else 400, error)
                return
            if pool_pty is None:
                preflight_error = self._pty_preflight_error(role)
                if preflight_error is not None:
                    self._send(400, preflight_error)
                    return
                pool_pty = self.daemon.pty_for(role, agent_id)
            try:
                pool_pty.ensure_started()
            except (OSError, RuntimeError) as exc:
                self._send(500, {"error": f"pty start failed: {exc}"})
                return
            session = pool_pty.session_id
            history = read_history(self.daemon.transcript_project(), session)
            if history is None:
                history = {
                    "messages": [],
                    "truncated": False,
                    "context_tokens": 0,
                    "usage": dict.fromkeys(USAGE_KEYS, 0),
                    "bytes": 0,
                    "offset": 0,
                }
            self._send(
                200,
                {
                    "session_id": session,
                    "source": "pty",
                    "agent_id": agent_id,
                    "budget_tokens": self.daemon.workspace_budget(),
                    **history,
                },
            )
            return

        if method == "GET" and path == "/pty/chat/stream":
            query = parse_qs(urlparse(self.path).query)
            role = _query_role(query)
            agent_id = _query_agent_id(query)
            try:
                offset = max(0, int((query.get("offset") or ["0"])[0]))
            except ValueError:
                offset = 0
            pool_pty = None
            if self.daemon.pool_enabled():
                pool_pty = self._terminal_target()
                meta = self.daemon.terminals.meta(self._terminal_id)
                if pool_pty is None or meta is None:
                    self._send(404, {"error": "unknown terminal"})
                    return
                if meta.kind == "shell":
                    self._send(
                        400,
                        {"error": "shell terminal has no chat session", "reason": "not_agent"},
                    )
                    return
                role = role or meta.role
                agent_id = agent_id or meta.agent_id
            error = self._agent_guard(role, agent_id)
            if error is not None:
                self._send(409 if error.get("reason") != "agent_id_required" else 400, error)
                return
            if pool_pty is None:
                preflight_error = self._pty_preflight_error(role)
                if preflight_error is not None:
                    self._send(400, preflight_error)
                    return
            self._stream_pty_chat(offset, role, agent_id, pool_pty)
            return

        if method == "GET" and path == "/pty/status":
            query = parse_qs(urlparse(self.path).query)
            role = _query_role(query)
            agent_id = _query_agent_id(query)
            if self.daemon.pool_enabled():
                pty_status = self.daemon.terminal_status(self._terminal_id)
                if pty_status is None:
                    self._send(404, {"error": "unknown terminal"})
                    return
            else:
                pty_status = self.daemon.terminals.active().status()
            if role:
                pty_status["role_online"] = self.daemon.role_online(role)
                pty_status["agent_match"] = self.daemon.check_agent_id(role, agent_id)
            self._send(200, pty_status)
            return

        if method == "GET" and path == "/pty/stream":
            query = parse_qs(urlparse(self.path).query)
            role = _query_role(query)
            agent_id = _query_agent_id(query)
            replay_mode = (query.get("replay") or ["tail"])[0].strip().lower()
            if replay_mode not in {"tail", "raw", "none"}:
                replay_mode = "none" if replay_mode in {"0", "false", "no"} else "tail"
            pool_pty = None
            if self.daemon.pool_enabled():
                pool_pty = self._terminal_target()
                meta = self.daemon.terminals.meta(self._terminal_id)
                if pool_pty is None:
                    self._send(404, {"error": "unknown terminal"})
                    return
                if meta is not None:
                    role = meta.role
                    agent_id = meta.agent_id
            error = self._agent_guard(role, agent_id)
            if error is not None:
                self._send(409 if error.get("reason") != "agent_id_required" else 400, error)
                return
            if pool_pty is None:
                preflight_error = self._pty_preflight_error(role)
                if preflight_error is not None:
                    self._send(400, preflight_error)
                    return
            self._stream_pty(role, agent_id, pool_pty, replay=replay_mode)
            return

        if method == "GET" and path == "/handoff":
            self._send(200, self.daemon.handoff_status())
            return

        if method == "GET" and path == "/workspaces/agents":
            query = parse_qs(urlparse(self.path).query)
            workspace_id = _int((query.get("workspace_id") or ["0"])[0])
            role = (query.get("role") or [""])[0].strip().casefold()
            if role:
                self._send(
                    200,
                    {"agent": self.daemon.workspaces.get_agent(workspace_id, role)},
                )
            else:
                self._send(
                    200,
                    {
                        "agents": self.daemon.workspaces.list_agents(workspace_id),
                        "workspace_id": workspace_id,
                    },
                )
            return

        if method == "GET" and path == "/workspaces":
            self._send(200, self._workspaces_payload())
            return

        if method == "GET" and path == "/events":
            self._stream_events()
            return
        if method == "GET" and path == "/logs/stream":
            self._stream_logs()
            return
        payload: dict[str, Any] = {}
        if method == "POST":
            parsed = self._read_json()
            if parsed is None:
                return
            payload = parsed

        if method == "POST" and path == "/logs/report":
            self._report_log(payload)
            return

        if method == "POST" and path == "/discover":
            self._send(200, self.daemon.discover())
            return

        if method == "POST" and path == "/wild/cleanup":
            try:
                status, body = self.daemon.cleanup_wild(payload)
            except (OSError, RuntimeError) as exc:
                status, body = 500, {"error": str(exc)}
            self._send(status, body)
            return

        if method == "POST" and path == "/handoff":
            role = _optional_role(payload)
            agent_id = _optional_agent_id(payload)
            error = self._pty_role_guard(role, agent_id)
            if error is not None:
                self._send(409 if error.get("reason") != "agent_id_required" else 400, error)
                return
            status, body = self.daemon.start_handoff(role, agent_id)
            self._send(status, body)
            return

        if method == "POST" and path == "/workspaces/agents/save":
            try:
                workspace_agent = self.daemon.workspaces.save_agent(payload)
            except ValueError as exc:
                self._send(400, {"error": str(exc)})
                return
            workspace_id = int(workspace_agent.get("workspace_id") or 0)
            active = self.daemon.active_workspace()
            if workspace_id == int(active.get("id") or 0):
                self.daemon._register_workspace_agents()
                self.daemon._publish()
            self._send(
                200,
                {
                    "agent": workspace_agent,
                    "agents": self.daemon.workspaces.list_agents(workspace_id),
                },
            )
            return

        if method == "POST" and path == "/workspaces/agents/delete":
            workspace_id = _int(payload.get("workspace_id"))
            role = _str(payload.get("role")).strip().casefold()
            if not role:
                self._send(400, {"error": "role is required"})
                return
            result = self.daemon.delete_agent_completely(workspace_id, role)
            self._send(
                200,
                {
                    **result,
                    "agents": self.daemon.workspaces.list_agents(workspace_id),
                },
            )
            return

        if method == "POST" and path == "/workspaces/budget":
            workspace_id = _int(payload.get("workspace_id")) or _int(payload.get("id"))
            try:
                workspace = self.daemon.workspaces.set_budget(
                    workspace_id, payload.get("budget_tokens")
                )
            except ValueError as exc:
                self._send(400, {"error": str(exc)})
                return
            self._send(200, {"workspace": workspace})
            return

        if method == "POST" and path == "/workspaces/preflight":
            self._send(
                200,
                preflight(_str(payload.get("command")), _str(payload.get("project_path"))),
            )
            return

        if method == "POST" and path == "/workspaces/save":
            try:
                workspace = self.daemon.workspaces.save(
                    payload, activate=bool(payload.get("activate"))
                )
            except ValueError as exc:
                self._send(400, {"error": str(exc)})
                return
            if workspace.get("active"):
                self.daemon.reset_pty()
            check = preflight(
                str(workspace.get("command", "")), str(workspace.get("project_path", ""))
            )
            workspace_data = self._workspaces_payload()
            workspace_data["workspace"] = {**workspace, "preflight": check}
            self._send(200, workspace_data)
            return

        if method == "POST" and path == "/workspaces/delete":
            if not self.daemon.workspaces.delete(_int(payload.get("id"))):
                self._send(404, {"error": "workspace not found"})
                return
            self.daemon.reset_pty()
            self._send(200, self._workspaces_payload())
            return

        if method == "POST" and path == "/workspaces/activate":
            try:
                workspace = self.daemon.activate_workspace(_int(payload.get("id")))
            except ValueError as exc:
                self._send(400, {"error": str(exc)})
                return
            check = preflight(
                str(workspace.get("command", "")), str(workspace.get("project_path", ""))
            )
            workspace_data = self._workspaces_payload()
            workspace_data["workspace"] = {**workspace, "preflight": check}
            self._send(200, workspace_data)
            return

        if method == "POST" and path == "/pty/start":
            role = _optional_role(payload)
            agent_id = _optional_agent_id(payload)
            target_pty: PtyManager | None = None
            if self.daemon.pool_enabled():
                target_pty = self._terminal_target()
                meta = self.daemon.terminals.meta(self._terminal_id)
                if target_pty is None:
                    self._send(404, {"error": "unknown terminal"})
                    return
                if meta is not None:
                    role = role or meta.role
                    agent_id = agent_id or meta.agent_id
            error = self._agent_guard(role, agent_id)
            if error is not None and error.get("reason") == "agent_offline":
                # 点击离线本地 Agent = 懒加载拉起；外部离线角色不擅自起本地 CLI。
                revived = self.daemon.revive_local_agent(role)
                if revived is not None:
                    agent_id = str(revived.get("agent_id") or "")
                    if target_pty is not None:
                        meta = self.daemon.terminals.meta(self._terminal_id)
                        if meta is not None:
                            meta.agent_id = agent_id
                            self.daemon.terminals.set_meta(meta)
                        target_pty.agent_id = agent_id
                    error = None
            if error is not None:
                self._send(409 if error.get("reason") != "agent_id_required" else 400, error)
                return
            if target_pty is None:
                preflight_error = self._pty_preflight_error(role)
                if preflight_error is not None:
                    self._send(400, preflight_error)
                    return
                target_pty = self.daemon.pty_for(role, agent_id)
            try:
                self._send(200, target_pty.start())
            except (OSError, RuntimeError) as exc:
                self._send(500, {"error": str(exc)})
            return

        if method == "POST" and path == "/pty/input":
            data = payload.get("data")
            if not isinstance(data, str):
                self._send(400, {"error": "data is required"})
                return
            role = _optional_role(payload)
            agent_id = _optional_agent_id(payload)
            target = self._terminal_target() if self.daemon.pool_enabled() else None
            if target is None and self.daemon.pool_enabled():
                self._send(404, {"error": "unknown terminal"})
                return
            role, agent_id = self._pool_binding(role, agent_id, target)
            error = self._pty_role_guard(role, agent_id, target)
            if error is not None:
                self._send(409 if error.get("reason") != "agent_id_required" else 400, error)
                return
            try:
                ok = (target or self.daemon.terminals.active()).write(data)
            except ValueError as exc:
                self._send(400, {"error": str(exc)})
                return
            if ok:
                if target is not None:
                    self.daemon.terminals.touch(self._terminal_id)
                self._send(200, {"written": len(data)})
            else:
                self._send(409, {"error": "pty not running", "reason": "pty_not_running"})
            return

        if method == "POST" and path == "/pty/resize":
            try:
                cols = int(payload.get("cols", 120))
                rows = int(payload.get("rows", 30))
            except (TypeError, ValueError):
                self._send(400, {"error": "invalid cols/rows"})
                return
            force = bool(payload.get("force"))
            role = _optional_role(payload)
            agent_id = _optional_agent_id(payload)
            target = self._terminal_target() if self.daemon.pool_enabled() else None
            if target is None and self.daemon.pool_enabled():
                self._send(404, {"error": "unknown terminal"})
                return
            role, agent_id = self._pool_binding(role, agent_id, target)
            error = self._pty_role_guard(role, agent_id, target)
            if error is not None:
                self._send(409 if error.get("reason") != "agent_id_required" else 400, error)
                return
            if target is not None:
                self.daemon.terminals.touch(self._terminal_id)
            self._send(
                200, (target or self.daemon.terminals.active()).resize(cols, rows, force=force)
            )
            return

        if method == "POST" and path == "/pty/stop":
            role = _optional_role(payload)
            agent_id = _optional_agent_id(payload)
            if self.daemon.pool_enabled():
                target = self._terminal_target()
                meta = self.daemon.terminals.meta(self._terminal_id)
                if target is None or meta is None:
                    self._send(404, {"error": "unknown terminal"})
                    return
                if meta.kind == "shell":
                    # Shell 关闭即销毁；Agent 关闭需要 confirm=true。
                    self._send(*self.daemon.remove_terminal(self._terminal_id, confirm=True))
                    return
                if not bool(payload.get("confirm")):
                    self._send(
                        409,
                        {
                            "error": "agent terminal requires confirm=true",
                            "reason": "confirm_required",
                        },
                    )
                    return
                role, agent_id = self._pool_binding(role, agent_id, target)
                error = self._pty_role_guard(role, agent_id, target)
                if error is not None:
                    if error.get("reason") == "agent_offline":
                        # 已经离线再点 X：幂等成功，避免前端红条死循环。
                        meta.closed = True
                        self.daemon.terminals.set_meta(meta)
                        self._send(200, {**target.stop(), "already_stopped": True})
                        return
                    self._send(409 if error.get("reason") != "agent_id_required" else 400, error)
                    return
                meta.closed = True
                self.daemon.terminals.set_meta(meta)
                self._send(200, target.stop())
                return
            if role and not agent_id:
                self._send(400, {"error": "agent_id is required", "reason": "agent_id_required"})
                return
            if role:
                stopped = self.daemon.stop_pty_for_role(role, agent_id)
                self._send(200, {"stopped": stopped, "role": role, "agent_id": agent_id})
                return
            self._send(200, self.daemon.terminals.active().stop())
            return

        if method == "POST" and path in ("/chat", "/chat/stop"):
            self._send(
                410,
                {
                    "error": "headless chat disabled; use PTY",
                    "reason": "pty_only",
                },
            )
            return

        if method == "GET" and path == "/health":
            self._send(
                200,
                {"ok": True, "pid": os.getpid(), "started_at": self.daemon.started_at},
            )
        elif method == "GET" and path == "/status":
            self._send(200, self.daemon.status())
        elif method == "POST" and path == "/register":
            try:
                self._send(200, {"agent": self.daemon.register(payload)})
            except ProjectMismatch as exc:
                self._send(409, {"error": str(exc)})
            except ValueError as exc:
                self._send(400, {"error": str(exc)})
        elif method == "POST" and path == "/resolve":
            try:
                role = _role(payload)
            except ValueError as exc:
                self._send(400, {"error": str(exc)})
                return
            agent = self.daemon.resolve(role)
            if agent is None:
                self._send(404, {"error": f"unknown role: {role}"})
            else:
                self._send(200, {"agent": agent})
        elif method == "POST" and path == "/start":
            try:
                status, body = self.daemon.start(payload)
            except ValueError as exc:
                status, body = 400, {"error": str(exc)}
            except (OSError, RuntimeError, TimeoutError) as exc:
                status, body = 500, {"error": str(exc)}
            self._send(status, body)
        elif method == "POST" and path == "/stop":
            try:
                status, body = self.daemon.stop(payload)
            except ValueError as exc:
                status, body = 400, {"error": str(exc)}
            except (OSError, RuntimeError) as exc:
                status, body = 500, {"error": str(exc)}
            self._send(status, body)
        elif method == "POST" and path == "/need":
            try:
                status, body = self.daemon.need(payload)
            except RoleBusy as exc:
                status, body = 409, {"error": str(exc)}
            except RoleBudgetExceeded as exc:
                status, body = 429, {"error": str(exc)}
            except ValueError as exc:
                status, body = 400, {"error": str(exc)}
            except (OSError, RuntimeError, TimeoutError) as exc:
                status, body = 500, {"error": str(exc)}
            self._send(status, body)
        elif method == "POST" and path == "/ack":
            try:
                status, body = self.daemon.ack(payload)
            except TaskNotFound as exc:
                status, body = 404, {"error": str(exc)}
            except RoleMismatch as exc:
                status, body = 400, {"error": str(exc)}
            except InvalidTransition as exc:
                status, body = 409, {"error": str(exc)}
            except ValueError as exc:
                status, body = 400, {"error": str(exc)}
            self._send(status, body)
        elif method == "POST" and path == "/completed":
            try:
                status, body = self.daemon.completed(payload)
            except TaskNotFound as exc:
                status, body = 404, {"error": str(exc)}
            except RoleMismatch as exc:
                status, body = 400, {"error": str(exc)}
            except InvalidTransition as exc:
                status, body = 409, {"error": str(exc)}
            except ValueError as exc:
                status, body = 400, {"error": str(exc)}
            self._send(status, body)
        elif method == "GET" and path == "/tasks":
            query = parse_qs(urlparse(self.path).query)
            self._send(
                200,
                self.daemon.tasks(
                    role=query.get("role", [""])[0],
                    status=query.get("status", [""])[0],
                ),
            )
        elif method == "POST" and path == "/task":
            task_id = payload.get("task_id")
            if not isinstance(task_id, str) or not task_id.strip():
                self._send(400, {"error": "task_id is required"})
            else:
                try:
                    self._send(200, self.daemon.task_detail(task_id.strip()))
                except TaskNotFound as exc:
                    self._send(404, {"error": str(exc)})
        elif method == "POST" and path == "/heartbeat":
            try:
                self._send(200, self.daemon.heartbeat(payload))
            except TaskNotFound as exc:
                self._send(404, {"error": str(exc)})
            except ValueError as exc:
                self._send(400, {"error": str(exc)})
        elif method == "POST" and path == "/step":
            try:
                self._send(200, self.daemon.step(payload))
            except TaskNotFound as exc:
                self._send(404, {"error": str(exc)})
            except InvalidTransition as exc:
                self._send(409, {"error": str(exc)})
            except ValueError as exc:
                self._send(400, {"error": str(exc)})
        elif method == "POST" and path == "/effect":
            try:
                self._send(200, self.daemon.effect(payload))
            except TaskNotFound as exc:
                self._send(404, {"error": str(exc)})
            except InvalidTransition as exc:
                self._send(409, {"error": str(exc)})
            except ValueError as exc:
                self._send(400, {"error": str(exc)})
        elif method == "GET" and path == "/steps":
            query = parse_qs(urlparse(self.path).query)
            task_id = query.get("task_id", [""])[0]
            if not task_id:
                self._send(400, {"error": "task_id is required"})
            else:
                try:
                    self._send(200, self.daemon.steps(task_id))
                except TaskNotFound as exc:
                    self._send(404, {"error": str(exc)})
        elif method == "GET" and path == "/effect":
            query = parse_qs(urlparse(self.path).query)
            idem_key = query.get("idem_key", [""])[0]
            task_id = query.get("task_id", [""])[0]
            if idem_key:
                try:
                    self._send(200, self.daemon.effect_get(idem_key))
                except TaskNotFound as exc:
                    self._send(404, {"error": str(exc)})
            elif task_id:
                try:
                    self._send(200, self.daemon.effects(task_id))
                except TaskNotFound as exc:
                    self._send(404, {"error": str(exc)})
            else:
                self._send(400, {"error": "idem_key or task_id is required"})
        elif method == "GET" and path == "/usage":
            query = parse_qs(urlparse(self.path).query)
            self._send(
                200,
                self.daemon.usage(
                    task_id=query.get("task", [""])[0],
                    role=query.get("role", [""])[0],
                    day=query.get("day", [""])[0],
                ),
            )
        elif method == "GET" and path == "/observability":
            self._send(200, self.daemon.observability())
        elif method == "POST" and path == "/shutdown":
            self._send(200, {"stopping": True, "pid": os.getpid()})
            self.daemon.request_shutdown()
        else:
            self._send(404, {"error": "not found"})


def _install_signal_handlers(daemon: Daemon) -> None:
    def handler(signum: int, frame: FrameType | None) -> None:
        daemon.request_shutdown()

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        value = getattr(signal, name, None)
        if value is not None:
            signal.signal(value, handler)


def main(argv: list[str] | None = None) -> int:
    _configure_stdio(sys.stdout, sys.stderr)
    parser = argparse.ArgumentParser(prog="orchestrator-service")
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--home", default="")
    parser.add_argument("--no-dashboard", action="store_true")
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--show-heartbeat", action="store_true")
    args = parser.parse_args(argv)
    home = Path(args.home).resolve() if args.home else default_home()
    try:
        daemon = Daemon(home, args.host, args.port)
        _install_signal_handlers(daemon)
        daemon.serve(
            dashboard=not args.no_dashboard,
            watch=args.watch,
            show_heartbeat=args.show_heartbeat,
            sweep_workers=_should_sweep_workers(),
        )
    except OSError as exc:
        print(f"orchestrator failed to start: {exc}", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
