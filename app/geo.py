"""地理归属判定：ip-api.com 批量接口 + SQLite 缓存。

为什么不在入库时判定：一次抓取 4000+ 候选，全量反查既慢又浪费额度。
存活代理每轮只有几十到几百个，只对它们做一次批量查询，命中缓存后长期复用。
"""
from __future__ import annotations

import asyncio
import logging
from itertools import islice

import aiohttp

from .config import GeoConfig
from .models import REGION_CN, REGION_HK, REGION_OTHER
from .store import Store

log = logging.getLogger(__name__)

# 国家码 → 区域。只有 CN/HK 会进订阅端点，其余统一 other。
_CC_REGION = {"CN": REGION_CN, "HK": REGION_HK}


def cc_to_region(cc: str) -> str:
    if not cc:
        return ""
    return _CC_REGION.get(cc.upper(), REGION_OTHER)


def _chunks(seq: list[str], size: int):
    it = iter(seq)
    while True:
        part = list(islice(it, size))
        if not part:
            return
        yield part


class GeoResolver:
    def __init__(self, cfg: GeoConfig, store: Store):
        self.cfg = cfg
        self.store = store

    async def run(self) -> dict:
        """对存活代理补齐区域标签。返回本轮统计。"""
        if not self.cfg.enabled:
            return {"enabled": False, "resolved": 0, "updated": 0}

        ttl = self.cfg.cache_days * 86400
        try:
            ips = await asyncio.to_thread(
                self.store.ips_needing_geo, ttl, self.cfg.max_per_cycle
            )
        except Exception as e:
            log.warning("geo: query pending ips failed: %s", e)
            return {"enabled": True, "resolved": 0, "updated": 0, "error": str(e)}

        if not ips:
            return {"enabled": True, "resolved": 0, "updated": 0}

        resolved = 0
        updated = 0
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            for batch in _chunks(ips, self.cfg.batch_size):
                payload = [{"query": ip} for ip in batch]
                try:
                    async with sess.post(
                        self.cfg.endpoint + "?fields=status,countryCode,query",
                        json=payload,
                        headers={"Content-Type": "application/json"},
                        ssl=False,
                    ) as resp:
                        if resp.status != 200:
                            log.warning("geo: endpoint status %s", resp.status)
                            break
                        data = await resp.json(content_type=None)
                except Exception as e:
                    log.warning("geo: batch request failed: %s", e)
                    break

                if not isinstance(data, list):
                    log.warning("geo: unexpected payload type %s", type(data).__name__)
                    break

                cache_rows: list[tuple[str, str]] = []
                mapping: dict[str, str] = {}
                for item in data:
                    if not isinstance(item, dict):
                        continue
                    ip = str(item.get("query") or "").strip()
                    if not ip:
                        continue
                    if item.get("status") != "success":
                        cache_rows.append((ip, ""))
                        continue
                    cc = str(item.get("countryCode") or "").upper()
                    cache_rows.append((ip, cc))
                    region = cc_to_region(cc)
                    if region:
                        mapping[ip] = region
                    resolved += 1

                if cache_rows:
                    await asyncio.to_thread(self.store.geo_put, cache_rows)
                if mapping:
                    updated += await asyncio.to_thread(self.store.apply_regions, mapping)

                if len(batch) < self.cfg.batch_size:
                    break
                await asyncio.sleep(self.cfg.request_interval)

        if resolved or updated:
            log.info("geo: resolved=%d updated=%d", resolved, updated)
        return {"enabled": True, "resolved": resolved, "updated": updated}
