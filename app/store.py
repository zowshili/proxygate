"""SQLite 持久化（v2.0.0：区域标签 + 地理缓存 + 版本号）。"""
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
    region TEXT DEFAULT '',
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
CREATE TABLE IF NOT EXISTS geo (
    ip TEXT PRIMARY KEY,
    cc TEXT NOT NULL DEFAULT '',
    updated INTEGER NOT NULL DEFAULT 0
);
"""

# 索引单独建：老库（v1，无 region 列）必须先 ALTER 再加索引，
# 否则 CREATE INDEX ... (region, ...) 会在迁移前报 no such column。
_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_alive ON proxies(alive);
CREATE INDEX IF NOT EXISTS idx_protocol ON proxies(protocol);
CREATE INDEX IF NOT EXISTS idx_foreign ON proxies(foreign_reachable);
CREATE INDEX IF NOT EXISTS idx_last_check ON proxies(last_check);
CREATE INDEX IF NOT EXISTS idx_pending ON proxies(alive, last_check);
CREATE INDEX IF NOT EXISTS idx_region_alive ON proxies(region, alive, latency_ms);
"""


class Store:
    def __init__(self, db_path: str):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self._lock = threading.Lock()
        # 写操作自增，供 Web 层做订阅缓存失效
        self._version = 0
        self._init_schema()

    @property
    def version(self) -> int:
        return self._version

    def bump_version(self) -> None:
        self._version += 1

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.db_path, timeout=20)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=15000")
        conn.execute("PRAGMA temp_store=MEMORY")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_schema(self):
        with self._lock, self._conn() as conn:
            conn.executescript(_SCHEMA)
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(proxies)")}
            if "region" not in cols:
                conn.execute("ALTER TABLE proxies ADD COLUMN region TEXT DEFAULT ''")
            conn.executescript(_INDEXES)

    # ---------- 写 ----------
    def upsert_seen(self, proxies: Iterable[Proxy]) -> int:
        """批量入库。已存在行只刷新 last_seen/source/country/region，
        **不重置 alive**：v1 每轮把 alive 置 NULL 会把 4000+ 条代理反复回炉，
        是探活开销最大的浪费。新行仍由 INSERT 置 alive=NULL 等首检。
        """
        now = int(time.time())
        rows = [
            (p.ip, p.port, p.protocol, p.country, p.source, p.region or "", now, now)
            for p in proxies
        ]
        if not rows:
            return 0
        with self._lock, self._conn() as conn:
            before = conn.execute("SELECT COUNT(*) c FROM proxies").fetchone()["c"]
            conn.executemany(
                """INSERT INTO proxies
                   (ip, port, protocol, country, source, region,
                    latency_ms, alive, cn_reachable, foreign_reachable,
                    fail_count, success_count, first_seen, last_seen, last_check)
                   VALUES (?,?,?,?,?,?, 0, NULL, 0,0, 0,0, ?,?, 0)
                   ON CONFLICT(ip, port, protocol) DO UPDATE SET
                       last_seen=excluded.last_seen,
                       source=excluded.source,
                       country=CASE WHEN proxies.country='' THEN excluded.country
                                    ELSE proxies.country END,
                       region=CASE WHEN proxies.region='' THEN excluded.region
                                   ELSE proxies.region END
                """,
                rows,
            )
            after = conn.execute("SELECT COUNT(*) c FROM proxies").fetchone()["c"]
        self.bump_version()
        return after - before

    def mark_check_results_bulk(
        self,
        results: list[tuple],
        fail_threshold_delete: int,
        alive_fail_to_dead: int,
    ) -> None:
        """批量写入探活结果（一个事务一次提交）。

        results: (ip, port, protocol, alive, latency_ms, cn, foreign)
          latency_ms = -1 表示"策略淘汰"（如延迟超上限）：立即置 alive=0。
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
        self.bump_version()

    def apply_regions(self, mapping: dict[str, str]) -> int:
        """按 IP 批量写区域标签（geo 判定结果覆盖源标签）。"""
        if not mapping:
            return 0
        rows = [(region, ip) for ip, region in mapping.items()]
        with self._lock, self._conn() as conn:
            conn.executemany("UPDATE proxies SET region=? WHERE ip=?", rows)
        self.bump_version()
        return len(rows)

    def geo_get(self, ips: list[str], ttl_seconds: int) -> dict[str, str]:
        """返回仍新鲜的地理缓存 {ip: cc}。"""
        if not ips:
            return {}
        cutoff = int(time.time()) - ttl_seconds
        out: dict[str, str] = {}
        with self._conn() as conn:
            for i in range(0, len(ips), 500):
                chunk = ips[i:i + 500]
                marks = ",".join("?" * len(chunk))
                sql = f"SELECT ip, cc FROM geo WHERE updated>=? AND ip IN ({marks})"
                for r in conn.execute(sql, (cutoff, *chunk)).fetchall():
                    out[r["ip"]] = r["cc"]
        return out

    def geo_put(self, items: list[tuple[str, str]]) -> None:
        """items: [(ip, cc)]，cc 为空表示查询失败（短 TTL 下一天重试）。"""
        if not items:
            return
        now = int(time.time())
        with self._lock, self._conn() as conn:
            conn.executemany(
                """INSERT INTO geo(ip, cc, updated) VALUES (?,?,?)
                   ON CONFLICT(ip) DO UPDATE SET cc=excluded.cc, updated=excluded.updated""",
                [(ip, cc or "", now) for ip, cc in items],
            )

    def ips_needing_geo(self, ttl_seconds: int, limit: int) -> list[str]:
        """存活代理中地理缓存缺失/过期的 IP（去重）。"""
        cutoff = int(time.time()) - ttl_seconds
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT DISTINCT p.ip FROM proxies p
                   LEFT JOIN geo g ON g.ip = p.ip
                   WHERE p.alive=1
                     AND (g.ip IS NULL OR g.updated < ? OR g.cc = '')
                   LIMIT ?""",
                (cutoff, int(limit)),
            ).fetchall()
        return [r["ip"] for r in rows]

    def delete_dead_old(self, older_than_seconds: int) -> int:
        """删除长期未采集到的（已下线）代理。"""
        cutoff = int(time.time()) - older_than_seconds
        with self._lock, self._conn() as conn:
            cur = conn.execute(
                "DELETE FROM proxies WHERE last_seen < ? AND COALESCE(alive,0)=0",
                (cutoff,),
            )
            n = cur.rowcount
        if n:
            self.bump_version()
        return n

    # ---------- 读 ----------
    def list_alive(self, protocol: str | None = None,
                   label: str | None = None,
                   region: str | None = None,
                   min_latency: int | None = None,
                   limit: int = 10000,
                   offset: int = 0) -> list[Proxy]:
        sql = "SELECT * FROM proxies WHERE alive=1"
        args: list = []
        if protocol and protocol != "all":
            sql += " AND protocol=?"
            args.append(protocol)
        if region and region != "all":
            sql += " AND region=?"
            args.append(region)
        if label == "cn":
            sql += " AND cn_reachable=1"
        elif label == "foreign":
            sql += " AND foreign_reachable=1"
        if min_latency is not None:
            sql += " AND latency_ms>0 AND latency_ms<=?"
            args.append(int(min_latency))
        sql += " ORDER BY latency_ms ASC, last_check DESC LIMIT ? OFFSET ?"
        args.append(int(limit))
        args.append(int(offset))
        with self._conn() as conn:
            return [self._row_to_proxy(r) for r in conn.execute(sql, args).fetchall()]

    def count_alive(self, region: str | None = None, protocol: str | None = None) -> int:
        sql = "SELECT COUNT(*) c FROM proxies WHERE alive=1"
        args: list = []
        if region and region != "all":
            sql += " AND region=?"
            args.append(region)
        if protocol and protocol != "all":
            sql += " AND protocol=?"
            args.append(protocol)
        with self._conn() as conn:
            return conn.execute(sql, args).fetchone()["c"]

    def list_pending_check(self, include_alive: bool, limit: int = 20000) -> list[Proxy]:
        """待探活：include_alive=True 时取 alive=1 或 NULL（新+保活）；
        排序：已存活的优先（最久未检的排前），再检新代理。"""
        if include_alive:
            sql = "SELECT * FROM proxies WHERE alive=1 OR alive IS NULL"
        else:
            sql = "SELECT * FROM proxies"
        sql += " ORDER BY alive DESC, last_check ASC LIMIT ?"
        with self._conn() as conn:
            return [self._row_to_proxy(r) for r in conn.execute(sql, (limit,)).fetchall()]

    def list_unchecked(self, limit: int = 20000) -> list[Proxy]:
        """从未探活过的行（alive IS NULL）。--once 模式靠它把队列排空。"""
        sql = "SELECT * FROM proxies WHERE alive IS NULL ORDER BY first_seen ASC LIMIT ?"
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
            last_fetch = conn.execute("SELECT MAX(last_seen) m FROM proxies").fetchone()["m"] or 0
            last_check = conn.execute("SELECT MAX(last_check) m FROM proxies").fetchone()["m"] or 0
            by_proto = {
                r["protocol"]: r["c"]
                for r in conn.execute(
                    "SELECT protocol, COUNT(*) c FROM proxies WHERE alive=1 GROUP BY protocol"
                ).fetchall()
            }
            by_region = {
                (r["region"] or "unknown"): r["c"]
                for r in conn.execute(
                    "SELECT region, COUNT(*) c FROM proxies WHERE alive=1 GROUP BY region"
                ).fetchall()
            }
            by_source = {
                r["source"]: r["c"]
                for r in conn.execute(
                    "SELECT source, COUNT(*) c FROM proxies WHERE alive=1 GROUP BY source"
                ).fetchall()
            }
            geo_cached = conn.execute("SELECT COUNT(*) c FROM geo WHERE cc<>''").fetchone()["c"]
        return {
            "total": total,
            "alive": alive,
            "alive_foreign_reachable": foreign,
            "alive_cn_reachable": cn,
            "by_protocol": by_proto,
            "by_region": by_region,
            "by_source": by_source,
            "geo_cached": geo_cached,
            "last_fetch_at": last_fetch,
            "last_check_at": last_check,
            "version": self._version,
        }

    @staticmethod
    def _row_to_proxy(r: sqlite3.Row) -> Proxy:
        return Proxy(
            ip=r["ip"], port=r["port"], protocol=r["protocol"],
            country=r["country"], source=r["source"], region=r["region"] or "",
            latency_ms=r["latency_ms"], alive=r["alive"],
            cn_reachable=r["cn_reachable"], foreign_reachable=r["foreign_reachable"],
            fail_count=r["fail_count"], success_count=r["success_count"],
            first_seen=r["first_seen"], last_seen=r["last_seen"], last_check=r["last_check"],
        )
