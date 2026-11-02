"""异步探活（v2.0.0）。

关键变化：
- socks5 改为原生 asyncio 流式 SOCKS5 握手 + 明文 HTTP GET。
  不再为每个代理创建 aiohttp_socks ProxyConnector/ClientSession（500 并发下
  每轮要建上万次会话），单代理只占一条 TCP 连接，内存与 fd 占用大幅下降。
- https 代理仍走 aiohttp CONNECT 隧道（必须通才算活），共享一个连接池。
- 存活延迟超过上限的节点用 latency=-1 打标，立即置死不走失败宽限。
"""
from __future__ import annotations

import asyncio
import logging
import struct
import time
from typing import List

import aiohttp
from aiohttp.resolver import AsyncResolver

from .config import CheckConfig
from .models import Proxy
from .store import Store

log = logging.getLogger(__name__)

_UA = b"Mozilla/5.0 proxypool-checker"


def _split_url(url: str) -> tuple[str, str, int]:
    """http://host/path → (host, path, port)。探活目标都是 http。"""
    rest = url.split("://", 1)[-1]
    if "/" in rest:
        host, path = rest.split("/", 1)
        path = "/" + path
    else:
        host, path = rest, "/"
    if ":" in host:
        host, p = host.rsplit(":", 1)
        port = int(p)
    else:
        port = 80
    return host, path, port


async def _socks5_probe(
    proxy: Proxy, url: str, timeout: float, expected: list[int]
) -> tuple[bool, int]:
    """原生 SOCKS5 CONNECT + HTTP 状态行读取，整体受 timeout 约束。"""
    try:
        return await asyncio.wait_for(_socks5_probe_impl(proxy, url, timeout, expected), timeout)
    except Exception:
        return False, 0


async def _socks5_probe_impl(
    proxy: Proxy, url: str, timeout: float, expected: list[int]
) -> tuple[bool, int]:
    host, path, port = _split_url(url)
    hb = host.encode()
    if len(hb) > 255:
        return False, 0
    start = time.monotonic()
    writer = None
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(proxy.ip, proxy.port), timeout
        )
        # 握手：VER=5 NMETHODS=1 METHODS=[0 无认证]
        writer.write(b"\x05\x01\x00")
        await writer.drain()
        ver, method = await asyncio.wait_for(reader.readexactly(2), timeout)
        if ver != 0x05 or method != 0x00:
            return False, 0

        # CONNECT：域名寻址（ATYP=3），让代理侧解析，顺带验证其 DNS 能力
        writer.write(b"\x05\x01\x00\x03" + bytes([len(hb)]) + hb + struct.pack("!H", port))
        await writer.drain()
        rep = await asyncio.wait_for(reader.readexactly(4), timeout)
        if rep[0] != 0x05 or rep[1] != 0x00:
            return False, 0
        atyp = rep[3]
        if atyp == 0x01:
            await asyncio.wait_for(reader.readexactly(4), timeout)
        elif atyp == 0x03:
            n = (await asyncio.wait_for(reader.readexactly(1), timeout))[0]
            await asyncio.wait_for(reader.readexactly(n), timeout)
        elif atyp == 0x04:
            await asyncio.wait_for(reader.readexactly(16), timeout)
        else:
            return False, 0
        await asyncio.wait_for(reader.readexactly(2), timeout)

        req = (
            f"GET {path} HTTP/1.1\r\nHost: {host}\r\n"
            f"User-Agent: {_UA.decode()}\r\nAccept: */*\r\nConnection: close\r\n\r\n"
        )
        writer.write(req.encode())
        await writer.drain()

        line = await asyncio.wait_for(reader.readline(), timeout)
        parts = line.split()
        if len(parts) < 2 or not parts[0].startswith(b"HTTP/"):
            return False, 0
        code = int(parts[1])
        if code not in expected:
            return False, 0
        return True, int((time.monotonic() - start) * 1000)
    except (asyncio.TimeoutError, asyncio.IncompleteReadError, OSError, ValueError):
        return False, 0
    except Exception:
        return False, 0
    finally:
        if writer is not None:
            try:
                writer.close()
            except Exception:
                pass


async def _probe_http(
    proxy: Proxy, url: str, timeout: float, expected: list[int],
    base_session: aiohttp.ClientSession,
) -> tuple[bool, int]:
    """https/http 代理探活：通过 CONNECT 隧道（aiohttp proxy 参数）。"""
    start = time.monotonic()
    proxy_url = f"http://{proxy.ip}:{proxy.port}"
    try:
        async with base_session.get(
            url, proxy=proxy_url,
            timeout=aiohttp.ClientTimeout(total=timeout),
            allow_redirects=False, ssl=False,
        ) as resp:
            ok = resp.status in expected
            return ok, int((time.monotonic() - start) * 1000) if ok else 0
    except Exception:
        return False, 0


