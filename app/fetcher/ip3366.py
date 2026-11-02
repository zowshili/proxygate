"""ip3366.net 抓取。表格结构：IP、端口、匿名度、类型、位置、响应速度、最后验证。
ip3366 的类型列实际全是 HTTP，protocol 由 config 指定。
"""
from __future__ import annotations

from typing import List

from ..models import Proxy
from .base import BaseFetcher


class Ip3366Fetcher(BaseFetcher):
    async def parse(self, html: str, source) -> List[Proxy]:
        soup = self.soup(html)
        out: List[Proxy] = []
        for tr in soup.select("table tbody tr") or soup.select("table tr"):
            tds = tr.find_all("td")
            if len(tds) < 3:
                continue
            ip = tds[0].get_text(strip=True)
            port = tds[1].get_text(strip=True)
            if not (ip.count(".") == 3 and port.isdigit()):
                continue
            # 页面是 gb2312 且没在 header 里声明字符集，解析出的位置列是乱码，
            # 国家留空，区域交给 geo 判定
            country = ""
            out.append(Proxy(
                ip=ip, port=int(port),
                protocol=source.protocol if source.protocol != "auto" else "http",
                country=country, source=source.name,
            ))
        if not out:
            out = self.extract_ip_port(html, source.protocol if source.protocol != "auto" else "http", source.name)
        return out
