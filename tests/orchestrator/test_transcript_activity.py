"""JSONL 活动状态：未完成 tool_use、stop_reason 与结束事件。"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from tools.orchestrator.transcript import project_slug, session_activity

SESSION = "11111111-1111-1111-1111-111111111111"


def _entry(
    role: str, content: list[dict[str, Any]], *, stop: str | None = None, sidechain: bool = False
) -> dict[str, Any]:
    return {
        "type": role,
        "isSidechain": sidechain,
        "timestamp": "2026-11-15T00:00:00.000Z",
        "message": {
            "role": role,
            "content": content,
            "stop_reason": stop,
        },
    }


def _session(tmp_path: Path) -> tuple[str, Path, Path]:
    project = tmp_path / "project"
    project.mkdir()
    config = tmp_path / "claude"
    folder = config / "projects" / project_slug(str(project))
    folder.mkdir(parents=True)
    path = folder / f"{SESSION}.jsonl"
    return str(project), config, path


def _write(path: Path, entries: list[dict[str, Any]]) -> None:
    path.write_text(
        "".join(json.dumps(entry, ensure_ascii=False) + "\n" for entry in entries),
        encoding="utf-8",
    )


def test_pending_tool_use_keeps_working(tmp_path: Path) -> None:
    project, config, path = _session(tmp_path)
    _write(
        path,
        [
            _entry("user", [{"type": "text", "text": "run"}]),
            _entry(
                "assistant",
                [{"type": "tool_use", "id": "call_1", "name": "Bash", "input": {}}],
                stop="tool_use",
            ),
        ],
    )
    activity = session_activity(project, SESSION, config_dir=config)
    assert activity["state"] == "working"
    assert activity["pending_tools"] == 1
    assert activity["stop_reason"] == "tool_use"


def test_resolved_tool_still_waits_for_model(tmp_path: Path) -> None:
    project, config, path = _session(tmp_path)
    _write(
        path,
        [
            _entry(
                "assistant",
                [{"type": "tool_use", "id": "call_1", "name": "Read", "input": {}}],
                stop="tool_use",
            ),
            _entry("user", [{"type": "tool_result", "tool_use_id": "call_1", "content": "ok"}]),
        ],
    )
    activity = session_activity(project, SESSION, config_dir=config)
    assert activity["state"] == "working"
    assert activity["pending_tools"] == 0


def test_end_turn_marks_ended(tmp_path: Path) -> None:
    project, config, path = _session(tmp_path)
    _write(
        path,
        [
            _entry("user", [{"type": "text", "text": "hi"}]),
            _entry("assistant", [{"type": "text", "text": "done"}], stop="end_turn"),
        ],
    )
    activity = session_activity(project, SESSION, config_dir=config)
    assert activity["state"] == "ended"
    assert activity["stop_reason"] == "end_turn"
    assert activity["last_assistant_at"] > 0


def test_new_user_after_end_turn_is_working(tmp_path: Path) -> None:
    project, config, path = _session(tmp_path)
    _write(
        path,
        [
            _entry("assistant", [{"type": "text", "text": "done"}], stop="end_turn"),
            _entry("user", [{"type": "text", "text": "next"}]),
        ],
    )
    activity = session_activity(project, SESSION, config_dir=config)
    assert activity["state"] == "working"


def test_sidechain_tool_is_ignored(tmp_path: Path) -> None:
    project, config, path = _session(tmp_path)
    _write(
        path,
        [
            _entry("user", [{"type": "text", "text": "hi"}]),
            _entry("assistant", [{"type": "text", "text": "done"}], stop="end_turn"),
            _entry(
                "assistant",
                [{"type": "tool_use", "id": "call_side", "name": "Bash", "input": {}}],
                stop="tool_use",
                sidechain=True,
            ),
        ],
    )
    activity = session_activity(project, SESSION, config_dir=config)
    assert activity["state"] == "ended"
    assert activity["pending_tools"] == 0


def test_activity_cache_refreshes_on_file_change(tmp_path: Path) -> None:
    project, config, path = _session(tmp_path)
    _write(path, [_entry("assistant", [{"type": "text", "text": "done"}], stop="end_turn")])
    assert session_activity(project, SESSION, config_dir=config)["state"] == "ended"
    time.sleep(0.02)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(_entry("user", [{"type": "text", "text": "next"}])) + "\n")
    assert session_activity(project, SESSION, config_dir=config)["state"] == "working"


def test_no_assistant_is_unknown(tmp_path: Path) -> None:
    project, config, path = _session(tmp_path)
    _write(path, [_entry("user", [{"type": "text", "text": "hi"}])])
    activity = session_activity(project, SESSION, config_dir=config)
    assert activity["state"] == "unknown"
