"""Fetcher 注册表 + 采集执行器。"""
from __future__ import annotations

import asyncio
import logging
from typing import List

import aiohttp

from ..config import FetchConfig
from ..models import Proxy
from .base import BaseFetcher
from .zdaye import ZdayeFetcher
from .ip3366 import Ip3366Fetcher
from .ip89 import Ip89Fetcher
from .kuaidaili import KuaidailiFetcher
from .qiyunip import QiyunipFetcher
from .daili66 import Daili66Fetcher
from .francevpn import FrancevpnFetcher
from .text import TextFetcher
from .proxyscdn import ProxyscdnFetcher

log = logging.getLogger(__name__)

_SPIDERS: dict[str, type[BaseFetcher]] = {
    "zdaye": ZdayeFetcher,
    "ip3366": Ip3366Fetcher,
    "89ip": Ip89Fetcher,
    "kuaidaili": KuaidailiFetcher,
    "qiyunip": QiyunipFetcher,
    "daili66": Daili66Fetcher,
    "francevpn": FrancevpnFetcher,
    "text": TextFetcher,
    "scdn": ProxyscdnFetcher,
}


def get_spider_cls(type_name: str) -> type[BaseFetcher] | None:
    return _SPIDERS.get(type_name)


async def run_fetch_all(fetch_cfg: FetchConfig, allowed_protocols: List[str]) -> List[Proxy]:
    """跑所有源，返回去重后的代理列表（含协议过滤）。"""
    headers = {
        "User-Agent": fetch_cfg.user_agent,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    conn = aiohttp.TCPConnector(limit=20, ssl=False)
    async with aiohttp.ClientSession(connector=conn, headers=headers) as session:
        tasks: List[asyncio.Task] = []
        for src in fetch_cfg.sources:
            cls = get_spider_cls(src.type)
            if not cls:
                log.warning("no spider registered for type %r", src.type)
                continue
            spider = cls(session, src, fetch_cfg)
            tasks.append(asyncio.create_task(spider.run(), name=f"fetch:{src.name}"))
        gathered = await asyncio.gather(*tasks, return_exceptions=True)

    all_proxies: List[Proxy] = []
    seen_keys: set[tuple[str, int, str]] = set()
    for src, result in zip(fetch_cfg.sources, gathered):
        if isinstance(result, Exception):
            log.warning("source %s failed: %s", src.name, result)
            continue
        for p in result:
            if p.protocol not in allowed_protocols:
                continue
            if p.port <= 0 or p.port > 65535:
                continue
            k = p.key()
            if k in seen_keys:
                continue
            seen_keys.add(k)
            all_proxies.append(p)
    log.info("fetched total %d unique proxies (after protocol filter %s)",
             len(all_proxies), allowed_protocols)
    return all_proxies
