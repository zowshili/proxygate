"""Clash YAML 订阅生成（v2.0.0：按区域出订阅，去掉 socks4 网关）。

分组结构固定 11 组，避免刷新订阅时因节点增减导致用户已选分组被重置：
  PROXY / AUTO-Foreign / AUTO-CN
  HTTPS-All  → HTTPS-Fast / HTTPS-Medium / HTTPS-Slow
  SOCKS5-All → SOCKS5-Fast / SOCKS5-Medium / SOCKS5-Slow
"""
from __future__ import annotations

from typing import Dict, List, Tuple

import yaml

from .models import Proxy

TIER_FAST_TOP = 1000
TIER_MEDIUM_TOP = 4000

_PROTO_LABELS = {"https": "HTTPS", "socks5": "SOCKS5", "http": "HTTP"}


def _latency_str(ms: int) -> str:
    if ms <= 0:
        return "?"
    if ms < 1000:
        return f"{ms}ms"
    return f"{ms / 1000:.1f}s"


def _tier(ms: int) -> str:
    if ms <= 0:
        return "unknown"
    if ms < TIER_FAST_TOP:
        return "Fast"
    if ms < TIER_MEDIUM_TOP:
        return "Medium"
    return "Slow"


def _clash_type(protocol: str) -> str:
    # Clash 里 https 代理也是 type: http（走 CONNECT）
    return "socks5" if protocol == "socks5" else "http"


def _sanitize(text: str) -> str:
    return "".join(c for c in text if c.isprintable() or ord(c) > 127)


def _node_name(p: Proxy, idx: int, proto_tag: str) -> str:
    labels = "-".join(p.labels) if p.labels else "nolabel"
    loc = _sanitize(p.country or (p.region or "NA").upper()).replace(" ", "")[:10]
    return f"{idx:03d}|{proto_tag}|{loc}|{p.ip}:{p.port}|{labels}|{_latency_str(p.latency_ms)}"


def _proto_tag(protocol: str) -> str:
    return {"https": "https", "socks5": "s5", "http": "http"}.get(protocol, "?")


def _build_proto_groups(label: str, nodes: List[Tuple[int, dict]]) -> List[dict]:
    buckets: Dict[str, List[str]] = {"Fast": [], "Medium": [], "Slow": [], "unknown": []}
    for _, nd in nodes:
        buckets[_tier(nd["_latency_ms"])].append(nd["name"])

    tier_groups = []
    for tier in ("Fast", "Medium", "Slow"):
        tier_groups.append({
            "name": f"{label}-{tier}",
            "type": "select",
            "proxies": buckets[tier] or ["DIRECT"],
        })
    all_names = [g["name"] for g in tier_groups] + buckets["unknown"]
    return [{
        "name": f"{label}-All",
        "type": "select",
        "proxies": all_names or ["DIRECT"],
    }] + tier_groups


def build_clash_config(
    proxies: List[Proxy],
    title: str = "proxypool",
    region: str | None = None,
) -> str:
    nodes: List[dict] = []
    proto_nodes: Dict[str, List[Tuple[int, dict]]] = {"https": [], "socks5": []}
    foreign_names: List[str] = []
    cn_names: List[str] = []
    all_names: List[str] = []

    for i, p in enumerate(proxies, start=1):
        node = {
            "name": _node_name(p, i, _proto_tag(p.protocol)),
            "type": _clash_type(p.protocol),
            "server": p.ip,
            "port": p.port,
            "_latency_ms": p.latency_ms,
        }
        # UDP 对免费代理基本不可用，显式关掉避免 Clash 走 UDP 直连造成"能上但漏流量"
        node["udp"] = False
        if p.protocol == "socks5":
            node["skip-cert-verify"] = True

        nodes.append(node)
        all_names.append(node["name"])
        proto_nodes.setdefault(p.protocol, []).append((i, node))
        if p.foreign_reachable:
            foreign_names.append(node["name"])
        if p.cn_reachable:
            cn_names.append(node["name"])

    if not all_names:
        return yaml.dump(
            {
                "port": 7890,
                "proxies": [],
                "proxy-groups": [
                    {"name": "PROXY", "type": "select", "proxies": ["DIRECT"]},
                ],
                "rules": ["MATCH,DIRECT"],
            },
            allow_unicode=True, sort_keys=False,
        )

    groups: List[dict] = []
    groups.append({
        "name": "AUTO-Foreign", "type": "url-test",
        "url": "http://www.gstatic.com/generate_204", "interval": 300,
        "tolerance": 100,
        "proxies": foreign_names or all_names,
    })
    groups.append({
        "name": "AUTO-CN", "type": "url-test",
        "url": "http://www.baidu.com", "interval": 300,
        "tolerance": 100,
        "proxies": cn_names or all_names,
    })

    proto_group_names: List[str] = []
    for proto in ("https", "socks5"):
        label = _PROTO_LABELS[proto]
        groups.extend(_build_proto_groups(label, proto_nodes.get(proto, [])))
        proto_group_names.append(f"{label}-All")

    groups.insert(0, {
        "name": "PROXY", "type": "select",
        "proxies": ["AUTO-Foreign", "AUTO-CN"] + proto_group_names + ["DIRECT"],
    })

    rules = [
        "DOMAIN-SUFFIX,google.com,AUTO-Foreign",
        "DOMAIN-SUFFIX,gstatic.com,AUTO-Foreign",
        "DOMAIN-SUFFIX,youtube.com,AUTO-Foreign",
        "DOMAIN-SUFFIX,github.com,AUTO-Foreign",
        "DOMAIN-SUFFIX,baidu.com,AUTO-CN",
        "DOMAIN-SUFFIX,taobao.com,AUTO-CN",
        "GEOIP,CN,AUTO-CN",
        "MATCH,PROXY",
    ]

    config = {
        "port": 7890,
        "socks-port": 7891,
        "allow-lan": False,
        "mode": "rule",
        "log-level": "info",
        "external-controller": "127.0.0.1:9090",
        "proxies": [
            {k: v for k, v in nd.items() if not k.startswith("_")} for nd in nodes
        ],
        "proxy-groups": groups,
        "rules": rules,
    }
    if region:
        config["name"] = f"{title}-{region}"
    return yaml.dump(config, allow_unicode=True, sort_keys=False)
