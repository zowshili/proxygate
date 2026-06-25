"""qiyunip.com 抓取。表格列：IP、PORT、匿名度、类型(http/https)、属地、响应速度、录取时间。
protocol=auto 时按页面类型列动态解析。
"""
from __future__ import annotations

from typing import List

from ..models import Proxy
from .base import BaseFetcher


class QiyunipFetcher(BaseFetcher):
    async def parse(self, html: str, source) -> List[Proxy]:
        soup = self.soup(html)
        out: List[Proxy] = []
        # qiyunip 的数据行用 <th> 而非 <td>（CSS 表格布局），需同时查 td/th
        for tr in soup.select("table tbody tr") or soup.select("table tr"):
            cells = tr.find_all(["td", "th"])
            if len(cells) < 4:
                continue
            ip = cells[0].get_text(strip=True)
            port = cells[1].get_text(strip=True)
            if not (ip.count(".") == 3 and port.isdigit()):
                continue
            type_str = cells[3].get_text(strip=True).upper() if len(cells) > 3 else ""
            country = cells[4].get_text(strip=True) if len(cells) > 4 else ""
            proto = self._map_proto(type_str, source.protocol)
            if not proto:
                continue
            out.append(Proxy(ip=ip, port=int(port), protocol=proto, country=country, source=source.name))
        if not out:
            out = self.extract_ip_port(html, "http", source.name)
        return out

    @staticmethod
    def _map_proto(type_str: str, fallback: str) -> str | None:
        s = type_str.upper()
        if "HTTPS" in s:
            return "https"
        if "HTTP" in s and "HTTPS" not in s:
            return "http"
        if "SOCKS5" in s:
            return "socks5"
        if "SOCKS4" in s and "SOCKS5" not in s:
            return "socks4"
        return fallback if fallback != "auto" else "http"
