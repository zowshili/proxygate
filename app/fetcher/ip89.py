"""89ip.cn 抓取。89ip 页面无类型列，实际都是 HTTP 代理（protocol 由 config 指定）。"""
from __future__ import annotations

from typing import List

from ..models import Proxy
from .base import BaseFetcher


class Ip89Fetcher(BaseFetcher):
    async def parse(self, html: str, source) -> List[Proxy]:
        soup = self.soup(html)
        out: List[Proxy] = []
        # 89ip 表格行：ip、port、位置、运营商、最后验证时间（无类型列）
        for tr in soup.select("table tbody tr") or soup.select("table tr"):
            tds = tr.find_all("td")
            if len(tds) < 3:
                continue
            ip = tds[0].get_text(strip=True)
            port = tds[1].get_text(strip=True)
            if not (ip.count(".") == 3 and port.isdigit()):
                continue
            # 第 3 列是位置，第 4 列是运营商
            country = tds[3].get_text(strip=True) if len(tds) >= 4 else ""
            if len(tds) >= 5:
                country = f"{country} {tds[4].get_text(strip=True)}".strip()
            out.append(Proxy(
                ip=ip, port=int(port),
                protocol=source.protocol if source.protocol != "auto" else "http",
                country=country, source=source.name,
            ))
        if not out:
            out = self.extract_ip_port(html, source.protocol if source.protocol != "auto" else "http", source.name)
        return out
