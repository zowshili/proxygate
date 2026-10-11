"""站大爷 zdaye.com 免费代理解析。

页面结构（2026-10 实测，ul-row 布局）：
  <ul class="ul-row">
    <li class="ul-cell"><p class="proxy_ip">IP</p><p class="proxy_port">Port：10808</p></li>
    <li class="ul-cell"><p class="proxy_protocol"><span class="protocol_span protocol_span_https">HTTPS</span></p>...</li>
    <li class="ul-cell" title="安徽省合肥市 ...">位置</li>
    ...
免费列表只开放第一页（翻页 405），协议标签含 HTTP/HTTPS/SOCKS4/SOCKS5。
"""
from __future__ import annotations

import re
from typing import List

from ..models import Proxy
from .base import BaseFetcher


class ZdayeFetcher(BaseFetcher):
    async def parse(self, html: str, source) -> List[Proxy]:
        soup = self.soup(html)
        out: List[Proxy] = []

        # 新结构：ul.ul-row 每行一个代理
        for ul in soup.select("ul.ul-row"):
            cells = ul.select("li.ul-cell")
            if not cells:
                continue
            ip_p = ul.select_one(".proxy_ip")
            port_p = ul.select_one(".proxy_port")
            if not ip_p or not port_p:
                continue
            ip = ip_p.get_text(strip=True)
            pm = re.search(r"(\d{2,5})", port_p.get_text(strip=True))
            if not (ip.count(".") == 3 and pm):
                continue
            port = int(pm.group(1))
            proto_raw = ""
            span = ul.select_one(".protocol_span")
            if span:
                proto_raw = span.get_text(strip=True)
            proto = self._map_proto(proto_raw, source.protocol)
            if not proto:
                continue
            country = ""
            for li in cells[2:]:
                t = li.get("title") or li.get_text(strip=True)
                if t and ("省" in t or "市" in t or len(t) <= 12):
                    country = t
                    break
            out.append(Proxy(ip=ip, port=port, protocol=proto, country=country, source=source.name))

        # 旧结构兜底：table 行
        if not out:
            rows = soup.select("table tbody tr") or soup.select("table tr")
            for tr in rows:
                tds = tr.find_all("td")
                if len(tds) < 4:
                    continue
                first = tds[0].get_text(strip=True)
                second = tds[1].get_text(strip=True) if len(tds) > 1 else ""
                ip, port = self._pick_ip_port(first, second)
                if not ip:
                    continue
                proto = source.protocol if source.protocol != "auto" else "https"
                country = ""
                for td in tds[2:]:
                    txt = td.get_text(strip=True)
                    if len(txt) <= 12 and ("省" in txt or "市" in txt or len(txt) < 6):
                        country = txt
                        break
                out.append(Proxy(ip=ip, port=port, protocol=proto, country=country, source=source.name))

        if not out:
            out = self.extract_ip_port(html, source.protocol if source.protocol != "auto" else "https", source.name)
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
        return fallback if fallback != "auto" else "https"

    @staticmethod
    def _pick_ip_port(a: str, b: str):
        ipre = re.compile(r"(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})")
        porte = re.compile(r"^\d{2,5}$")
        m1 = ipre.search(a)
        if m1:
            ip = m1.group(1)
            rest = a.split(ip, 1)[1]
            pm = re.search(r"(\d{2,5})", rest)
            if pm:
                return ip, int(pm.group(1))
            if porte.match(b):
                return ip, int(b)
        if ipre.search(b):
            ip = ipre.search(b).group(1)
            if porte.match(a):
                return ip, int(a)
        return None, None
