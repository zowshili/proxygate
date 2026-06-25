"""66daili.com 抓取。每个代理是一个 <ul class="flex"> 含 8 个 <li>：
[IP, 端口, 地区, 匿名, 协议, 响应时长, 检测时间, 操作]
protocol=auto 时按页面协议列动态解析。URL 可带 protocol 参数分协议抓。
"""
from __future__ import annotations

import re
from typing import List

from ..models import Proxy
from .base import BaseFetcher


class Daili66Fetcher(BaseFetcher):
    async def parse(self, html: str, source) -> List[Proxy]:
        soup = self.soup(html)
        out: List[Proxy] = []
        # 每个代理是一个 ul.flex，内含 8 个 li
        for ul in soup.select("ul.flex"):
            lis = ul.find_all("li")
            if len(lis) < 6:
                continue
            texts = [li.get_text(strip=True) for li in lis]
            ip = texts[0]
            port = texts[1] if len(texts) > 1 else ""
            if not re.match(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$", ip):
                continue
            if not port.isdigit():
                continue
            country = texts[2] if len(texts) > 2 else ""
            # texts[3] 是匿名度，texts[4] 是协议
            proto_raw = texts[4] if len(texts) > 4 else ""
            proto = self._map_proto(proto_raw, source.protocol)
            if not proto:
                continue
            out.append(Proxy(ip=ip, port=int(port), protocol=proto, country=country, source=source.name))
        if not out:
            out = self.extract_ip_port(html, "http", source.name)
        return out

    @staticmethod
    def _map_proto(type_str: str, fallback: str) -> str | None:
        s = type_str.upper()
        if "SOCKS5" in s:
            return "socks5"
        if "SOCKS4" in s and "SOCKS5" not in s:
            return "socks4"
        if "HTTPS" in s:
            return "https"
        if "HTTP" in s and "HTTPS" not in s:
            return "http"
        return fallback if fallback != "auto" else "http"
