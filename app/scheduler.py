"""调度器：采集 + 探活 三独立周期。"""
from __future__ import annotations

import asyncio
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from .checker import run_check_batch
from .config import AppConfig
from .fetcher import run_fetch_all
from .gateway import Gateway
from .store import Store

log = logging.getLogger(__name__)


class Scheduler:
    def __init__(self, cfg: AppConfig, store: Store, gateway: Gateway):
        self.cfg = cfg
        self.store = store
        self.gateway = gateway
        self.loop: asyncio.AbstractEventLoop | None = None
        self.scheduler = AsyncIOScheduler(timezone="Asia/Shanghai")
        self._last_fetch_count = 0
        self._last_check_count = 0

    # —— 任务实现 ——
    async def _do_fetch(self):
        log.info("== fetch job start ==")
        try:
            proxies = await run_fetch_all(self.cfg.fetch, self.cfg.allowed_protocols)
            added = self.store.upsert_seen(proxies)
            self._last_fetch_count = len(proxies)
            log.info("== fetch job done: total=%d new=%d ==", len(proxies), added)
        except Exception as e:
            log.exception("fetch job failed: %s", e)

    async def _do_check(self, include_alive: bool, label: str):
        log.info("== check job [%s] start ==", label)
        try:
            pending = self.store.list_pending_check(include_alive=include_alive, limit=20000)
            self._last_check_count = len(pending)
            if not pending:
                log.info("== check job [%s]: nothing to check ==", label)
                return
            await run_check_batch(pending, self.cfg.check, self.store)
            log.info("== check job [%s] done ==", label)
            # 探活后同步更新 socks4 网关（加超时，不阻塞调度器）
            try:
                alive_socks4 = [p for p in self.store.list_alive(protocol="socks4", limit=20000)]
                await asyncio.wait_for(self.gateway.update(alive_socks4), timeout=30)
            except asyncio.TimeoutError:
                log.warning("gateway update timed out")
            except Exception as e:
                log.warning("gateway update failed: %s", e)
        except Exception as e:
            log.exception("check job [%s] failed: %s", label, e)

    async def _do_check_new(self):
        await self._do_check(include_alive=True, label="new/alive")

    async def _do_check_full(self):
        await self._do_check(include_alive=False, label="full")

    # —— 单次手动触发 ——
    async def trigger_update(self) -> dict:
        """立即跑一次采集 + 新探活，返回结果。须在事件循环内 await。"""
        await self._do_fetch()
        await self._do_check_new()
        return {
            "last_fetch_count": self._last_fetch_count,
            "last_check_count": self._last_check_count,
        }

    # —— 启动 ——
    def start(self):
        # 在 running loop 里调用，绑定到 uvicorn 的 loop
        self.loop = asyncio.get_running_loop()
        self.scheduler.add_job(
            self._wrap(self._do_fetch),
            IntervalTrigger(
                seconds=self.cfg.fetch.interval_seconds,
                jitter=self.cfg.fetch.jitter_seconds,
            ),
            id="fetch", max_instances=1, coalesce=True,
        )
        self.scheduler.add_job(
            self._wrap(self._do_check_new),
            IntervalTrigger(seconds=self.cfg.check.new_interval, jitter=30),
            id="check_new", max_instances=1, coalesce=True,
        )
        self.scheduler.add_job(
            self._wrap(self._do_check_full),
            IntervalTrigger(seconds=self.cfg.check.full_interval, jitter=60),
            id="check_full", max_instances=1, coalesce=True,
        )
        self.scheduler.start()
        log.info("scheduler started: fetch=%ss, check_new=%ss, check_full=%ss",
                 self.cfg.fetch.interval_seconds,
                 self.cfg.check.new_interval,
                 self.cfg.check.full_interval)

    @staticmethod
    def _wrap(coro_fn):
        async def _runner():
            await coro_fn()
        return _runner

    def shutdown(self):
        self.scheduler.shutdown(wait=False)
