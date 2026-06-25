"""异步探活：对 HTTP / SOCKS4 / SOCKS5 代理双标签验证（百度 + Google）。

关键设计：
- socks4/socks5：session 加 ssl=False，避免访问 https 目标时 TLS 校验失败（这是
  早期 socks4 全死的根因——实际上 socks4 代理能通，只是 https 目标证书校验抛异常）。
- HTTP 代理：必须能通过 https 目标(CONNECT 隧道)才算 alive，过滤掉只能明文转发的假代理。
- 探活目标统一用 http（存活率更高），额外对 HTTP 代理测一个 https 目标做 CONNECT 检验。
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import List

import aiohttp
from aiohttp_socks import ProxyConnector

from .config import CheckConfig
from .models import Proxy
from .store import Store

log = logging.getLogger(__name__)


def _connector_for(proxy: Proxy) -> ProxyConnector | None:
    """构造 socks4/socks5 的 aiohttp connector。"""
    if proxy.protocol == "socks5":
        scheme = "socks5"
    elif proxy.protocol == "socks4":
        scheme = "socks4"
    else:
        # HTTP 代理不走 connector，由 _probe_http 处理
        return None
    # aiohttp_socks 0.11+ 不接受 socks:// scheme，必须用 socks5:// / socks4://
    return ProxyConnector.from_url(f"{scheme}://{proxy.ip}:{proxy.port}")


async def _probe_socks(
    proxy: Proxy, url: str, timeout: float, expected: list[int]
) -> tuple[bool, int]:
    """socks4/socks5 探活。独立 session + ssl=False（关键修复）。"""
    start = time.time()
    connector = _connector_for(proxy)
    if connector is None:
        return False, 0
    timeout_obj = aiohttp.ClientTimeout(total=timeout)
    try:
        # ssl=False 让访问 https 目标时跳过证书校验
        async with aiohttp.ClientSession(connector=connector, timeout=timeout_obj) as s:
            async with s.get(url, allow_redirects=True, ssl=False) as resp:
                ok = resp.status in expected
                return ok, int((time.time() - start) * 1000) if ok else 0
    except Exception:
        return False, 0


async def _probe_http(
    proxy: Proxy, url: str, timeout: float, expected: list[int],
    base_session: aiohttp.ClientSession,
) -> tuple[bool, int]:
    """HTTP 代理探活：通过 CONNECT 隧道（aiohttp proxy 参数）。"""
    start = time.time()
    proxy_url = f"http://{proxy.ip}:{proxy.port}"
    try:
        async with base_session.get(
            url, proxy=proxy_url,
            timeout=aiohttp.ClientTimeout(total=timeout),
            allow_redirects=True, ssl=False,
        ) as resp:
            ok = resp.status in expected
            return ok, int((time.time() - start) * 1000) if ok else 0
    except Exception:
        return False, 0


async def check_one(
    proxy: Proxy,
    cfg: CheckConfig,
    base_session: aiohttp.ClientSession,
) -> tuple[Proxy, bool, int, bool, bool]:
    """探活单个代理，返回 (proxy, alive, latency_ms, cn, foreign)。

    - socks4/socks5: 测 cn(http) + foreign(http)，任一通即 alive
    - http 代理: 先测 cn(http) 打标签，再额外测 https 目标验证 CONNECT 能力；
      只有 CONNECT 通了才 alive（过滤假 HTTP 代理）。
    """
    timeout = cfg.timeout

    if proxy.protocol in ("socks4", "socks5"):
        cn_ok, cn_lat = await _probe_socks(proxy, cfg.url_cn, timeout, [cfg.expected_cn_status])
        fr_ok, fr_lat = await _probe_socks(proxy, cfg.url_foreign, timeout, cfg.expected_foreign_status)
        alive = cn_ok or fr_ok
        lats = [l for l in (cn_lat, fr_lat) if l > 0]
        return proxy, alive, (min(lats) if lats else 0), cn_ok, fr_ok

    # HTTP 代理
    cn_ok, cn_lat = await _probe_http(proxy, cfg.url_cn, timeout, [cfg.expected_cn_status], base_session)
    fr_ok, fr_lat = await _probe_http(proxy, cfg.url_foreign, timeout, cfg.expected_foreign_status, base_session)
    # 额外测 https 目标：CONNECT 隧道必须通才算 alive
    https_ok, https_lat = await _probe_http(
        proxy, cfg.url_https_check, timeout, cfg.expected_https_status, base_session
    )
    alive = https_ok and (cn_ok or fr_ok)
    # 延迟取 https 测试（这反映真实使用场景的延迟）
    lat = https_lat if https_ok else 0
    return proxy, alive, lat, cn_ok, fr_ok


async def run_check_batch(
    proxies: List[Proxy],
    cfg: CheckConfig,
    store: Store,
) -> None:
    """对一批代理探活并落库。设总超时兜底，避免单个挂起探活卡死调度器。"""
    if not proxies:
        return

    # 总超时 = 单代理超时 × (代理数 / 并发数) × 1.5 倍保险
    estimated = (len(proxies) / max(cfg.concurrency, 1)) * cfg.timeout * 1.5
    total_timeout = max(estimated, 60)  # 最少 60 秒

    sem = asyncio.Semaphore(cfg.concurrency)
    headers = {"User-Agent": "Mozilla/5.0 proxypool-checker"}
    conn = aiohttp.TCPConnector(limit=cfg.concurrency, ssl=False, force_close=True)
    progress = {"done": 0, "alive": 0}

    async with aiohttp.ClientSession(connector=conn, headers=headers) as base_session:

        async def _wrapped(p: Proxy):
            async with sem:
                try:
                    _, alive, lat, cn, fr = await asyncio.wait_for(
                        check_one(p, cfg, base_session), timeout=cfg.timeout * 2
                    )
                except asyncio.TimeoutError:
                    log.debug("check_one timeout %s:%d", p.ip, p.port)
                    _, alive, lat, cn, fr = p, False, 0, False, False
                except Exception:
                    _, alive, lat, cn, fr = p, False, 0, False, False
            store.mark_check_result(
                p.ip, p.port, p.protocol,
                alive, lat, cn, fr,
                cfg.fail_threshold, cfg.alive_fail_to_dead,
            )
            progress["done"] += 1
            if alive:
                progress["alive"] += 1
            if progress["done"] % 50 == 0:
                log.info("check progress %d/%d alive=%d",
                         progress["done"], len(proxies), progress["alive"])

        try:
            await asyncio.wait_for(
                asyncio.gather(*[_wrapped(p) for p in proxies], return_exceptions=True),
                timeout=total_timeout,
            )
        except asyncio.TimeoutError:
            log.warning("check batch total timeout (%ds) reached, %d/%d done",
                        total_timeout, progress["done"], len(proxies))

    log.info("check batch done: %d/%d alive", progress["alive"], len(proxies))
