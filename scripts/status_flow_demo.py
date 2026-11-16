"""工作台状态流转演示（0 API Token）。

用法：
    uv run python scripts/status_flow_demo.py

脚本只做这些事：
- 创建/停止/删除 Pool 终端；
- 在 Shell 里执行本地 PowerShell 命令；
- 向 /hooks/claude/notification 发一个合成 permission_prompt；
- 对 Agent 终端只发 Ctrl+C 清除等待，从不发送任何文本 prompt。

因此不会触发任何 Claude API 请求，Token 消耗为 0（末尾会打印 Agent 会话 usage 校验）。
请在脚本运行期间观察左侧会话坞的颜色与文案变化。
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, cast

HOME = Path(".claude/orchestrator")
ROLE = "flow-demo"
SHELL_COMMAND = "powershell.exe -NoLogo -NoProfile"


def _token() -> str:
    return (HOME / "token").read_text(encoding="utf-8").strip()


def _base() -> str:
    info = json.loads((HOME / "daemon.json").read_text(encoding="utf-8"))
    return f"http://{info['host']}:{info['port']}"


def api(
    method: str, path: str, payload: dict[str, Any] | None = None
) -> tuple[int, dict[str, Any]]:
    headers = {
        "Authorization": f"Bearer {_token()}",
        "Content-Type": "application/json",
    }
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(_base() + path, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return int(response.status), json.load(response)
    except urllib.error.HTTPError as exc:
        try:
            return int(exc.code), json.loads(exc.read().decode("utf-8", "replace"))
        except json.JSONDecodeError:
            return int(exc.code), {"error": "invalid response"}


def terminal(terminal_id: str) -> dict[str, Any] | None:
    _status, body = api("GET", "/status")
    for item in cast(list[dict[str, Any]], body.get("terminals", [])):
        if item.get("terminal_id") == terminal_id:
            return item
    return None


def label(item: dict[str, Any] | None) -> str:
    if item is None:
        return "gone"
    state = item.get("state")
    if state == "failed":
        return "ERROR (red / 报错)"
    if state == "stopped":
        return "OFFLINE (gray / 离线)"
    if item.get("waiting"):
        return "WAITING (amber / 待处理)"
    if item.get("has_task") or int(item.get("pending_tools") or 0) > 0:
        return "RUNNING (blue / 运行中)"
    if time.time() - float(item.get("last_activity_at") or 0) <= 3:
        return "RUNNING (blue / 运行中)"
    return "ONLINE (green / 在线)"


def show(phase: str, terminal_id: str, hold: float) -> None:
    item = terminal(terminal_id)
    print(f"[{phase}] {label(item)}  ({hold:.0f}s)", flush=True)
    if hold > 0:
        time.sleep(hold)


def cleanup(terminal_ids: list[str], workspace_id: int) -> None:
    for terminal_id in terminal_ids:
        api("DELETE", f"/pty/{terminal_id}", {"confirm": True})
    api("POST", "/workspaces/agents/delete", {"workspace_id": workspace_id, "role": ROLE})


def main() -> int:
    _status, status = api("GET", "/status")
    if not status.get("terminal_pool"):
        print("PTY pool is disabled; restart the daemon with PRISM_TERMINAL_POOL=1.")
        return 1
    _status, workspaces = api("GET", "/workspaces")
    workspace_id = int(workspaces.get("active_id") or 0)
    if workspace_id <= 0:
        print("no active workspace")
        return 1

    created: list[str] = []
    try:
        # 预清理上一次异常退出留下的同名角色
        api("POST", "/workspaces/agents/delete", {"workspace_id": workspace_id, "role": ROLE})
        shell_status, shell_body = api("POST", "/pty/terminals", {"kind": "shell"})
        if shell_status != 201:
            print("create shell failed:", shell_body)
            return 1
        shell = shell_body["terminal"]
        shell_id = shell["terminal_id"]
        created.append(shell_id)

        print("== 1. Shell created; watch the row turn green ==", flush=True)
        show("startup", shell_id, 4)
        show("idle", shell_id, 6)

        print("== 2. Run a 4s loop; expect blue, then green after ~3s ==", flush=True)
        api(
            "POST",
            f"/pty/{shell_id}/input",
            {
                "data": (
                    '1..10 | ForEach-Object { Write-Output "FLOW_OUTPUT_$_"; '
                    "Start-Sleep -Milliseconds 400 }\r"
                )
            },
        )
        show("output", shell_id, 2)
        time.sleep(6)
        show("after output", shell_id, 4)

        print("== 3. Create an agent terminal (claude, no prompt; 0 token) ==", flush=True)
        agent_status, agent_body = api(
            "POST", "/pty/terminals", {"kind": "agent", "role": ROLE, "command": "claude"}
        )
        if agent_status != 201:
            print("create agent failed:", agent_body)
            return 1
        agent = agent_body["terminal"]
        agent_id = agent["terminal_id"]
        session_id = agent["session_id"]
        created.append(agent_id)
        show("agent startup", agent_id, 4)
        show("agent idle", agent_id, 6)

        print("== 4. Synthetic permission_prompt; expect amber and hold ==", flush=True)
        api(
            "POST",
            "/hooks/claude/notification",
            {"notification_type": "permission_prompt", "session_id": session_id},
        )
        show("waiting", agent_id, 12)
        print("== 5. Ctrl+C clears waiting; expect green/blue ==", flush=True)
        api("POST", f"/pty/{agent_id}/input", {"data": "\u0003"})
        time.sleep(0.8)
        show("after clear", agent_id, 0)
        time.sleep(4)
        show("settled", agent_id, 4)

        print("== 6. X = offline; expect gray and row retained ==", flush=True)
        api("POST", f"/pty/{agent_id}/stop", {"confirm": True})
        show("offline", agent_id, 8)

        print("== 7. Token check: Agent history usage should be all zero ==", flush=True)
        _code, history = api(
            "GET", f"/pty/{agent_id}/history?role={ROLE}&agent_id={agent['agent_id']}"
        )
        usage = history.get("usage") or {}
        print(
            "agent usage:",
            {
                k: int(usage.get(k) or 0)
                for k in (
                    "input_tokens",
                    "output_tokens",
                    "cache_read_input_tokens",
                    "cache_creation_input_tokens",
                )
            },
            flush=True,
        )

        print("== 8. Trash = permanent delete; expect row to disappear ==", flush=True)
        api("POST", "/workspaces/agents/delete", {"workspace_id": workspace_id, "role": ROLE})
        show("deleted", agent_id, 5)

        print("== 9. Shell exit 42; expect red ERROR ==", flush=True)
        api("POST", f"/pty/{shell_id}/input", {"data": "exit 42\r"})
        time.sleep(1.5)
        show("error", shell_id, 8)

        print("== 10. Remove shell row ==", flush=True)
        api("DELETE", f"/pty/{shell_id}", {"confirm": True})
        show("removed", shell_id, 3)

        print("== done: 0 API tokens were spent (no text prompt sent to claude) ==", flush=True)
        return 0
    finally:
        cleanup([item for item in created if item], workspace_id)


if __name__ == "__main__":
    raise SystemExit(main())
