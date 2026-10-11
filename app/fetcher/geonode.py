"""geonode proxylist API 抓取。

GET https://proxylist.geonode.com/api/proxy-list?limit=50&page=1&country=CN&protocols=https%2Csocks5
返回 JSON：{"total": N, "data": [{"ip","port","country","protocols":["https","socks5"],...}]}
protocol=auto 时按条目 protocols[] 解析，只保留配置允许的协议（跑 fetch 全局过滤）。
"""
from __future__ import annotations

import json
import logging
from typing import List

from ..models import Proxy
from .base import BaseFetcher

log = logging.getLogger(__name__)

_KNOWN = {"https", "http", "socks5", "socks4"}


class GeonodeFetcher(BaseFetcher):
    async def parse(self, html: str, source) -> List[Proxy]:
        try:
            data = json.loads(html)
        except json.JSONDecodeError as e:
            log.warning("geonode JSON parse error: %s", e)
            return []

        entries = data.get("data") or []
        out: List[Proxy] = []
        for item in entries:
            ip = str(item.get("ip", "")).strip()
            port_raw = item.get("port", "")
            if not ip or ip.count(".") != 3:
                continue
            try:
                port = int(port_raw)
            except (TypeError, ValueError):
                continue
            if not (1 <= port <= 65535):
                continue

            protocols = item.get("protocols") or []
            protos = [str(p).lower() for p in protocols if str(p).lower() in _KNOWN]
            country = str(item.get("country", "")).strip()

            if source.protocol != "auto":
                # 配置指定协议：仅当条目支持该协议才收
                if source.protocol in protos or not protos:
                    out.append(Proxy(ip=ip, port=port, protocol=source.protocol,
                                     country=country, source=source.name))
                continue

            # auto：按 protocols[] 展开，多协议条目每种各生成一条（全局按 key 去重）
            if not protos:
                protos = ["https"]
            for proto in protos:
                out.append(Proxy(ip=ip, port=port, protocol=proto,
                                 country=country, source=source.name))
        return out
