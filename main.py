"""proxypool 入口：启动调度器 + FastAPI。"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI

from app.config import load_config
from app.gateway import Gateway
from app.scheduler import Scheduler
from app.store import Store
from app.web import create_app


def setup_logging(level: str):
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler("logs/proxypool.log", encoding="utf-8"),
        ],
    )


def main():
    cfg = load_config(os.environ.get("PROXYPOOL_CONFIG", "config.yaml"))
    os.makedirs("logs", exist_ok=True)
    os.makedirs("data", exist_ok=True)
    setup_logging(cfg.log_level)
    log = logging.getLogger("main")

    store = Store(cfg.db)
    gateway = Gateway(cfg.gateway)
    scheduler = Scheduler(cfg, store, gateway)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # 在 uvicorn 的事件循环里启动调度器
        scheduler.start()
        # 启动后立刻跑一次采集 + 探活，避免订阅启动后为空（不阻塞服务启动）
        async def _bootstrap():
            log.info("bootstrap: first fetch + check")
            await scheduler._do_fetch()
            await scheduler._do_check_new()
            log.info("bootstrap done")
        asyncio.create_task(_bootstrap())
        yield
        await gateway.stop_all()
        scheduler.shutdown()

    app = create_app(cfg, store, scheduler, gateway, lifespan=lifespan)

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
