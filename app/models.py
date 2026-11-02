"""数据模型。"""
from __future__ import annotations

from dataclasses import dataclass, asdict

# 区域常量：cn=中国大陆，hk=香港，other=其他地区（两个订阅端点都不收），''=待判定
REGION_CN = "cn"
REGION_HK = "hk"
REGION_OTHER = "other"
REGION_UNKNOWN = ""

REGIONS = (REGION_CN, REGION_HK)


@dataclass
class Proxy:
    ip: str
    port: int
    protocol: str          # https / socks5
    country: str = ""      # 国家/地区显示名，从源页面解析，缺失留空
    source: str = ""       # 来源 spider 名
    region: str = ""       # cn / hk / other / ''

    # 探活状态
    latency_ms: int = 0            # 最近一次成功延迟
    alive: int | None = None       # 1 可用 / 0 不可用 / NULL 未检
    cn_reachable: int = 0          # baidu 可达 0/1
    foreign_reachable: int = 0     # gstatic 可达 0/1
    fail_count: int = 0
    success_count: int = 0

    first_seen: int = 0            # 入库时间戳
    last_seen: int = 0             # 最近一次采集到的时间戳
    last_check: int = 0            # 最近一次探活时间戳

    def key(self) -> tuple[str, int, str]:
        return (self.ip, self.port, self.protocol)

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def labels(self) -> list[str]:
        tags = []
        if self.cn_reachable:
            tags.append("cn")
        if self.foreign_reachable:
            tags.append("foreign")
        return tags
