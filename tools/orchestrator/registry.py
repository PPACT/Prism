"""P1 注册表：SQLite WAL + busy_timeout，``agents`` 表原子 upsert。

`role` 是稳定主键；`pgid` 在 Windows 上存根进程 pid（真正的进程组由 Job 承担），
`job_id` 标识 daemon 持有的 Job Object。
"""

from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

BUSY_TIMEOUT_MS = 5000
_COLUMNS = (
    "role",
    "pid",
    "identity_pid",
    "generation",
    "pgid",
    "job_id",
    "status",
    "started_at",
    "last_heartbeat",
    "command",
    "updated_at",
    "current_task_id",
    "cost_tokens",
    "cost_budget",
    "project",
)


def utc_now() -> str:
    # 毫秒/微秒精度：心跳与 ACK 超时需要同格式字符串比较。
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@dataclass(frozen=True)
class AgentRecord:
    role: str
    pid: int
    pgid: int
    job_id: str
    status: str
    started_at: str
    last_heartbeat: str
    command: str
    updated_at: str
    current_task_id: str = ""
    cost_tokens: int = 0
    cost_budget: int | None = None
    project: str = ""
    # 身份锚点：跨 /clear 仍存活的宿主进程（Claude 父进程）；0 时回退 pid。
    identity_pid: int = 0
    # 物理删除后的重建代数；每次删除 +1，确保同名重建拿到全新 agent_id。
    generation: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _row_to_record(row: sqlite3.Row) -> AgentRecord:
    budget = row["cost_budget"]
    return AgentRecord(
        role=str(row["role"]),
        pid=int(row["pid"]),
        identity_pid=int(row["identity_pid"] or 0),
        generation=int(row["generation"] or 0),
        pgid=int(row["pgid"]),
        job_id=str(row["job_id"]),
        status=str(row["status"]),
        started_at=str(row["started_at"]),
        last_heartbeat=str(row["last_heartbeat"]),
        command=str(row["command"]),
        updated_at=str(row["updated_at"]),
        current_task_id=str(row["current_task_id"] or ""),
        cost_tokens=int(row["cost_tokens"] or 0),
        cost_budget=int(budget) if budget is not None else None,
        project=str(row["project"] or ""),
    )


class Registry:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.db_path,
            timeout=BUSY_TIMEOUT_MS / 1000.0,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        return connection

    def _init(self) -> None:
        connection = self._connect()
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS agents ("
                "role TEXT PRIMARY KEY, "
                "pid INTEGER NOT NULL, "
                "identity_pid INTEGER NOT NULL DEFAULT 0, "
                "generation INTEGER NOT NULL DEFAULT 0, "
                "pgid INTEGER NOT NULL, "
                "job_id TEXT NOT NULL, "
                "status TEXT NOT NULL, "
                "started_at TEXT NOT NULL, "
                "last_heartbeat TEXT NOT NULL, "
                "command TEXT NOT NULL, "
                "updated_at TEXT NOT NULL, "
                "current_task_id TEXT NOT NULL DEFAULT '', "
                "cost_tokens INTEGER NOT NULL DEFAULT 0, "
                "cost_budget INTEGER, "
                "project TEXT NOT NULL DEFAULT '')"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS role_generations ("
                "role TEXT PRIMARY KEY, "
                "generation INTEGER NOT NULL DEFAULT 0)"
            )
            existing = {
                str(row[1]) for row in connection.execute("PRAGMA table_info(agents)").fetchall()
            }
            migrations = (
                ("identity_pid", "INTEGER NOT NULL DEFAULT 0"),
                ("generation", "INTEGER NOT NULL DEFAULT 0"),
                ("current_task_id", "TEXT NOT NULL DEFAULT ''"),
                ("cost_tokens", "INTEGER NOT NULL DEFAULT 0"),
                ("cost_budget", "INTEGER"),
                ("project", "TEXT NOT NULL DEFAULT ''"),
            )
            for column, definition in migrations:
                if column not in existing:
                    connection.execute(f"ALTER TABLE agents ADD COLUMN {column} {definition}")
        finally:
            connection.close()

    def upsert(self, record: AgentRecord) -> None:
        connection = self._connect()
        try:
            connection.execute(
                "INSERT INTO agents(" + ", ".join(_COLUMNS) + ") "
                "VALUES(" + ", ".join("?" for _ in _COLUMNS) + ") "
                "ON CONFLICT(role) DO UPDATE SET "
                "pid=excluded.pid, identity_pid=excluded.identity_pid, "
                "generation=excluded.generation, pgid=excluded.pgid, "
                "job_id=excluded.job_id, "
                "status=excluded.status, started_at=excluded.started_at, "
                "last_heartbeat=excluded.last_heartbeat, command=excluded.command, "
                "updated_at=excluded.updated_at, project=excluded.project",
                tuple(getattr(record, column) for column in _COLUMNS),
            )
        finally:
            connection.close()

    def get(self, role: str) -> AgentRecord | None:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT " + ", ".join(_COLUMNS) + " FROM agents WHERE role=?", (role,)
            ).fetchone()
        finally:
            connection.close()
        return _row_to_record(row) if row is not None else None

    def list(self) -> list[AgentRecord]:
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT " + ", ".join(_COLUMNS) + " FROM agents ORDER BY role"
            ).fetchall()
        finally:
            connection.close()
        return [_row_to_record(row) for row in rows]

    def set_status(self, role: str, status: str) -> bool:
        connection = self._connect()
        now = utc_now()
        try:
            cursor = connection.execute(
                "UPDATE agents SET status=?, last_heartbeat=?, updated_at=? WHERE role=?",
                (status, now, now, role),
            )
            changed = cursor.rowcount > 0
        finally:
            connection.close()
        return changed

    def delete(self, role: str) -> bool:
        """从列表中彻底移除一个角色（不影响外部进程本身）。"""
        role = role.strip()
        if not role:
            return False
        connection = self._connect()
        try:
            cursor = connection.execute("DELETE FROM agents WHERE role=?", (role,))
            connection.commit()
            return bool(cursor.rowcount)
        finally:
            connection.close()

    def generation(self, role: str) -> int:
        """角色重建代数：物理删除后 +1，保证同名重建不会复用旧 agent_id。"""
        role = role.strip()
        if not role:
            return 0
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT generation FROM role_generations WHERE role=?", (role,)
            ).fetchone()
            return int(row["generation"]) if row is not None else 0
        finally:
            connection.close()

    def bump_generation(self, role: str) -> int:
        role = role.strip()
        if not role:
            return 0
        connection = self._connect()
        try:
            connection.execute(
                "INSERT INTO role_generations(role, generation) VALUES(?, 1) "
                "ON CONFLICT(role) DO UPDATE SET generation=generation+1",
                (role,),
            )
            connection.commit()
            row = connection.execute(
                "SELECT generation FROM role_generations WHERE role=?", (role,)
            ).fetchone()
            return int(row["generation"]) if row is not None else 0
        finally:
            connection.close()

    def heartbeat(self, role: str) -> bool:
        connection = self._connect()
        now = utc_now()
        try:
            cursor = connection.execute(
                "UPDATE agents SET last_heartbeat=?, updated_at=? WHERE role=?",
                (now, now, role),
            )
            changed = cursor.rowcount > 0
        finally:
            connection.close()
        return changed

    def set_current_task(self, role: str, task_id: str) -> bool:
        connection = self._connect()
        try:
            cursor = connection.execute(
                "UPDATE agents SET current_task_id=?, updated_at=? WHERE role=?",
                (task_id, utc_now(), role),
            )
            changed = cursor.rowcount > 0
        finally:
            connection.close()
        return changed
