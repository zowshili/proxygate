"""纯文本 ip:port 列表解析器，适用于 GitHub 聚合源（TheSpeedX 等）。

每行一个 `ip:port`，协议由 source 配置指定。空行和注释行跳过。
"""
from __future__ import annotations

import re
from typing import List

from ..models import Proxy
from .base import BaseFetcher

_LINE_RE = re.compile(r"^(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}):(\d{2,5})\s*$")


class TextFetcher(BaseFetcher):
    async def parse(self, html: str, source) -> List[Proxy]:
        out: List[Proxy] = []
        proto = source.protocol if source.protocol != "auto" else "https"
        for line in html.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            m = _LINE_RE.match(line)
            if not m:
                continue
            ip = m.group(1)
            port = int(m.group(2))
            if 1 <= port <= 65535:
                out.append(Proxy(ip=ip, port=port, protocol=proto, source=source.name))
        return out
