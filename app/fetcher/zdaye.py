"""站大爷 zdaye.com 免费代理解析。"""
from __future__ import annotations

from typing import List

from ..models import Proxy
from .base import BaseFetcher


class ZdayeFetcher(BaseFetcher):
    async def parse(self, html: str, source) -> List[Proxy]:
        # zdaye 的免费列表是 table 结构：每行 tr，列含 ip、port、协议、匿名度、位置、响应速度、最后验证时间
        soup = self.soup(html)
        out: List[Proxy] = []
        # 表头通常在 thead，代理行在 tbody
        rows = soup.select("table tbody tr") or soup.select("table tr")
        for tr in rows:
            tds = tr.find_all("td")
            if len(tds) < 4:
                continue
            # 第一列通常含 ip（可能带 :port 或单独两列）
            first = tds[0].get_text(strip=True)
            second = tds[1].get_text(strip=True) if len(tds) > 1 else ""
            ip, port = self._pick_ip_port(first, second)
            if not ip:
                continue
            # 协议从配置里取（页面有时混排），fallback 用配置
            proto = source.protocol if source.protocol != "auto" else "https"
            # 找位置列（通常是某列含"省"或国名）
            country = ""
            for td in tds[2:]:
                txt = td.get_text(strip=True)
                if len(txt) <= 12 and ("省" in txt or "市" in txt or len(txt) < 6):
                    country = txt
                    break
            out.append(Proxy(ip=ip, port=port, protocol=proto, country=country, source=source.name))
        if not out:
            # 兜底：正则
            out = self.extract_ip_port(html, source.protocol if source.protocol != "auto" else "https", source.name)
        return out

    @staticmethod
    def _pick_ip_port(a: str, b: str):
        import re
        ipre = re.compile(r"(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})")
        porte = re.compile(r"^\d{2,5}$")
        m1 = ipre.search(a)
        if m1:
            ip = m1.group(1)
            # 端口可能在 a 的后半部分或 b
            rest = a.split(ip, 1)[1]
            pm = re.search(r"(\d{2,5})", rest)
            if pm:
                return ip, int(pm.group(1))
            if porte.match(b):
                return ip, int(b)
        if ipre.search(b):
            # 第一个是 port，第二个是 ip 的少见格式
            ip = ipre.search(b).group(1)
            if porte.match(a):
                return ip, int(a)
        return None, None
