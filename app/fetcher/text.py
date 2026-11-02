"""纯文本列表解析器（GitHub 聚合源）。

支持三种常见行格式：
  ip:port
  scheme://ip:port
  ip:port:Country Name        （hideip.me 风格）
协议由行内 scheme 决定，缺省用 source.protocol；国家名做显示用，区域仍由地理库判定。
"""
from __future__ import annotations

import re
from typing import List

from ..models import Proxy
from .base import BaseFetcher

_LINE_RE = re.compile(
    r"^(?:(?P<scheme>socks5|socks4|https|http)://)?"
    r"(?P<ip>\d{1,3}(?:\.\d{1,3}){3}):(?P<port>\d{2,5})"
    r"(?::(?P<country>[^:]{1,40}))?$"
)
_SCHEME_PROTO = {"socks5": "socks5", "socks4": "socks4", "https": "https", "http": "http"}


class TextFetcher(BaseFetcher):
    async def parse(self, html: str, source) -> List[Proxy]:
        out: List[Proxy] = []
        default_proto = source.protocol if source.protocol != "auto" else "https"
        for line in html.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            m = _LINE_RE.match(line)
            if not m:
                continue
            port = int(m.group("port"))
            if not (1 <= port <= 65535):
                continue
            proto = _SCHEME_PROTO.get(m.group("scheme") or "", default_proto)
            out.append(Proxy(
                ip=m.group("ip"), port=port, protocol=proto,
                country=(m.group("country") or "").strip(), source=source.name,
            ))
        return out
