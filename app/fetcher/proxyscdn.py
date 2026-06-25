"""proxy.scdn.io API 抓取。JSON 格式：{"code":200, "data":{"proxies":["ip:port",...]}}

每次最多 20 个，每 15 分钟采集足够。
"""
from __future__ import annotations

import json
import logging
from typing import List

from ..models import Proxy
from .base import BaseFetcher

log = logging.getLogger(__name__)


class ProxyscdnFetcher(BaseFetcher):
    async def parse(self, html: str, source) -> List[Proxy]:
        try:
            data = json.loads(html)
        except json.JSONDecodeError as e:
            log.warning("scdn JSON parse error: %s", e)
            return []

        if data.get("code") != 200:
            log.warning("scdn API error: %s", data.get("message", "unknown"))
            return []

        proxies_raw = data.get("data", {}).get("proxies", [])
        if not proxies_raw:
            return []

        proto = source.protocol if source.protocol != "auto" else "socks5"
        out: List[Proxy] = []
        for entry in proxies_raw:
            if ":" not in entry:
                continue
            ip, port_str = entry.rsplit(":", 1)
            if not port_str.isdigit():
                continue
            out.append(Proxy(ip=ip, port=int(port_str), protocol=proto, source=source.name))
        return out
