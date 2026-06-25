"""Fetcher 基类：每个源实现 parse()。"""
from __future__ import annotations

import asyncio
import logging
import re
from abc import ABC, abstractmethod
from typing import List

import aiohttp
from bs4 import BeautifulSoup

from ..models import Proxy
from ..config import SourceConfig

log = logging.getLogger(__name__)

_IP_RE = re.compile(r"(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})")
_PORT_RE = re.compile(r"(?<!\d)(\d{2,5})(?!\d)")


class BaseFetcher(ABC):
    """所有 spider 的父类。

    子类只需实现 `parse(html, source)` 返回 Proxy 列表。
    HTTP 抓取、分页、错误处理由本类负责。
    """

    def __init__(self, session: aiohttp.ClientSession, cfg: SourceConfig, fetch_cfg):
        self.session = session
        self.cfg = cfg
        self.fetch_cfg = fetch_cfg

    @abstractmethod
    async def parse(self, html: str, source: SourceConfig) -> List[Proxy]:
        ...

    async def fetch_text(self, url: str, referer: str | None = None) -> str:
        """抓取页面文本。带 cookie 重试：站点反爬常先返回 521/403 设 cookie，
        第二次带 cookie 访问即可通过。"""
        headers = self._browser_headers(referer)
        cookie_jar = aiohttp.CookieJar(unsafe=True)
        timeout = aiohttp.ClientTimeout(total=self.fetch_cfg.timeout)
        # 独立 session 以便每个源隔离 cookie；SSL 校验关闭（代理站点证书普遍不规范）
        async with aiohttp.ClientSession(cookie_jar=cookie_jar) as sess:
            for attempt in range(2):
                try:
                    async with sess.get(url, headers=headers, timeout=timeout,
                                        ssl=False, allow_redirects=True) as resp:
                        # 521/403/405 这种反爬先吃 cookie，不报错直接重试
                        await resp.read()
                        if resp.status in (403, 405, 521) and attempt == 0:
                            log.debug("%s got %d, retry with cookies", url, resp.status)
                            await asyncio.sleep(1.2)
                            continue
                        if resp.status >= 400:
                            log.debug("%s status=%d", url, resp.status)
                            return ""
                        # 编码处理：aiohttp 默认按 header；fallback utf-8
                        raw = await resp.read()
                        ctype = resp.headers.get("Content-Type", "")
                        enc = "utf-8"
                        for tok in ctype.replace(";", " ").split():
                            if tok.lower().startswith("charset="):
                                enc = tok.split("=", 1)[1]
                        try:
                            return raw.decode(enc, errors="ignore")
                        except (LookupError, UnicodeDecodeError):
                            return raw.decode("utf-8", errors="ignore")
                except Exception as e:
                    log.warning("fetch %s failed (attempt %d): %s", url, attempt + 1, e)
                    if attempt == 0:
                        await asyncio.sleep(1.0)
                        continue  # 第一次失败后重试
                    return ""
        return ""

    def _browser_headers(self, referer: str | None) -> dict:
        h = {
            "User-Agent": self.fetch_cfg.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
                      "image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Accept-Encoding": "gzip, deflate",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Linux"',
            "sec-fetch-dest": "document",
            "sec-fetch-mode": "navigate",
            "sec-fetch-site": "none",
            "sec-fetch-user": "?1",
            "upgrade-insecure-requests": "1",
        }
        if referer:
            h["Referer"] = referer
        return h

    async def run(self) -> List[Proxy]:
        """按 pages 抓取并解析。url 含 {page} 则分页；否则单页。"""
        results: List[Proxy] = []
        has_placeholder = "{page}" in self.cfg.url
        pages = self.cfg.pages if has_placeholder else 1
        # 提取源首页作为 Referer，帮助过 WAF
        from urllib.parse import urlparse
        origin = urlparse(self.cfg.url if not has_placeholder
                          else self.cfg.url.format(page=1)).scheme + "://" + \
                 urlparse(self.cfg.url if not has_placeholder
                          else self.cfg.url.format(page=1)).netloc + "/"
        for page in range(1, pages + 1):
            url = self.cfg.url.format(page=page) if has_placeholder else self.cfg.url
            html = await self.fetch_text(url, referer=origin)
            if html:
                try:
                    results.extend(await self.parse(html, self.cfg))
                except Exception as e:
                    log.warning("parse %s page=%s failed: %s", self.cfg.name, page, e)
            if has_placeholder:
                await asyncio.sleep(0.8)
        log.info("fetcher %s got %d proxies", self.cfg.name, len(results))
        return results

    # —— 工具方法 ——
    @staticmethod
    def soup(html: str) -> BeautifulSoup:
        return BeautifulSoup(html, "lxml")

    @staticmethod
    def extract_ip_port(text: str, fallback_protocol: str, source: str) -> List[Proxy]:
        """兜底解析：从任意文本抓 ip:port 对。"""
        pairs: list[tuple[str, int]] = []
        pattern = re.compile(r"(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})\D{1,8}?(\d{2,5})")
        for m in pattern.finditer(text):
            ip = m.group(1)
            port = int(m.group(2))
            if 1 <= port <= 65535:
                pairs.append((ip, port))
        # 去重保序
        seen = set()
        out = []
        for ip, port in pairs:
            if (ip, port) in seen:
                continue
            seen.add((ip, port))
            out.append(Proxy(ip=ip, port=port, protocol=fallback_protocol, source=source))
        return out
