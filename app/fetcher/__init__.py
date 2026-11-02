"""Fetcher 注册表 + 采集执行器。"""
from __future__ import annotations

import asyncio
import logging
from typing import List

import aiohttp
from aiohttp.resolver import AsyncResolver

from ..config import FetchConfig
from ..models import Proxy
from .base import BaseFetcher
from .zdaye import ZdayeFetcher
from .kuaidaili import KuaidailiFetcher
from .qiyunip import QiyunipFetcher
from .francevpn import FrancevpnFetcher
from .text import TextFetcher
from .proxyscdn import ProxyscdnFetcher
from .geonode import GeonodeFetcher
from .seofangfa import SeofangfaFetcher

log = logging.getLogger(__name__)

_SPIDERS: dict[str, type[BaseFetcher]] = {
    "zdaye": ZdayeFetcher,
    "kuaidaili": KuaidailiFetcher,
    "qiyunip": QiyunipFetcher,
    "francevpn": FrancevpnFetcher,
    "text": TextFetcher,
    "scdn": ProxyscdnFetcher,
    "geonode": GeonodeFetcher,
    "seofangfa": SeofangfaFetcher,
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
    resolver = AsyncResolver(timeout=2.0)
    conn = aiohttp.TCPConnector(limit=20, ssl=False, resolver=resolver)
    # 注意：task 必须和 source 成对保存，否则某个 type 未注册时
    # zip(sources, gathered) 会把结果整体错位（v1 的隐藏 bug）
    pairs: list[tuple] = []
    async with aiohttp.ClientSession(connector=conn, headers=headers) as session:
        for src in fetch_cfg.sources:
            cls = get_spider_cls(src.type)
            if not cls:
                log.warning("no spider registered for type %r (source=%s)", src.type, src.name)
                continue
            spider = cls(session, src, fetch_cfg)
            pairs.append((src, asyncio.create_task(spider.run(), name=f"fetch:{src.name}")))
        gathered = await asyncio.gather(*[t for _, t in pairs], return_exceptions=True)

    all_proxies: List[Proxy] = []
    seen_keys: set[tuple[str, int, str]] = set()
    for (src, _), result in zip(pairs, gathered):
        if isinstance(result, Exception):
            log.warning("source %s failed: %s", src.name, result)
            continue
        for p in result:
            if p.protocol not in allowed_protocols:
                continue
            if p.port <= 0 or p.port > 65535:
                continue
            if not p.region and src.region:
                p.region = src.region
            k = p.key()
            if k in seen_keys:
                continue
            seen_keys.add(k)
            all_proxies.append(p)
    log.info("fetched total %d unique proxies (protocols %s)",
             len(all_proxies), allowed_protocols)
    return all_proxies
