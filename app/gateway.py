"""socks4→socks5 网关：对存活的 socks4 代理在本地开 SOCKS5 监听端口转发。

原理：
  Clash(不支持 socks4 出站) → 127.0.0.1:300xx [网关 SOCKS5 服务端]
                                 → socks4a 连接真实 socks4 代理 → 目标

实现：
  - SOCKS5 服务端握手：接收 VER/NMETHODS/METHODS → 回 0x05,0x00(无认证)
  - 解析 CONNECT 请求：VER/CMD/ATYP/DST.ADDR/DST.PORT
  - socks4a 连后端：发 [0x04,0x01, PORT(2), IP(4)=0.0.0.1, USERID\0, DOMAIN\0]，读响应 CD=90 成功
  - 双向 TCP 隧道转发

端口管理：
  - 范围由 config.gateway.port_start/port_end 控制
  - 内存映射 {(ip,port,socks4) -> local_port}
  - 探活后调用 update(alive_socks4_proxies) 动态增删

注意：网关监听 0.0.0.0 时无认证，局域网内任何人都可连。部署时需用防火墙限制。
"""
from __future__ import annotations

import asyncio
import logging
import socket
import struct
from typing import Dict, List, Tuple

from .config import GatewayConfig
from .models import Proxy

log = logging.getLogger(__name__)


