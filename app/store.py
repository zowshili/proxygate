"""SQLite 持久化。"""
from __future__ import annotations

import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import Iterable

from .models import Proxy

_SCHEMA = """
CREATE TABLE IF NOT EXISTS proxies (
    ip TEXT NOT NULL,
    port INTEGER NOT NULL,
    protocol TEXT NOT NULL,
    country TEXT DEFAULT '',
    source TEXT DEFAULT '',
    latency_ms INTEGER DEFAULT 0,
    alive INTEGER,
    cn_reachable INTEGER DEFAULT 0,
    foreign_reachable INTEGER DEFAULT 0,
    fail_count INTEGER DEFAULT 0,
    success_count INTEGER DEFAULT 0,
    first_seen INTEGER DEFAULT 0,
    last_seen INTEGER DEFAULT 0,
    last_check INTEGER DEFAULT 0,
    PRIMARY KEY (ip, port, protocol)
);
CREATE INDEX IF NOT EXISTS idx_alive ON proxies(alive);
CREATE INDEX IF NOT EXISTS idx_protocol ON proxies(protocol);
CREATE INDEX IF NOT EXISTS idx_foreign ON proxies(foreign_reachable);
CREATE INDEX IF NOT EXISTS idx_last_check ON proxies(last_check);
"""


