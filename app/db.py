"""SQLite 持久化层。

设计要点
========
* 全部写入经同一把可重入锁串行化，配合 ``check-then-write`` 逻辑保证
  重复实例不重复计数、批状态迁移只发生一次。
* ``instances`` 表对 ``(batch_id, sop_instance_uid)`` 建唯一索引：
  清单登记和回传登记都靠它天然去重。
* 文件与登记在同一事务边界内处理：调用方先把文件写好并 fsync，再调用
  ``register_instance``；登记失败时调用方负责删除已写文件，保证
  “文件写完且登记成功才回复 C-STORE 成功”。
"""
from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

# ---- 批状态（终态不可变，迟到回传无法恢复失败批）----
STATE_PENDING = "PENDING"        # 已创建，尚未开始 C-FIND
STATE_FINDING = "FINDING"        # C-FIND 固定清单中
STATE_MOVING = "MOVING"          # C-MOVE 进行中，等待 C-STORE 回传
STATE_COMPLETED = "COMPLETED"    # 清单收齐且 C-MOVE 成功，可下载
STATE_FAILED = "FAILED"          # 终态失败（FIND/MOVE/保存/重启中断/缺件）

TERMINAL_STATES = {STATE_COMPLETED, STATE_FAILED}

# 进程级单例（main 启动时注入），供 storage 等模块反向访问
_DB_SINGLETON: "Database | None" = None


def set_db(db: "Database") -> None:
    global _DB_SINGLETON
    _DB_SINGLETON = db


