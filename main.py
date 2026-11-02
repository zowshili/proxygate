"""ProxyGate v2.0.0 入口。

两种运行方式：
    python main.py                    常驻：调度器 + FastAPI，订阅服务
    python main.py --once --out public 单轮：采集→探活→区域判定→导出文件（CI/定时任务）

`--once` 跑完就退出，不监听端口，适合 GitHub Actions 这种无状态环境。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI

from app.checker import run_check_batch
from app.config import load_config
from app.export import export_all
from app.scheduler import Scheduler
from app.store import Store
from app.web import create_app

VERSION = "2.0.0"


def setup_logging(level: str, logfile: str = "logs/proxypool.log"):
    os.makedirs(os.path.dirname(logfile) or ".", exist_ok=True)
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(logfile, encoding="utf-8"),
        ],
    )


async def run_once(cfg, store: Store, outdir: str, fmt: str, limit: int,
                   max_rounds: int = 12) -> dict:
    """单轮全流程：采集 → 探活（排空未检队列）→ 区域判定 → 导出。"""
    log = logging.getLogger("once")
    sched = Scheduler(cfg, store)

    fetched = await sched._do_fetch_locked()
    log.info("fetch: %s", fetched)

    # 新库首轮所有节点都是"未检"，一批只探 max_per_cycle 条，循环到排空。
    # 只取 alive IS NULL 的行：不复检刚探活的节点，避免空转。
    rounds = 0
    while rounds < max_rounds:
        pending = await asyncio.to_thread(store.list_unchecked, cfg.check.max_per_cycle)
        if not pending:
            break
        result = await run_check_batch(pending, cfg.check, store)
        rounds += 1
        log.info("check round %d: %s (batch %d)", rounds, result, len(pending))
        if not result.get("checked"):
            break

    geo = await sched.geo.run()
    log.info("geo: %s", geo)

    counts = await asyncio.to_thread(export_all, cfg, store, outdir, limit, fmt)
    log.info("done: %s", counts)
    return counts


def main():
    parser = argparse.ArgumentParser(description=f"ProxyGate v{VERSION}")
    parser.add_argument("-c", "--config", default=os.environ.get("PROXYPOOL_CONFIG", "config.yaml"))
    parser.add_argument("--once", action="store_true", help="跑一轮后退出（CI 模式）")
    parser.add_argument("--out", default="public", help="--once 导出目录")
    parser.add_argument("--fmt", default="hostport", choices=["hostport", "scheme"],
                        help="txt 格式：hostport=ip:port，scheme=protocol://ip:port")
    parser.add_argument("--limit", type=int, default=20000, help="单文件最多导出条数")
    args = parser.parse_args()

    cfg = load_config(args.config)
    os.makedirs("logs", exist_ok=True)
    os.makedirs("data", exist_ok=True)
    setup_logging(cfg.log_level)
    log = logging.getLogger("main")
    log.info("ProxyGate v%s starting (mode=%s, regions=%s, geo=%s)",
             VERSION, "once" if args.once else "server",
             cfg.regions, "on" if cfg.geo.enabled else "off")

    store = Store(cfg.db)

    if args.once:
        counts = asyncio.run(run_once(cfg, store, args.out, args.fmt, args.limit))
        print("EXPORT " + json.dumps(counts, ensure_ascii=False))
        return

    scheduler = Scheduler(cfg, store)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        scheduler.start()

        async def _bootstrap():
            log.info("bootstrap: first fetch + check + geo")
            try:
                await scheduler._do_fetch_locked()
                await scheduler._do_check_new()
            except Exception as e:
                log.exception("bootstrap failed: %s", e)
            log.info("bootstrap done")

        asyncio.create_task(_bootstrap())
        yield
        scheduler.shutdown()

    app = create_app(cfg, store, scheduler, lifespan=lifespan)

    config = uvicorn.Config(
        app, host=cfg.web.host, port=cfg.web.port,
        log_level=cfg.log_level.lower(), access_log=False,
    )
    server = uvicorn.Server(config)
    try:
        server.run()
    except KeyboardInterrupt:
        pass
    finally:
        scheduler.shutdown()


if __name__ == "__main__":
    main()
