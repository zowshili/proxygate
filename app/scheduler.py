"""调度器：采集 / 保活 / 全量 / 区域判定 / 清理（v2.0.0，无 socks4 网关）。"""
from __future__ import annotations

import asyncio
import logging
import time

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from .checker import run_check_batch
from .config import AppConfig
from .fetcher import run_fetch_all
from .geo import GeoResolver
from .store import Store

log = logging.getLogger(__name__)


class Scheduler:
    def __init__(self, cfg: AppConfig, store: Store):
        self.cfg = cfg
        self.store = store
        self.geo = GeoResolver(cfg.geo, store)
        self.scheduler = AsyncIOScheduler(timezone="Asia/Shanghai")
        self._last_fetch_count = 0
        self._last_check_count = 0
        self._last_alive_count = 0
        self._last_geo: dict = {}
        self._lock = asyncio.Lock()   # fetch/check 互斥，避免叠加占用带宽

    # —— 任务实现 ——
    async def _do_fetch(self) -> dict:
        log.info("== fetch job start ==")
        try:
            proxies = await run_fetch_all(self.cfg.fetch, self.cfg.allowed_protocols)
            added = await asyncio.to_thread(self.store.upsert_seen, proxies)
            self._last_fetch_count = len(proxies)
            log.info("== fetch job done: total=%d new=%d ==", len(proxies), added)
            return {"fetched": len(proxies), "new": added}
        except Exception as e:
            log.exception("fetch job failed: %s", e)
            return {"fetched": 0, "new": 0, "error": str(e)}

    async def _do_check(self, include_alive: bool, label: str) -> dict:
        log.info("== check job [%s] start ==", label)
        result = {"checked": 0, "alive": 0}
        try:
            pending = await asyncio.to_thread(
                self.store.list_pending_check, include_alive, self.cfg.check.max_per_cycle
            )
            self._last_check_count = len(pending)
            if not pending:
                log.info("== check job [%s]: nothing to check ==", label)
                return result
            result = await run_check_batch(pending, self.cfg.check, self.store)
            self._last_alive_count = result.get("alive", 0)
            log.info("== check job [%s] done: %s ==", label, result)
        except Exception as e:
            log.exception("check job [%s] failed: %s", label, e)
            return result

        # 探活后给存活的代理补区域标签（只查新增/过期 IP，命中缓存零开销）
        try:
            self._last_geo = await self.geo.run()
        except Exception as e:
            log.warning("geo job failed: %s", e)
        return result

    async def _do_check_new(self) -> dict:
        async with self._lock:
            return await self._do_check(include_alive=True, label="new/alive")

    async def _do_check_full(self) -> dict:
        async with self._lock:
            return await self._do_check(include_alive=False, label="full")

    async def _do_fetch_locked(self) -> dict:
        async with self._lock:
            return await self._do_fetch()

    async def _do_cleanup(self) -> dict:
        """删除长期抓不到且已死的行，控制库体积。"""
        try:
            n = await asyncio.to_thread(
                self.store.delete_dead_old, self.cfg.retention_hours * 3600
            )
            log.info("cleanup: removed %d stale dead rows", n)
            return {"removed": n}
        except Exception as e:
            log.warning("cleanup failed: %s", e)
            return {"removed": 0, "error": str(e)}

    # —— 手动触发 ——
    async def trigger_update(self) -> dict:
        """立即跑一次采集 + 新探活 + 区域判定，返回结果。须在事件循环内 await。"""
        fetched = await self._do_fetch_locked()
        checked = await self._do_check_new()
        return {"fetch": fetched, "check": checked, "geo": self._last_geo}

    # —— 启动 ——
    def start(self):
        self.scheduler.add_job(
            self._do_fetch_locked,
            IntervalTrigger(
                seconds=self.cfg.fetch.interval_seconds,
                jitter=self.cfg.fetch.jitter_seconds,
            ),
            id="fetch", max_instances=1, coalesce=True,
        )
        self.scheduler.add_job(
            self._do_check_new,
            IntervalTrigger(seconds=self.cfg.check.new_interval, jitter=30),
            id="check_new", max_instances=1, coalesce=True,
        )
        self.scheduler.add_job(
            self._do_check_full,
            IntervalTrigger(seconds=self.cfg.check.full_interval, jitter=60),
            id="check_full", max_instances=1, coalesce=True,
        )
        self.scheduler.add_job(
            self._do_cleanup,
            CronTrigger(hour=4, minute=20),
            id="cleanup", max_instances=1, coalesce=True,
        )
        self.scheduler.start()
        log.info(
            "scheduler started: fetch=%ss check_new=%ss check_full=%ss regions=%s geo=%s",
            self.cfg.fetch.interval_seconds, self.cfg.check.new_interval,
            self.cfg.check.full_interval, self.cfg.regions,
            "on" if self.cfg.geo.enabled else "off",
        )

    def shutdown(self):
        try:
            self.scheduler.shutdown(wait=False)
        except Exception:
            pass
