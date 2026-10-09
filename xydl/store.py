"""SQLite 持久化层。

为什么要落库：代下载是「异步 + 多进程」的业务 —— 控制台 HTTP 线程在写，下载
worker 线程也在写。SQLite 开 WAL 模式后足够用，而且零运维。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable

from .models import Order, OrderStatus

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    external_id     TEXT NOT NULL UNIQUE,
    channel         TEXT NOT NULL DEFAULT '',
    conversation_id TEXT NOT NULL DEFAULT '',
    sender          TEXT NOT NULL DEFAULT '',
    text            TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'received',
    stage           TEXT NOT NULL DEFAULT '',
    progress        REAL NOT NULL DEFAULT 0,
    links           TEXT NOT NULL DEFAULT '[]',
    chosen_url      TEXT NOT NULL DEFAULT '',
    title           TEXT NOT NULL DEFAULT '',
    file_path       TEXT NOT NULL DEFAULT '',
    file_size       INTEGER NOT NULL DEFAULT 0,
    reply           TEXT NOT NULL DEFAULT '',
    error           TEXT NOT NULL DEFAULT '',
    meta            TEXT NOT NULL DEFAULT '{}',
    replied_at      REAL,
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_orders_status  ON orders(status);
CREATE INDEX IF NOT EXISTS idx_orders_conv    ON orders(conversation_id);
CREATE INDEX IF NOT EXISTS idx_orders_created ON orders(created_at DESC);

CREATE TABLE IF NOT EXISTS events (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL,
    ts       REAL NOT NULL,
    level    TEXT NOT NULL DEFAULT 'info',
    message  TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_events_order ON events(order_id, id);

CREATE TABLE IF NOT EXISTS outbox (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id        INTEGER NOT NULL,
    conversation_id TEXT NOT NULL DEFAULT '',
    text            TEXT NOT NULL DEFAULT '',
    file_path       TEXT NOT NULL DEFAULT '',
    delivered       INTEGER NOT NULL DEFAULT 0,
    ts              REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_outbox_pending ON outbox(delivered, id);
"""

_COLUMNS = (
    "external_id", "channel", "conversation_id", "sender", "text", "status",
    "stage", "progress", "links", "chosen_url", "title", "file_path",
    "file_size", "reply", "error", "meta", "created_at", "updated_at",
)

#: 新加的列必须登记在这里，否则读老库会漏字段。
#: 形如 (列名, 建表时该列的 DDL 片段)。
_MIGRATIONS: tuple[tuple[str, str], ...] = (
    ("meta", "ALTER TABLE orders ADD COLUMN meta TEXT NOT NULL DEFAULT '{}'"),
)