class Socks4Bridge:
    """单个 socks4 代理的 SOCKS5 桥接服务。"""

    def __init__(self, backend_ip: str, backend_port: int, listen_host: str, listen_port: int):
        self.backend_ip = backend_ip
        self.backend_port = backend_port
        self.listen_host = listen_host
        self.listen_port = listen_port
        self._server: asyncio.AbstractServer | None = None
        self._active_conns = 0

    async def start(self) -> bool:
        try:
            self._server = await asyncio.start_server(
                self._handle_client, self.listen_host, self.listen_port
            )
            log.info("bridge up: %s:%d -> socks4 %s:%d",
                     self.listen_host, self.listen_port, self.backend_ip, self.backend_port)
            return True
        except OSError as e:
            log.warning("bridge bind %s:%d failed: %s", self.listen_host, self.listen_port, e)
            return False

    async def stop(self):
        if self._server:
            self._server.close()
            try:
                await self._server.wait_closed()
            except Exception:
                pass
            self._server = None

    async def _handle_client(self, c_reader: asyncio.StreamReader, c_writer: asyncio.StreamWriter):
        """处理 Clash 的 SOCKS5 连接，转 socks4a 给后端。"""
        self._active_conns += 1
        b_reader = b_writer = None
        try:
            # —— SOCKS5 握手 ——
            ver = await c_reader.readexactly(1)
            if ver != b"\x05":
                return
            nmethods = (await c_reader.readexactly(1))[0]
            await c_reader.readexactly(nmethods)  # 读方法列表
            c_writer.write(b"\x05\x00")  # 选无认证
            await c_writer.drain()

            # —— 解析 CONNECT 请求 ——
            hdr = await c_reader.readexactly(4)
            ver, cmd, _rsv, atyp = hdr
            if ver != 0x05 or cmd != 0x01:  # 只支持 CONNECT
                c_writer.write(b"\x05\x07\x00\x01\x00\x00\x00\x00\x00\x00")
                await c_writer.drain()
                return

            if atyp == 0x01:  # IPv4
                dst_addr = await c_reader.readexactly(4)
                host = socket.inet_ntoa(dst_addr)
            elif atyp == 0x03:  # 域名
                dlen = (await c_reader.readexactly(1))[0]
                host = (await c_reader.readexactly(dlen)).decode("idna", errors="ignore")
            elif atyp == 0x04:  # IPv6
                dst_addr = await c_reader.readexactly(16)
                host = socket.inet_ntop(socket.AF_INET6, dst_addr)
            else:
                c_writer.write(b"\x05\x08\x00\x01\x00\x00\x00\x00\x00\x00")
                await c_writer.drain()
                return
            dst_port = struct.unpack("!H", await c_reader.readexactly(2))[0]

            # —— socks4a 连接后端 ——
            b_reader, b_writer = await asyncio.wait_for(
                asyncio.open_connection(self.backend_ip, self.backend_port),
                timeout=8,
            )
            # socks4a 请求包: VER(0x04) CMD(0x01) PORT(2) IP(4)=0.0.0.1 USERID\0 DOMAIN\0
            # IP 用 0.0.0.1 表示走 socks4a 域名解析
            req = b"\x04\x01" + struct.pack("!H", dst_port) + b"\x00\x00\x00\x01" + b"\x00" + host.encode() + b"\x00"
            b_writer.write(req)
            await b_writer.drain()

            # 读 socks4 响应: VN(1) CD(1) DSTPORT(2) DSTIP(4)
            resp = await asyncio.wait_for(b_reader.readexactly(8), timeout=10)
            cd = resp[1]
            if cd != 0x5A:  # 0x5A=90=成功
                # 回 socks5 失败
                c_writer.write(b"\x05\x05\x00\x01\x00\x00\x00\x00\x00\x00")
                await c_writer.drain()
                return

            # 回 socks5 成功
            c_writer.write(b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00")
            await c_writer.drain()

            # —— 双向隧道转发 ——
            await asyncio.gather(
                self._pipe(c_reader, b_writer),
                self._pipe(b_reader, c_writer),
                return_exceptions=True,
            )
        except (asyncio.IncompleteReadError, asyncio.TimeoutError, ConnectionError, OSError):
            pass
        except Exception as e:
            log.debug("bridge conn error: %s", e)
        finally:
            self._active_conns -= 1
            for w in (c_writer, b_writer):
                if w:
                    try:
                        w.close()
                    except Exception:
                        pass

    @staticmethod
    async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            while True:
                data = await reader.read(65536)
                if not data:
                    break
                writer.write(data)
                await writer.drain()
        except Exception:
            pass
        finally:
            try:
                writer.close()
            except Exception:
                pass

    @property
    def active_conns(self) -> int:
        return self._active_conns


class Gateway:
    """socks4→socks5 网关管理器：动态管理多个 Socks4Bridge。"""

    def __init__(self, cfg: GatewayConfig):
        self.cfg = cfg
        # key: (ip, port) -> bridge
        self._bridges: Dict[Tuple[str, int], Socks4Bridge] = {}
        self._next_port = cfg.port_start
        # port -> key 反向映射，便于端口复用
        self._port_used: set[int] = set()

    def _alloc_port(self) -> int | None:
        for _ in range(self.cfg.port_end - self.cfg.port_start + 1):
            p = self._next_port
            self._next_port += 1
            if self._next_port > self.cfg.port_end:
                self._next_port = self.cfg.port_start
            if p not in self._port_used:
                self._port_used.add(p)
                return p
        return None

    async def update(self, alive_socks4: List[Proxy]) -> None:
        """探活后调用：传入当前所有存活的 socks4 代理，增删网关端口。

        - 新增的 socks4 代理 → 分配端口并启动 bridge（并行）
        - 已下线的 socks4 代理 → 停止 bridge 释放端口（并行）
        - 已存在的 socks4 代理 → 保持不动
        """
        alive_keys = {(p.ip, p.port) for p in alive_socks4 if p.protocol == "socks4"}

        # 停止已下线的（并行）
        to_remove = [k for k in self._bridges.keys() if k not in alive_keys]
        if to_remove:
            stop_ports = {k: self._bridges[k].listen_port for k in to_remove}
            await asyncio.gather(*[
                self._stop_bridge(k) for k in to_remove
            ], return_exceptions=True)
            for k in to_remove:
                self._port_used.discard(stop_ports[k])

        # 启动新增的（并行）
        to_add = [k for k in alive_keys if k not in self._bridges]
        if to_add:
            async def _start_one(ip: str, port: int):
                lp = self._alloc_port()
                if lp is None:
                    log.warning("gateway port range exhausted, skip %s:%d", ip, port)
                    return
                bridge = Socks4Bridge(ip, port, self.cfg.listen_host, lp)
                ok = await bridge.start()
                if ok:
                    self._bridges[(ip, port)] = bridge
                else:
                    self._port_used.discard(lp)

            await asyncio.gather(*[_start_one(ip, port) for ip, port in to_add], return_exceptions=True)

        log.info("gateway updated: %d bridges active", len(self._bridges))

    async def _stop_bridge(self, key):
        bridge = self._bridges.pop(key, None)
        if bridge:
            self._port_used.discard(bridge.listen_port)
            await bridge.stop()
            log.info("bridge down: socks4 %s:%d (port %d)", key[0], key[1], bridge.listen_port)

        log.info("gateway updated: %d bridges active", len(self._bridges))

    async def stop_all(self):
        if self._bridges:
            await asyncio.gather(*[b.stop() for b in self._bridges.values()], return_exceptions=True)
        self._bridges.clear()
        self._port_used.clear()

    def get_bridge_port(self, ip: str, port: int) -> int | None:
        b = self._bridges.get((ip, port))
        return b.listen_port if b else None

    def stats(self) -> dict:
        return {
            "active_bridges": len(self._bridges),
            "active_connections": sum(b.active_conns for b in self._bridges.values()),
            "port_range": f"{self.cfg.port_start}-{self.cfg.port_end}",
            "listen_host": self.cfg.listen_host,
        }

    def list_bridges(self) -> list[dict]:
        return [
            {
                "backend": f"{ip}:{port}",
                "listen_port": b.listen_port,
                "active_conns": b.active_conns,
            }
            for (ip, port), b in self._bridges.items()
        ]
