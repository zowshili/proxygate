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
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=15000")
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
        """批量入库：已存在则更新 last_seen/source/country；新行置 alive=NULL。返回新增数量。

        逐行 SELECT+INSERT/UPDATE 慢路径换成 executmany + 幂等 SQL，
        新增数通过 changes() 一次性取（3742 行从 ~4s 降到 <0.3s）。
        """
        now = int(time.time())
        rows = [(p.ip, p.port, p.protocol, p.country, p.source, now, now) for p in proxies]
        if not rows:
            return 0
        with self._lock, self._conn() as conn:
            before = conn.execute("SELECT COUNT(*) c FROM proxies").fetchone()["c"]
            conn.executemany(
                """INSERT INTO proxies
                   (ip, port, protocol, country, source,
                    latency_ms, alive, cn_reachable, foreign_reachable,
                    fail_count, success_count, first_seen, last_seen, last_check)
                   VALUES (?,?,?,?,?, 0, NULL, 0,0, 0,0, ?,?, 0)
                   ON CONFLICT(ip, port, protocol) DO UPDATE SET
                       last_seen=excluded.last_seen,
                       source=excluded.source,
                       country=CASE WHEN proxies.country='' THEN excluded.country
                                    ELSE proxies.country END,
                       -- 重新抓到的代理重置为"未检"，交给下一轮保活快速复查；
                       -- fail_count 保留（连续失败累计到阈值后照常删除）
                       alive=NULL
                """,
                rows,
            )
            after = conn.execute("SELECT COUNT(*) c FROM proxies").fetchone()["c"]
        return after - before

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
        """单条探活结果（保留兼容），内部走批量接口。"""
        self.mark_check_results_bulk(
            [(ip, port, protocol, alive, latency_ms, cn, foreign)],
            fail_threshold_delete, alive_fail_to_dead,
        )

    def mark_check_results_bulk(
        self,
        results: list[tuple],
        fail_threshold_delete: int,
        alive_fail_to_dead: int,
    ) -> None:
        """批量写入探活结果（一个事务一次提交）。

        results: (ip, port, protocol, alive, latency_ms, cn, foreign)
          latency_ms = -1 表示"策略淘汰"（如延迟超上限）：立即置 alive=0，不走失败宽限。

        逐条 commit 在 500 并发下会阻塞事件循环（实测 300 条 3.3s），批量后
        每批 1 次 commit，探活吞吐从 ~26/s 提升到网络上限。
        """
        if not results:
            return
        now = int(time.time())
        updates = []
        deletes = []
        for ip, port, protocol, alive, latency_ms, cn, foreign in results:
            force_dead = latency_ms < 0
            if force_dead:
                latency_ms = 0
            updates.append((
                alive, latency_ms,
                alive, force_dead, alive_fail_to_dead,
                1 if cn else 0, 1 if foreign else 0,
                alive, alive, now,
                ip, port, protocol,
            ))
            if not alive:
                deletes.append((ip, port, protocol, fail_threshold_delete))
        with self._lock, self._conn() as conn:
            conn.executemany(
                """UPDATE proxies SET
                     -- 失败时保留上次成功延迟（宽限期内 alive 仍为 1，
                     -- 清零会导致僵尸行延迟=0 排到订阅最前）
                     latency_ms = CASE WHEN ? THEN ? ELSE latency_ms END,
                     alive = CASE
                               WHEN ? THEN 1
                               WHEN ? THEN 0
                               WHEN fail_count + 1 >= ? THEN 0
                               ELSE COALESCE(alive, 0)
                             END,
                     cn_reachable = ?,
                     foreign_reachable = ?,
                     fail_count = CASE WHEN ? THEN 0 ELSE fail_count + 1 END,
                     success_count = CASE WHEN ? THEN success_count + 1 ELSE success_count END,
                     last_check = ?
                   WHERE ip=? AND port=? AND protocol=?""",
                updates,
            )
            if deletes:
                conn.executemany(
                    """DELETE FROM proxies
                       WHERE ip=? AND port=? AND protocol=? AND fail_count >= ?""",
                    deletes,
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
