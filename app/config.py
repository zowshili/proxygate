"""配置加载：把 config.yaml 读成全局可访问的对象。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, List

import yaml


@dataclass
class SourceConfig:
    name: str
    type: str            # spider 类型，对应 fetcher/<type>.py
    protocol: str        # https / socks4 / socks5 / auto
    url: str
    pages: int = 1


@dataclass
class WebConfig:
    host: str
    port: int
    token: str


@dataclass
class ClashConfig:
    gateway_host: str          # socks4 网关注册到 Clash 订阅时用的地址


@dataclass
class GatewayConfig:
    listen_host: str
    port_start: int
    port_end: int


@dataclass
class FetchConfig:
    interval_seconds: int
    jitter_seconds: int
    timeout: int
    max_pages: int
    user_agent: str
    sources: List[SourceConfig] = field(default_factory=list)


@dataclass
class CheckConfig:
    new_interval: int
    full_interval: int
    concurrency: int
    timeout: int
    fail_threshold: int
    alive_fail_to_dead: int
    url_cn: str
    url_foreign: str
    url_https_check: str           # 用于验证 HTTP 代理 CONNECT 隧道能力的 https 目标
    expected_cn_status: int
    expected_foreign_status: List[int]
    expected_https_status: List[int]
    extra_headers: Dict[str, str] = field(default_factory=dict)


@dataclass
class AppConfig:
    web: WebConfig
    clash: ClashConfig
    fetch: FetchConfig
    check: CheckConfig
    gateway: GatewayConfig
    db: str
    log_level: str
    allowed_protocols: List[str]


def _load_yaml(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_config(path: str | None = None) -> AppConfig:
    """加载配置。默认读项目根的 config.yaml。"""
    if path is None:
        path = os.environ.get("PROXYPOOL_CONFIG", "config.yaml")
    raw = _load_yaml(path)

    sources = [
        SourceConfig(
            name=s["name"],
            type=s["type"],
            protocol=s.get("protocol", "auto"),
            url=s["url"],
            pages=int(s.get("pages", 1)),
        )
        for s in raw.get("fetch", {}).get("sources", [])
    ]

    # 校验
    web_raw = raw.get("web", {})
    if web_raw.get("token", "") in ("change-me-to-a-secret-token", "", None):
        import logging
        logging.warning("⚠ web.token 使用默认值或为空，请立即改为强随机串！")
    if web_raw.get("host", "0.0.0.0") == "0.0.0.0":
        gw_host = raw.get("clash", {}).get("gateway_host", "127.0.0.1")
        if gw_host in ("127.0.0.1", "localhost"):
            import logging
            logging.warning("⚠ web.host=0.0.0.0 但 clash.gateway_host=127.0.0.1，"
                            "局域网 Clash 无法连接 socks4 网关节点，请改为 NAS 的局域网 IP")

    return AppConfig(
        web=WebConfig(
            host=raw["web"]["host"],
            port=int(raw["web"]["port"]),
            token=raw["web"]["token"],
        ),
        clash=ClashConfig(
            gateway_host=raw.get("clash", {}).get("gateway_host", "127.0.0.1"),
        ),
        fetch=FetchConfig(
            interval_seconds=int(raw["fetch"]["interval_seconds"]),
            jitter_seconds=int(raw["fetch"]["jitter_seconds"]),
            timeout=int(raw["fetch"]["timeout"]),
            max_pages=int(raw["fetch"].get("max_pages", 5)),
            user_agent=raw["fetch"].get("user_agent", ""),
            sources=sources,
        ),
        check=CheckConfig(
            new_interval=int(raw["check"]["new_interval"]),
            full_interval=int(raw["check"]["full_interval"]),
            concurrency=int(raw["check"]["concurrency"]),
            timeout=int(raw["check"]["timeout"]),
            fail_threshold=int(raw["check"]["fail_threshold"]),
            alive_fail_to_dead=int(raw["check"]["alive_fail_to_dead"]),
            url_cn=raw["check"]["url_cn"],
            url_foreign=raw["check"]["url_foreign"],
            url_https_check=raw["check"]["url_https_check"],
            expected_cn_status=int(raw["check"]["expected_cn_status"]),
            expected_foreign_status=list(raw["check"]["expected_foreign_status"]),
            expected_https_status=list(raw["check"]["expected_https_status"]),
        ),
        gateway=GatewayConfig(
            listen_host=raw.get("gateway", {}).get("listen_host", "127.0.0.1"),
            port_start=int(raw.get("gateway", {}).get("port_start", 30000)),
            port_end=int(raw.get("gateway", {}).get("port_end", 31999)),
        ),
        db=raw.get("db", "data/proxies.db"),
        log_level=raw.get("log_level", "INFO"),
        allowed_protocols=raw.get("allowed_protocols", ["https", "socks4", "socks5"]),
    )


# 全局单例（main.py 启动时赋值）
CFG: AppConfig | None = None


def get_config() -> AppConfig:
    global CFG
    if CFG is None:
        CFG = load_config()
    return CFG
