"""seofangfa（SEO编程坊 proxy.seofangfa.com）抓取。

页面结构：table 行，第一格是
  <td class="proxy-ip"><button class="proxy-ip-copy mono" data-proxy="ip:port">ip</button></td>
后续格：端口、协议标签(<span class="protocol-tag">HTTP/HTTPS/SOCKS4/SOCKS5</span>)、
匿名度、省份、响应速度、更新时间、套餐。
全站为中国大陆代理。protocol=auto 时按协议标签解析，只留 https/socks5
（http/socks4 行直接丢弃）。
"""
from __future__ import annotations

import re
from typing import List

from ..models import Proxy
from .base import BaseFetcher


class SeofangfaFetcher(BaseFetcher):
    async def parse(self, html: str, source) -> List[Proxy]:
        out: List[Proxy] = []
        # 每行一个 data-proxy="ip:port" 的按钮，协议标签在同行的 protocol-tag
        soup = self.soup(html)
        for tr in soup.select("tr"):
            btn = tr.select_one("button[data-proxy]")
            tag = tr.select_one(".protocol-tag")
            if not btn or not tag:
                continue
            pair = (btn.get("data-proxy") or "").strip()
            proto_raw = tag.get_text(strip=True).upper()
            m = re.match(r"^(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}):(\d{2,5})$", pair)
            if not m:
                continue
            proto = self._map_proto(proto_raw, source.protocol)
            if not proto:
                continue
            ip, port = m.group(1), int(m.group(2))
            # 省份列：td[title] 含省/市
            country = ""
            for td in tr.find_all("td"):
                t = td.get("title") or ""
                if ("省" in t or "市" in t or "自治" in t) and len(t) <= 20:
                    country = t
                    break
            out.append(Proxy(ip=ip, port=port, protocol=proto,
                             country=country, source=source.name))
        if not out:
            # 兜底：正则直接抓 data-proxy 属性（soup 解析失败时）
            out = []
            for pair in re.findall(r'data-proxy="(\d{1,3}(?:\.\d{1,3}){3}:\d{2,5})"', html):
                ip, port_str = pair.rsplit(":", 1)
                out.append(Proxy(ip=ip, port=int(port_str),
                                 protocol=source.protocol if source.protocol != "auto" else "https",
                                 source=source.name))
        return out

    @staticmethod
    def _map_proto(type_str: str, fallback: str) -> str | None:
        s = type_str.upper()
        if "SOCKS5" in s:
            return "socks5"
        if "SOCKS4" in s:
            return "socks4"
        if "HTTPS" in s:
            return "https"
        if "HTTP" in s:
            return "http"
        return fallback if fallback != "auto" else "https"
