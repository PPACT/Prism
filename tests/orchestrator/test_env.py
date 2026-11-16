"""工作台环境变量助手：PRISM_* 优先、SENTINEL_* 兜底、写入只用 PRISM_*。"""

from __future__ import annotations

import pytest

from tools.orchestrator import env as workbench_env


def test_prism_wins_over_legacy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRISM_ORCH_HOME", "new-home")
    monkeypatch.setenv("SENTINEL_ORCH_HOME", "old-home")

    assert workbench_env.get("ORCH_HOME") == "new-home"
    assert workbench_env.get("SENTINEL_ORCH_HOME") == "new-home"


def test_legacy_fallback_when_prism_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PRISM_ORCH_HOME", raising=False)
    monkeypatch.setenv("SENTINEL_ORCH_HOME", "old-home")

    assert workbench_env.get("ORCH_HOME") == "old-home"


def test_missing_returns_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PRISM_TERMINAL_POOL", raising=False)
    monkeypatch.delenv("SENTINEL_TERMINAL_POOL", raising=False)

    assert workbench_env.get("TERMINAL_POOL", "0") == "0"
    assert workbench_env.get("TERMINAL_POOL") is None


def test_set_writes_only_prism_name() -> None:
    environment: dict[str, str] = {}

    workbench_env.set("ORCH_HOME", "new-home", environment)
    workbench_env.set("SENTINEL_ROLE", "dev", environment)

    assert environment == {"PRISM_ORCH_HOME": "new-home", "PRISM_ROLE": "dev"}


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1", True),
        ("TRUE", True),
        ("yes", True),
        ("on", True),
        ("0", False),
        ("off", False),
        ("", False),
    ],
)
def test_flag_parsing(monkeypatch: pytest.MonkeyPatch, raw: str, expected: bool) -> None:
    monkeypatch.delenv("SENTINEL_TERMINAL_POOL", raising=False)
    monkeypatch.setenv("PRISM_TERMINAL_POOL", raw)

    assert workbench_env.flag("TERMINAL_POOL") is expected