class Store:
    def __init__(self, db_path: str):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        # 用一个本地线程锁保护写连接；读用 row_factory 返回 dict
        self._lock = threading.Lock()
        self._init_schema()

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.db_path, timeout=20)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_schema(self):
        with self._lock, self._conn() as conn:
            conn.executescript(_SCHEMA)

    # ---------- 写 ----------
    def upsert_seen(self, proxies: Iterable[Proxy]) -> int:
        """批量入库：已存在则更新 last_seen/source/country；新行置 alive=NULL。返回新增数量。"""
        now = int(time.time())
        rows = list(proxies)
        if not rows:
            return 0
        added = 0
        with self._lock, self._conn() as conn:
            for p in rows:
                cur = conn.execute(
                    "SELECT 1 FROM proxies WHERE ip=? AND port=? AND protocol=?",
                    (p.ip, p.port, p.protocol),
                )
                exists = cur.fetchone() is not None
                if exists:
                    conn.execute(
                        """UPDATE proxies
                           SET last_seen=?, source=?,
                               country=CASE WHEN country='' THEN ? ELSE country END,
                               alive=CASE WHEN alive IS NULL THEN NULL ELSE alive END
                           WHERE ip=? AND port=? AND protocol=?""",
                        (now, p.source, p.country, p.ip, p.port, p.protocol),
                    )
                else:
                    conn.execute(
                        """INSERT INTO proxies
                           (ip, port, protocol, country, source,
                            latency_ms, alive, cn_reachable, foreign_reachable,
                            fail_count, success_count, first_seen, last_seen, last_check)
                           VALUES (?,?,?,?,?, 0, NULL, 0,0, 0,0, ?,?, 0)""",
                        (p.ip, p.port, p.protocol, p.country, p.source, now, now),
                    )
                    added += 1
        return added

    def mark_check_result(
        self,
        ip: str,
        port: int,
        protocol: str,
        alive: bool,
        latency_ms: int,
        cn: bool,
        foreign: bool,
        fail_threshold_delete: int,
        alive_fail_to_dead: int,
    ) -> None:
        """更新单条探活结果。"""
        now = int(time.time())
        with self._lock, self._conn() as conn:
            row = conn.execute(
                """SELECT fail_count, success_count, alive, first_seen
                   FROM proxies WHERE ip=? AND port=? AND protocol=?""",
                (ip, port, protocol),
            ).fetchone()
            if not row:
                return
            fail_count = row["fail_count"]
            success_count = row["success_count"]

            if alive:
                fail_count = 0
                success_count += 1
                new_alive = 1
            else:
                fail_count += 1
                if fail_count >= alive_fail_to_dead:
                    new_alive = 0
                else:
                    new_alive = row["alive"] if row["alive"] is not None else 0

            # 超过删除阈值：直接删
            if fail_count >= fail_threshold_delete:
                conn.execute(
                    "DELETE FROM proxies WHERE ip=? AND port=? AND protocol=?",
                    (ip, port, protocol),
                )
                return

            conn.execute(
                """UPDATE proxies
                   SET latency_ms=?, alive=?, cn_reachable=?, foreign_reachable=?,
                       fail_count=?, success_count=?, last_check=?
                   WHERE ip=? AND port=? AND protocol=?""",
                (latency_ms if alive else 0, new_alive,
                 1 if cn else 0, 1 if foreign else 0,
                 fail_count, success_count, now,
                 ip, port, protocol),
            )

    def delete_dead_old(self, older_than_seconds: int) -> int:
        """删除长期未采集到的代理（下线）。"""
        cutoff = int(time.time()) - older_than_seconds
        with self._lock, self._conn() as conn:
            cur = conn.execute(
                "DELETE FROM proxies WHERE last_seen < ? AND alive = 0",
                (cutoff,),
            )
            return cur.rowcount

    # ---------- 读 ----------
    def list_alive(self, protocol: str | None = None,
                   label: str | None = None,
                   min_latency: int | None = None,
                   limit: int = 10000) -> list[Proxy]:
        sql = "SELECT * FROM proxies WHERE alive=1"
        args: list = []
        if protocol and protocol != "all":
            sql += " AND protocol=?"
            args.append(protocol)
        if label == "cn":
            sql += " AND cn_reachable=1"
        elif label == "foreign":
            sql += " AND foreign_reachable=1"
        if min_latency is not None:
            sql += " AND latency_ms>0 AND latency_ms<=?"
            args.append(int(min_latency))
        sql += " ORDER BY latency_ms ASC, last_check DESC LIMIT ?"
        args.append(int(limit))
        with self._conn() as conn:
            return [self._row_to_proxy(r) for r in conn.execute(sql, args).fetchall()]

    def list_pending_check(self, include_alive: bool, limit: int = 10000) -> list[Proxy]:
        """待探活：include_alive=True 时取 alive=1 或 NULL（新+保活）；
        排序策略：alive=1 的优先（按 last_check ASC 把最久未检的排前），
        再检 alive=NULL 的。这样每个周期先刷新已存活的，再批量检新代理。"""
        if include_alive:
            sql = "SELECT * FROM proxies WHERE alive=1 OR alive IS NULL"
        else:
            sql = "SELECT * FROM proxies"
        sql += " ORDER BY alive DESC, last_check ASC LIMIT ?"
        with self._conn() as conn:
            return [self._row_to_proxy(r) for r in conn.execute(sql, (limit,)).fetchall()]

    def stats(self) -> dict:
        with self._conn() as conn:
            total = conn.execute("SELECT COUNT(*) c FROM proxies").fetchone()["c"]
            alive = conn.execute("SELECT COUNT(*) c FROM proxies WHERE alive=1").fetchone()["c"]
            foreign = conn.execute(
                "SELECT COUNT(*) c FROM proxies WHERE alive=1 AND foreign_reachable=1"
            ).fetchone()["c"]
            cn = conn.execute(
                "SELECT COUNT(*) c FROM proxies WHERE alive=1 AND cn_reachable=1"
            ).fetchone()["c"]
            last_fetch = conn.execute(
                "SELECT MAX(last_seen) m FROM proxies"
            ).fetchone()["m"] or 0
            last_check = conn.execute(
                "SELECT MAX(last_check) m FROM proxies"
            ).fetchone()["m"] or 0
            by_proto = {
                r["protocol"]: r["c"]
                for r in conn.execute(
                    "SELECT protocol, COUNT(*) c FROM proxies WHERE alive=1 GROUP BY protocol"
                ).fetchall()
            }
            by_source = {
                r["source"]: r["c"]
                for r in conn.execute(
                    "SELECT source, COUNT(*) c FROM proxies WHERE alive=1 GROUP BY source"
                ).fetchall()
            }
        return {
            "total": total,
            "alive": alive,
            "alive_foreign_reachable": foreign,
            "alive_cn_reachable": cn,
            "by_protocol": by_proto,
            "by_source": by_source,
            "last_fetch_at": last_fetch,
            "last_check_at": last_check,
        }

    @staticmethod
    def _row_to_proxy(r: sqlite3.Row) -> Proxy:
        return Proxy(
            ip=r["ip"], port=r["port"], protocol=r["protocol"],
            country=r["country"], source=r["source"],
            latency_ms=r["latency_ms"], alive=r["alive"],
            cn_reachable=r["cn_reachable"], foreign_reachable=r["foreign_reachable"],
            fail_count=r["fail_count"], success_count=r["success_count"],
            first_seen=r["first_seen"], last_seen=r["last_seen"], last_check=r["last_check"],
        )