def get_db() -> "Database":
    if _DB_SINGLETON is None:
        raise RuntimeError("数据库尚未初始化")
    return _DB_SINGLETON


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            str(path), check_same_thread=False, timeout=30.0, isolation_level=None
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA synchronous=FULL;")
        self._conn.execute("PRAGMA foreign_keys=ON;")
        self._create_schema()

    # ------------------------------------------------------------------ schema
    def _create_schema(self) -> None:
        with self._lock, self._conn:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS batches (
                    id                  TEXT PRIMARY KEY,
                    study_instance_uid  TEXT NOT NULL,
                    state               TEXT NOT NULL,
                    expected_count      INTEGER NOT NULL DEFAULT 0,
                    received_count      INTEGER NOT NULL DEFAULT 0,
                    failure_reason      TEXT,
                    zip_path            TEXT,
                    fault_inject        TEXT NOT NULL DEFAULT '',
                    created_at          TEXT NOT NULL,
                    updated_at          TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_batches_study
                    ON batches(study_instance_uid);

                CREATE TABLE IF NOT EXISTS instances (
                    batch_id            TEXT NOT NULL
                        REFERENCES batches(id) ON DELETE CASCADE,
                    sop_instance_uid    TEXT NOT NULL,
                    sop_class_uid       TEXT NOT NULL,
                    transfer_syntax     TEXT NOT NULL,
                    file_path           TEXT NOT NULL,
                    received_at         TEXT NOT NULL,
                    size_bytes          INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (batch_id, sop_instance_uid)
                );

                CREATE TABLE IF NOT EXISTS events (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_id    TEXT,
                    kind        TEXT NOT NULL,
                    message     TEXT NOT NULL,
                    created_at  TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_events_batch
                    ON events(batch_id, id);
                """
            )
            self._migrate_schema()

    def _migrate_schema(self) -> None:
        """对既有数据卷做轻量迁移（SQLite 不支持 IF NOT EXISTS 加列）。"""
        cols = {
            r["name"] for r in self._conn.execute(
                "PRAGMA table_info(instances)"
            ).fetchall()
        }
        if "size_bytes" not in cols:
            self._conn.execute(
                "ALTER TABLE instances ADD COLUMN size_bytes "
                "INTEGER NOT NULL DEFAULT 0"
            )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------ events
    def add_event(self, kind: str, message: str, batch_id: str | None = None) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO events(batch_id, kind, message, created_at) "
                "VALUES (?, ?, ?, ?)",
                (batch_id, kind, message, _now()),
            )

    def list_events(self, batch_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT kind, message, created_at FROM events "
                "WHERE batch_id = ? ORDER BY id",
                (batch_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------ batches
    def create_batch(
        self, batch_id: str, study_instance_uid: str, fault_inject: str
    ) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO batches(id, study_instance_uid, state, fault_inject, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (batch_id, study_instance_uid, STATE_PENDING, fault_inject,
                 _now(), _now()),
            )
        self.add_event("BATCH_CREATED", f"批创建，Study={study_instance_uid}", batch_id)

    def get_batch(self, batch_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM batches WHERE id = ?", (batch_id,)
            ).fetchone()
        return dict(row) if row else None

    def list_batches(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM batches ORDER BY rowid DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def has_active_batch_for_study(self, study_instance_uid: str) -> bool:
        """同一 Study 已有未终态批时拒绝重复提交。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM batches WHERE study_instance_uid = ? "
                "AND state NOT IN (?, ?) LIMIT 1",
                (study_instance_uid, STATE_COMPLETED, STATE_FAILED),
            ).fetchone()
        return row is not None

    def transition_state(
        self,
        batch_id: str,
        expected: Iterable[str],
        new_state: str,
        *,
        failure_reason: str | None = None,
        expected_count: int | None = None,
        zip_path: str | None = None,
    ) -> bool:
        """仅当当前状态属于 ``expected`` 时迁移，返回是否发生了迁移。

        终态（COMPLETED / FAILED）不可再被改写——这是“迟到回传不能恢复
        失败批”的数据库级保证。
        """
        expected = set(expected)
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT state FROM batches WHERE id = ?", (batch_id,)
            ).fetchone()
            if row is None or row["state"] not in expected:
                return False
            # 数据库级终态保护：COMPLETED / FAILED 一旦落定不可迁出，
            # 任何迟到事件都无法“复活”一个失败批。
            if row["state"] in TERMINAL_STATES and new_state != row["state"]:
                return False
            self._conn.execute(
                "UPDATE batches SET state = ?, failure_reason = COALESCE(?, failure_reason), "
                "zip_path = COALESCE(?, zip_path), updated_at = ? WHERE id = ?",
                (new_state, failure_reason, zip_path, _now(), batch_id),
            )
            if expected_count is not None:
                self._conn.execute(
                    "UPDATE batches SET expected_count = ? WHERE id = ?",
                    (expected_count, batch_id),
                )
        return True

    def fail_batch(self, batch_id: str, reason: str) -> bool:
        ok = self.transition_state(
            batch_id,
            [STATE_PENDING, STATE_FINDING, STATE_MOVING],
            STATE_FAILED,
            failure_reason=reason,
        )
        if ok:
            self.add_event("BATCH_FAILED", reason, batch_id)
        return ok

    def complete_batch(self, batch_id: str, zip_path: str) -> bool:
        ok = self.transition_state(
            batch_id, [STATE_MOVING], STATE_COMPLETED, zip_path=zip_path
        )
        if ok:
            self.add_event("BATCH_COMPLETED", "清单收齐，ZIP 已生成", batch_id)
        return ok

    # ------------------------------------------------------------------ manifest
    def set_manifest(
        self, batch_id: str, items: list[tuple[str, str, str]]
    ) -> None:
        """登记 C-FIND 固定下来的 SOP 清单。

        ``items`` 元素为 ``(sop_instance_uid, sop_class_uid, transfer_syntax)``。
        同一批只允许固定一次。
        """
        with self._lock, self._conn:
            exists = self._conn.execute(
                "SELECT 1 FROM instances WHERE batch_id = ? LIMIT 1", (batch_id,)
            ).fetchone()
            if exists:
                raise RuntimeError("manifest already fixed")
            self._conn.executemany(
                "INSERT INTO instances(batch_id, sop_instance_uid, sop_class_uid, "
                "transfer_syntax, file_path, received_at) VALUES (?, ?, ?, ?, '', '')",
                [(batch_id, sop, cls, ts) for sop, cls, ts in items],
            )
            self._conn.execute(
                "UPDATE batches SET expected_count = ?, updated_at = ? WHERE id = ?",
                (len(items), _now(), batch_id),
            )

    def manifest_uids(self, batch_id: str) -> set[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT sop_instance_uid FROM instances WHERE batch_id = ?",
                (batch_id,),
            ).fetchall()
        return {r["sop_instance_uid"] for r in rows}

    def is_manifest_uid(self, batch_id: str, sop_uid: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM instances WHERE batch_id = ? AND sop_instance_uid = ?",
                (batch_id, sop_uid),
            ).fetchone()
        return row is not None

    def is_already_received(self, batch_id: str, sop_uid: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM instances WHERE batch_id = ? AND sop_instance_uid = ? "
                "AND file_path != ''",
                (batch_id, sop_uid),
            ).fetchone()
        return row is not None

    def register_instance(
        self,
        batch_id: str,
        sop_uid: str,
        file_path: str,
        transfer_syntax: str,
        size_bytes: int = 0,
    ) -> bool:
        """登记一个已落盘实例。返回 False 表示重复投送（不重复计数）。"""
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT file_path FROM instances WHERE batch_id = ? AND sop_instance_uid = ?",
                (batch_id, sop_uid),
            ).fetchone()
            if row is None:
                return False  # 不在清单内，调用方应拒绝
            if row["file_path"] != "":
                return False  # 重复实例
            self._conn.execute(
                "UPDATE instances SET file_path = ?, transfer_syntax = ?, "
                "size_bytes = ?, received_at = ? WHERE batch_id = ? AND sop_instance_uid = ?",
                (file_path, transfer_syntax, size_bytes, _now(),
                 batch_id, sop_uid),
            )
            self._conn.execute(
                "UPDATE batches SET received_count = received_count + 1, updated_at = ? "
                "WHERE id = ?",
                (_now(), batch_id),
            )
        return True

    def clear_instance_registration(self, batch_id: str, sop_uid: str) -> None:
        """重启恢复：文件缺失时清空登记（回到“清单内未接收”状态）。"""
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE instances SET file_path = '', received_at = '', "
                "size_bytes = 0 WHERE batch_id = ? AND sop_instance_uid = ?",
                (batch_id, sop_uid),
            )
            self._conn.execute(
                "UPDATE batches SET received_count = (SELECT COUNT(*) FROM instances "
                "WHERE batch_id = ? AND file_path != '') WHERE id = ?",
                (batch_id, batch_id),
            )

    def received_progress(self, batch_id: str) -> tuple[int, int]:
        with self._lock:
            row = self._conn.execute(
                "SELECT expected_count, received_count FROM batches WHERE id = ?",
                (batch_id,),
            ).fetchone()
        if row is None:
            return (0, 0)
        return row["expected_count"], row["received_count"]

    def missing_uids(self, batch_id: str) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT sop_instance_uid FROM instances WHERE batch_id = ? AND file_path = ''",
                (batch_id,),
            ).fetchall()
        return [r["sop_instance_uid"] for r in rows]

    def received_files(self, batch_id: str) -> list[tuple[str, str]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT sop_instance_uid, file_path FROM instances "
                "WHERE batch_id = ? AND file_path != '' ORDER BY sop_instance_uid",
                (batch_id,),
            ).fetchall()
        return [(r["sop_instance_uid"], r["file_path"]) for r in rows]

    def find_batch_for_study(self, study_uid: str) -> dict[str, Any] | None:
        """按 StudyInstanceUID 路由 C-STORE 回传到对应批。

        用 rowid 而不是时间戳排序：同秒内创建多个批时顺序仍确定，
        永远命中最新的批。
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM batches WHERE study_instance_uid = ? "
                "ORDER BY rowid DESC LIMIT 1",
                (study_uid,),
            ).fetchone()
        return dict(row) if row else None

    # ------------------------------------------------------------------ recovery
    def non_terminal_batches(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM batches WHERE state NOT IN (?, ?)",
                (STATE_COMPLETED, STATE_FAILED),
            ).fetchall()
        return [dict(r) for r in rows]

    def all_completed_batches(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM batches WHERE state = ?", (STATE_COMPLETED,)
            ).fetchall()
        return [dict(r) for r in rows]
