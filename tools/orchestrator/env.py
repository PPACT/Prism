"""Workbench environment variables (Prism).

读取优先 ``PRISM_*``、回退旧名 ``SENTINEL_*``（迁移期兼容）；写入一律使用
``PRISM_*``。所有读取都应经过本模块，禁止在工作台代码里散落 ``os.environ``
前缀判断。
"""

from __future__ import annotations

import os
from collections.abc import MutableMapping
from typing import overload

ENV_PREFIX = "PRISM_"
LEGACY_PREFIX = "SENTINEL_"


def suffix(name: str) -> str:
    """去掉任一已知前缀，返回裸名（如 ``ORCH_HOME``）。"""
    for prefix in (ENV_PREFIX, LEGACY_PREFIX):
        if name.startswith(prefix):
            return name[len(prefix) :]
    return name


def name(env_suffix: str) -> str:
    """规范写入名：``ORCH_HOME`` / ``SENTINEL_ORCH_HOME`` → ``PRISM_ORCH_HOME``。"""
    return ENV_PREFIX + suffix(env_suffix)


def legacy_name(env_suffix: str) -> str:
    return LEGACY_PREFIX + suffix(env_suffix)


@overload
def get(env_suffix: str) -> str | None: ...


@overload
def get(env_suffix: str, default: str) -> str: ...


def get(env_suffix: str, default: str | None = None) -> str | None:
    """读 ``PRISM_<name>``，缺失时回退 ``SENTINEL_<name>``。"""
    value = os.environ.get(name(env_suffix))
    if value is None:
        value = os.environ.get(legacy_name(env_suffix))
    return value if value is not None else default


def set(
    env_suffix: str,
    value: str,
    environment: MutableMapping[str, str] | None = None,
) -> None:
    """写 ``PRISM_<name>``；可作用于 ``os.environ`` 或子进程环境字典。"""
    target = os.environ if environment is None else environment
    target[name(env_suffix)] = value


def flag(env_suffix: str, default: bool = False) -> bool:
    raw = (get(env_suffix) or "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}
