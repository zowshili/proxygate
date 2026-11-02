"""配置加载：把 config.yaml 读成全局可访问的对象。"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import List

import yaml

log = logging.getLogger(__name__)


@dataclass
class SourceConfig:
    name: str
    type: str            # spider 类型，对应 fetcher/<type>.py
    protocol: str        # https / socks5 / auto
    url: str
    region: str = ""     # cn / hk / ''（空=等地理库判定）
    pages: int = 1


@dataclass
class WebConfig:
    host: str
    port: int
    token: str


@dataclass
class FetchConfig:
    interval_seconds: int
    jitter_seconds: int
    timeout: int
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
    url_https_check: str           # 用于验证 https 代理 CONNECT 隧道能力的 https 目标
    expected_cn_status: List[int]
    expected_foreign_status: List[int]
    expected_https_status: List[int]
    max_alive_latency_ms: int = 0  # 存活延迟上限（ms），0=不限；超过则标死
    max_per_cycle: int = 3000      # 单轮最多探活条数，避免首轮 1 万条把调度卡死


@dataclass
class GeoConfig:
    enabled: bool = True
    endpoint: str = "http://ip-api.com/batch"
    batch_size: int = 100          # ip-api 单次批量上限
    request_interval: float = 4.5  # 批间隔（免费版 15 次/分钟）
    cache_days: int = 7            # 同一 IP 的地理结果缓存天数
    max_per_cycle: int = 400       # 每轮最多解析多少个 IP，避免拖慢调度


@dataclass
class AppConfig:
    web: WebConfig
    fetch: FetchConfig
    check: CheckConfig
    geo: GeoConfig
    regions: List[str]
    db: str
    log_level: str
    allowed_protocols: List[str]
    retention_hours: int = 48


def _status_list(v) -> List[int]:
    if isinstance(v, int):
        return [v]
    return [int(x) for x in v]


def load_config(path: str) -> AppConfig:
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    sources = [
        SourceConfig(
            name=str(s["name"]),
            type=str(s["type"]),
            protocol=str(s.get("protocol", "auto")),
            url=str(s["url"]),
            region=str(s.get("region", "") or "").lower(),
            pages=int(s.get("pages", 1)),
        )
        for s in (raw.get("fetch", {}).get("sources") or [])
    ]

    ck = raw["check"]
    web = WebConfig(
        host=raw["web"].get("host", "127.0.0.1"),
        port=int(raw["web"].get("port", 8080)),
        token=str(raw["web"].get("token", "")),
    )
    if not web.token or web.token in ("YOUR_SECURE_TOKEN_HERE", "change-me"):
        log.warning("⚠ web.token 仍是占位值，所有端点等于无鉴权")
    if web.host == "0.0.0.0" and not web.token:
        log.warning("⚠ web.host=0.0.0.0 且 token 为空：公网可无条件拉取订阅")

    geo_raw = raw.get("geo") or {}
    geo = GeoConfig(
        enabled=bool(geo_raw.get("enabled", True)),
        endpoint=str(geo_raw.get("endpoint", "http://ip-api.com/batch")),
        batch_size=int(geo_raw.get("batch_size", 100)),
        request_interval=float(geo_raw.get("request_interval", 4.5)),
        cache_days=int(geo_raw.get("cache_days", 7)),
        max_per_cycle=int(geo_raw.get("max_per_cycle", 400)),
    )

    regions = [str(r).lower() for r in (raw.get("regions") or ["cn", "hk"])]

    return AppConfig(
        web=web,
        fetch=FetchConfig(
            interval_seconds=int(raw["fetch"]["interval_seconds"]),
            jitter_seconds=int(raw["fetch"]["jitter_seconds"]),
            timeout=int(raw["fetch"]["timeout"]),
            user_agent=str(raw["fetch"]["user_agent"]),
            sources=sources,
        ),
        check=CheckConfig(
            new_interval=int(ck["new_interval"]),
            full_interval=int(ck["full_interval"]),
            concurrency=int(ck["concurrency"]),
            timeout=int(ck["timeout"]),
            fail_threshold=int(ck["fail_threshold"]),
            alive_fail_to_dead=int(ck["alive_fail_to_dead"]),
            url_cn=ck["url_cn"],
            url_foreign=ck["url_foreign"],
            url_https_check=ck["url_https_check"],
            expected_cn_status=_status_list(ck["expected_cn_status"]),
            expected_foreign_status=_status_list(ck["expected_foreign_status"]),
            expected_https_status=_status_list(ck["expected_https_status"]),
            max_alive_latency_ms=int(ck.get("max_alive_latency_ms", 0)),
            max_per_cycle=int(ck.get("max_per_cycle", 3000)),
        ),
        geo=geo,
        regions=regions,
        db=str(raw.get("db", "data/proxies.db")),
        log_level=str(raw.get("log_level", "INFO")),
        allowed_protocols=list(raw.get("allowed_protocols", ["https", "socks5"])),
        retention_hours=int(raw.get("retention_hours", 48)),
    )