async def check_one(
    proxy: Proxy,
    cfg: CheckConfig,
    base_session: aiohttp.ClientSession,
) -> tuple[Proxy, bool, int, bool, bool]:
    """探活单个代理，返回 (proxy, alive, latency_ms, cn, foreign)。

    - socks5: 测 cn(baidu) + foreign(gstatic)，任一通即 alive
    - https : 必须过 https 目标(CONNECT 隧道)才 alive，再补打 cn 标签
    """
    timeout = cfg.timeout
    max_lat = cfg.max_alive_latency_ms

    if proxy.protocol == "socks5":
        cn_res, fr_res = await asyncio.gather(
            _socks5_probe(proxy, cfg.url_cn, timeout, cfg.expected_cn_status),
            _socks5_probe(proxy, cfg.url_foreign, timeout, cfg.expected_foreign_status),
        )
        cn_ok, cn_lat = cn_res
        fr_ok, fr_lat = fr_res
        alive = cn_ok or fr_ok
        lats = [l for l in (cn_lat, fr_lat) if l > 0]
        lat = min(lats) if lats else 0
        if alive and max_lat and lat > max_lat:
            return proxy, False, -1, cn_ok, fr_ok
        return proxy, alive, lat, cn_ok, fr_ok

    # https 代理：先验 CONNECT 隧道（打不了洞的进 Clash 也是废的）
    https_ok, https_lat = await _probe_http(
        proxy, cfg.url_https_check, timeout, cfg.expected_https_status, base_session
    )
    if https_ok:
        if max_lat and https_lat > max_lat:
            return proxy, False, -1, False, False
        cn_ok, _ = await _probe_http(
            proxy, cfg.url_cn, timeout, [200, 301, 302], base_session
        )
        return proxy, True, https_lat, cn_ok, False

    # 回退：大陆出口的 HTTP 代理到不了 gstatic（自身被墙），但对 baidu 的请求能过。
    # 不放这条回退，ip3366 / 89ip / qiyunip 这类大陆源会整批判死。
    cn_ok, cn_lat = await _probe_http(
        proxy, cfg.url_cn, timeout, cfg.expected_cn_status, base_session
    )
    if not cn_ok:
        return proxy, False, 0, False, False
    if max_lat and cn_lat > max_lat:
        return proxy, False, -1, False, False
    return proxy, True, cn_lat, True, False


async def run_check_batch(
    proxies: List[Proxy],
    cfg: CheckConfig,
    store: Store,
) -> dict:
    """对一批代理探活并落库。设总超时兜底，避免挂起探活卡死调度器。"""
    if not proxies:
        return {"checked": 0, "alive": 0}

    # socks5 两个探测已改为并行，单代理最坏耗时 ≈ timeout；https 是 2 次串行。
    per_proxy_worst = cfg.timeout * 2
    estimated = (len(proxies) / max(cfg.concurrency, 1)) * per_proxy_worst * 1.5
    total_timeout = max(estimated, 180)

    sem = asyncio.Semaphore(cfg.concurrency)
    headers = {"User-Agent": "Mozilla/5.0 proxypool-checker"}
    resolver = AsyncResolver(timeout=2.0)
    conn = aiohttp.TCPConnector(
        limit=cfg.concurrency, ssl=False, force_close=True,
        resolver=resolver, enable_cleanup_closed=True,
    )
    progress = {"done": 0, "alive": 0}
    buf: list[tuple] = []
    buf_lock = asyncio.Lock()
    FLUSH_SIZE = 300

    async def _flush():
        async with buf_lock:
            if not buf:
                return
            items = buf[:]
            buf.clear()
        # SQLite 写在独立线程执行，避免 500 并发下的 commit 阻塞事件循环
        await asyncio.to_thread(
            store.mark_check_results_bulk,
            items, cfg.fail_threshold, cfg.alive_fail_to_dead,
        )

    async def _flush_loop():
        while progress["done"] < len(proxies):
            await asyncio.sleep(2)
            await _flush()

    async with aiohttp.ClientSession(connector=conn, headers=headers) as base_session:

        async def _wrapped(p: Proxy):
            async with sem:
                try:
                    _, alive, lat, cn, fr = await asyncio.wait_for(
                        check_one(p, cfg, base_session), timeout=cfg.timeout * 2 + 1
                    )
                except asyncio.TimeoutError:
                    log.debug("check_one timeout %s:%d", p.ip, p.port)
                    _, alive, lat, cn, fr = p, False, 0, False, False
                except Exception:
                    _, alive, lat, cn, fr = p, False, 0, False, False
            buf.append((p.ip, p.port, p.protocol, alive, lat, cn, fr))
            progress["done"] += 1
            if alive:
                progress["alive"] += 1
            if len(buf) >= FLUSH_SIZE:
                await _flush()
            if progress["done"] % 300 == 0:
                log.info("check progress %d/%d alive=%d",
                         progress["done"], len(proxies), progress["alive"])

        flush_task = asyncio.create_task(_flush_loop())
        try:
            await asyncio.wait_for(
                asyncio.gather(*[_wrapped(p) for p in proxies], return_exceptions=True),
                timeout=total_timeout,
            )
        except asyncio.TimeoutError:
            log.warning("check batch total timeout (%ds) reached, %d/%d done",
                        total_timeout, progress["done"], len(proxies))
        finally:
            flush_task.cancel()
            await _flush()

    log.info("check batch done: %d/%d alive", progress["alive"], len(proxies))
    return {"checked": progress["done"], "alive": progress["alive"]}
