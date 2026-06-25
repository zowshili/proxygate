"""快代理 kuaidaili.com 抓取。
URL: https://www.kuaidaili.com/free/inha/{page}/
表格列：IP、PORT、匿名度、类型(HTTP/HTTPS)、位置、响应速度、最后验证时间。
protocol=auto 时按页面类型列动态解析；否则用配置。
"""
from __future__ import annotations

from typing import List

from ..models import Proxy
from .base import BaseFetcher


class KuaidailiFetcher(BaseFetcher):
    async def parse(self, html: str, source) -> List[Proxy]:
        soup = self.soup(html)
        out: List[Proxy] = []
        for tr in soup.select("table tbody tr") or soup.select("table tr"):
            tds = tr.find_all("td")
            if len(tds) < 5:
                continue
            ip = tds[0].get_text(strip=True)
            port = tds[1].get_text(strip=True)
            if not (ip.count(".") == 3 and port.isdigit()):
                continue
            type_str = tds[3].get_text(strip=True).upper() if len(tds) > 3 else ""
            country = tds[4].get_text(strip=True) if len(tds) > 4 else ""
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
        # 类型列缺失时用 fallback（auto 时默认 http）
        return fallback if fallback != "auto" else "http"