class Store:
    """订单仓库。线程安全（每线程一个连接 + 写锁）。"""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._write_lock = threading.RLock()
        self._init_schema()

    # ── 连接管理 ───────────────────────────────────────────────────
    @property
    def conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(str(self.db_path), timeout=30.0, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=30000")
            self._local.conn = conn
        return conn

    def _init_schema(self) -> None:
        with self._write_lock:
            self.conn.executescript(SCHEMA)
            self._apply_migrations()

    def _apply_migrations(self) -> None:
        """给已存在的老库补列。

        ``CREATE TABLE IF NOT EXISTS`` 对已存在的表什么都不做，所以任何新增列
        都必须在这里显式补上，否则老用户升级后会读不到新字段。
        """
        for column, ddl in _MIGRATIONS:
            try:
                self.conn.execute(f"SELECT {column} FROM orders LIMIT 1")
            except sqlite3.OperationalError:
                self.conn.execute(ddl)

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    # ── 行 ↔ 对象 ──────────────────────────────────────────────────
    @staticmethod
    def _row_to_order(row: sqlite3.Row) -> Order:
        try:
            links = json.loads(row["links"] or "[]")
        except json.JSONDecodeError:
            links = []
        try:
            meta = json.loads(row["meta"] or "{}")
        except (json.JSONDecodeError, IndexError):
            meta = {}
        if not isinstance(meta, dict):
            meta = {}
        return Order(
            id=row["id"],
            external_id=row["external_id"],
            channel=row["channel"],
            conversation_id=row["conversation_id"],
            sender=row["sender"],
            text=row["text"],
            status=row["status"],
            stage=row["stage"],
            progress=row["progress"],
            links=links,
            chosen_url=row["chosen_url"],
            title=row["title"],
            file_path=row["file_path"],
            file_size=row["file_size"],
            reply=row["reply"],
            error=row["error"],
            meta=meta,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    # ── 写 ─────────────────────────────────────────────────────────
    def create_order(
        self,
        external_id: str,
        channel: str,
        conversation_id: str,
        text: str,
        sender: str = "",
        links: Iterable[dict[str, Any]] | None = None,
        meta: dict[str, Any] | None = None,
    ) -> tuple[Order, bool]:
        """新建订单。返回 (order, created)；external_id 重复时返回已存在的订单。"""
        now = time.time()
        payload = json.dumps(list(links or []), ensure_ascii=False)
        meta_payload = json.dumps(meta or {}, ensure_ascii=False)
        with self._write_lock:
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO orders "
                "(external_id, channel, conversation_id, sender, text, status, links,"
                " meta, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (external_id, channel, conversation_id, sender, text,
                 OrderStatus.RECEIVED.value, payload, meta_payload, now, now),
            )
            created = cur.rowcount > 0
            if created:
                order_id = int(cur.lastrowid)
            else:
                row = self.conn.execute(
                    "SELECT id FROM orders WHERE external_id=?", (external_id,)
                ).fetchone()
                order_id = int(row["id"])
        order = self.get(order_id)
        assert order is not None
        if created:
            self.log(order_id, f"接单：{text[:80]}")
        return order, created

    def update(self, order_id: int, **fields: Any) -> None:
        """更新订单字段；links / meta 可以是 dict/list，自动序列化。"""
        allowed = {k: v for k, v in fields.items() if k in _COLUMNS}
        if not allowed:
            return
        if "links" in allowed and not isinstance(allowed["links"], str):
            allowed["links"] = json.dumps(allowed["links"], ensure_ascii=False)
        if "meta" in allowed and not isinstance(allowed["meta"], str):
            allowed["meta"] = json.dumps(allowed["meta"] or {}, ensure_ascii=False)
        allowed["updated_at"] = time.time()
        assignments = ", ".join(f"{k}=?" for k in allowed)
        params = list(allowed.values()) + [order_id]
        with self._write_lock:
            self.conn.execute(f"UPDATE orders SET {assignments} WHERE id=?", params)

    def mark_replied(self, order_id: int) -> None:
        with self._write_lock:
            self.conn.execute(
                "UPDATE orders SET replied_at=?, updated_at=? WHERE id=?",
                (time.time(), time.time(), order_id),
            )

    def log(self, order_id: int, message: str, level: str = "info") -> None:
        with self._write_lock:
            self.conn.execute(
                "INSERT INTO events (order_id, ts, level, message) VALUES (?,?,?,?)",
                (order_id, time.time(), level, message),
            )

    def push_outbox(self, order_id: int, conversation_id: str, text: str,
                    file_path: str = "") -> int:
        """把要回给买家的话塞进 outbox，由渠道层消费。"""
        with self._write_lock:
            cur = self.conn.execute(
                "INSERT INTO outbox (order_id, conversation_id, text, file_path, ts)"
                " VALUES (?,?,?,?,?)",
                (order_id, conversation_id, text, file_path, time.time()),
            )
            return int(cur.lastrowid)

    def pop_outbox(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._write_lock:
            rows = self.conn.execute(
                "SELECT * FROM outbox WHERE delivered=0 ORDER BY id LIMIT ?", (limit,)
            ).fetchall()
            if rows:
                ids = ",".join("?" for _ in rows)
                self.conn.execute(f"UPDATE outbox SET delivered=1 WHERE id IN ({ids})",
                                  [r["id"] for r in rows])
        return [dict(r) for r in rows]

    # ── 读 ─────────────────────────────────────────────────────────
    def get(self, order_id: int) -> Order | None:
        row = self.conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        return self._row_to_order(row) if row else None

    def get_by_external_id(self, external_id: str) -> Order | None:
        row = self.conn.execute(
            "SELECT * FROM orders WHERE external_id=?", (external_id,)
        ).fetchone()
        return self._row_to_order(row) if row else None

    def list_orders(self, status: str | None = None, channel: str | None = None,
                    limit: int = 100, offset: int = 0,
                    search: str = "") -> list[Order]:
        clauses, params = [], []
        if status == "active":
            placeholders = ",".join("?" for _ in range(6))
            clauses.append(
                f"status IN ({placeholders})"
            )
            params.extend([
                OrderStatus.RECEIVED.value, OrderStatus.RESOLVING.value,
                OrderStatus.RESOLVED.value, OrderStatus.DOWNLOADING.value,
                OrderStatus.PACKAGING.value, OrderStatus.NEED_MANUAL.value,
            ])
        elif status:
            clauses.append("status=?")
            params.append(status)
        if channel:
            clauses.append("channel=?")
            params.append(channel)
        if search:
            clauses.append("(text LIKE ? OR title LIKE ? OR conversation_id LIKE ?)")
            like = f"%{search}%"
            params.extend([like, like, like])
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.extend([limit, offset])
        rows = self.conn.execute(
            f"SELECT * FROM orders {where} ORDER BY id DESC LIMIT ? OFFSET ?", params
        ).fetchall()
        return [self._row_to_order(r) for r in rows]

    def events(self, order_id: int, limit: int = 200) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT ts, level, message FROM events WHERE order_id=? ORDER BY id LIMIT ?",
            (order_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def stats(self) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT status, COUNT(*) AS n FROM orders GROUP BY status"
        ).fetchall()
        out = {r["status"]: r["n"] for r in rows}
        out["total"] = sum(out.values())
        out["active"] = sum(
            out.get(s.value, 0) for s in (
                OrderStatus.RECEIVED, OrderStatus.RESOLVING, OrderStatus.RESOLVED,
                OrderStatus.DOWNLOADING, OrderStatus.PACKAGING, OrderStatus.NEED_MANUAL,
            )
        )
        return out
