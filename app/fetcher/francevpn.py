"""francevpn.github.io/free-proxy-for-china 抓取。
表格列：IP地址、端口号、用户名、密码、国家、协议、匿名、速度。
protocol=auto 时按协议列动态解析（socks4/socks5），否则用配置。
用户名/密码列显示 **** 但实测免费代理列表通常无需认证，按无认证处理。
"""
from __future__ import annotations

from typing import List

from ..models import Proxy
from .base import BaseFetcher


class FrancevpnFetcher(BaseFetcher):
    async def parse(self, html: str, source) -> List[Proxy]:
        soup = self.soup(html)
        out: List[Proxy] = []
        # 第二个 table 是代理列表（第一个是对比表）
        tables = soup.select("table")
        for table in tables:
            rows = table.select("tr")
            if len(rows) < 2:
                continue
            # 表头含 "IP地址" 才是代理表
            header_cells = [c.get_text(strip=True) for c in rows[0].find_all(["td", "th"])]
            if not any("IP" in h.upper() for h in header_cells):
                continue
            for tr in rows[1:]:
                tds = tr.find_all("td")
                if len(tds) < 6:
                    continue
                ip = tds[0].get_text(strip=True)
                port = tds[1].get_text(strip=True)
                if not (ip.count(".") == 3 and port.isdigit()):
                    continue
                country = tds[4].get_text(strip=True)
                proto_raw = tds[5].get_text(strip=True).upper()
                proto = self._map_proto(proto_raw, source.protocol)
                if not proto:
                    continue
                out.append(Proxy(ip=ip, port=int(port), protocol=proto, country=country, source=source.name))
            if out:
                break  # 只取第一个代理表
        if not out:
            out = self.extract_ip_port(html, "socks5", source.name)
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
        return fallback if fallback != "auto" else "socks5"
