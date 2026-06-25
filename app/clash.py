"""Clash YAML 订阅生成。按协议 x 延迟分档分组：

协议 4 类：HTTP / HTTPS / SOCKS5 / SOCKS4
延迟 3 档：Fast(<1s) / Medium(1~4s) / Slow(≥4s)

各协议有 All 父组，内含 3 个延迟子组。PROXY 组包含全部协议父组。
"""
from __future__ import annotations

import logging
from typing import Dict, List, Optional, Tuple

import yaml

from .gateway import Gateway
from .models import Proxy

log = logging.getLogger(__name__)

# 延迟分档边界（毫秒）
TIER_FAST_TOP = 1000
TIER_MEDIUM_TOP = 4000

# 协议显示名称（排序 & 标签映射）
_PROTO_LABELS: dict[str, str] = {
    "http": "HTTP",
    "https": "HTTPS",
    "socks5": "SOCKS5",
    "socks4": "SOCKS4",
}


def _latency_str(ms: int) -> str:
    """1921ms -> '1.9s',  345ms -> '345ms'"""
    if ms <= 0:
        return "?"
    if ms < 1000:
        return f"{ms}ms"
    return f"{ms / 1000:.1f}s"


def _tier(ms: int) -> str | None:
    if ms <= 0:
        return None
    if ms < TIER_FAST_TOP:
        return "Fast"
    if ms < TIER_MEDIUM_TOP:
        return "Medium"
    return "Slow"


def _clash_type(protocol: str) -> str:
    return {
        "http": "http",
        "https": "http",
        "socks5": "socks5",
        "socks4": "socks5",
    }.get(protocol, "http")


def _sanitize(text: str) -> str:
    """移除不可见控制字符，保留字母/数字/中文/常见标点。"""
    return "".join(c for c in text if c.isprintable() or ord(c) > 127)


def _node_name(p: Proxy, idx: int, proto_tag: str) -> str:
    labels = "-".join(p.labels) if p.labels else "nolabel"
    loc = _sanitize(p.country or "NA").replace(" ", "")[:12]
    return f"{idx:03d}|{proto_tag}|{loc}|{p.ip}:{p.port}|{labels}|{_latency_str(p.latency_ms)}"


def _proto_tag(protocol: str) -> str:
    return {"http": "http", "https": "https", "socks5": "s5", "socks4": "s4"}.get(
        protocol, "?"
    )


def _build_proto_groups(
    protocol: str,
    proxies: List[Tuple[int, dict]],
    label: str,
) -> List[dict]:
    """为单个协议创建 1 个 All 组 + 3 个延迟 Tier 组。

    proxies: [(idx, node_dict), ...]
    返回 4 个 group dict（All + Fast + Medium + Slow）；无节点时 All 用 DIRECT。
    """
    groups: List[dict] = []
    fast_nodes: List[str] = []
    med_nodes: List[str] = []
    slow_nodes: List[str] = []
    unknown_nodes: List[str] = []

    for _, nd in proxies:
        n = nd["_latency_ms"]
        tier = _tier(n)
        name = nd["name"]
        if tier == "Fast":
            fast_nodes.append(name)
        elif tier == "Medium":
            med_nodes.append(name)
        elif tier == "Slow":
            slow_nodes.append(name)
        else:
            unknown_nodes.append(name)

    tier_groups = []
    for tier_name, nodes in [("Fast", fast_nodes), ("Medium", med_nodes), ("Slow", slow_nodes)]:
        gname = f"{label}-{tier_name}"
        tiertag = tier_name.lower()
        tier_groups.append({"name": gname, "type": "select",
                            "proxies": nodes or ["DIRECT"]})

    # All 组：包含 3 个 tier 子组 + 全部节点（方便直接全选）
    all_proxies = [g["name"] for g in tier_groups]
    all_proxies.extend(unknown_nodes)
    groups.append({
        "name": f"{label}-All",
        "type": "select",
        "proxies": all_proxies or ["DIRECT"],
    })
    groups.extend(tier_groups)
    return groups


def build_clash_config(
    proxies: List[Proxy],
    gateway: Optional[Gateway] = None,
    gateway_host: str = "127.0.0.1",
    title: str = "proxypool",
) -> str:
    nodes: List[dict] = []
    # 按协议收集节点
    proto_nodes: Dict[str, List[Tuple[int, dict]]] = {
        "http": [], "https": [], "socks5": [], "socks4": [],
    }
    foreign_names: List[str] = []
    cn_names: List[str] = []
    all_names: List[str] = []

    for i, p in enumerate(proxies, start=1):
        tag = _proto_tag(p.protocol)
        ctype = _clash_type(p.protocol)

        if p.protocol == "socks4":
            if gateway is None:
                continue
            port = gateway.get_bridge_port(p.ip, p.port)
            if port is None:
                continue
            node = {
                "name": _node_name(p, i, tag),
                "type": "socks5",
                "server": gateway_host,
                "port": port,
                "_latency_ms": p.latency_ms,
            }
        elif p.protocol == "socks5":
            node = {
                "name": _node_name(p, i, tag),
                "type": "socks5",
                "server": p.ip,
                "port": p.port,
                "_latency_ms": p.latency_ms,
            }
        else:
            # http / https
            node = {
                "name": _node_name(p, i, tag),
                "type": "http",
                "server": p.ip,
                "port": p.port,
                "_latency_ms": p.latency_ms,
            }

        nodes.append(node)
        all_names.append(node["name"])
        if p.protocol in proto_nodes:
            proto_nodes[p.protocol].append((i, node))
        if p.foreign_reachable:
            foreign_names.append(node["name"])
        if p.cn_reachable:
            cn_names.append(node["name"])

    proxy_groups = []

    if not all_names:
        proxy_groups.append({"name": "PROXY", "type": "select",
                             "proxies": ["DIRECT"]})
        return yaml.dump(
            {"port": 7890, "proxies": [], "proxy-groups": proxy_groups, "rules": []},
            allow_unicode=True, sort_keys=False,
        )

    # 自动测速组
    proxy_groups.append({
        "name": "AUTO-Foreign", "type": "url-test",
        "url": "http://www.gstatic.com/generate_204", "interval": 300,
        "proxies": foreign_names or all_names,
    })
    proxy_groups.append({
        "name": "AUTO-CN", "type": "url-test",
        "url": "http://www.baidu.com", "interval": 300,
        "proxies": cn_names or all_names,
    })

    # 按协议（HTTP / HTTPS / SOCKS5 / SOCKS4）建 All→Tier 组
    proto_group_names: List[str] = []
    for proto in ["http", "https", "socks5", "socks4"]:
        p_nodes = proto_nodes.get(proto, [])
        label = _PROTO_LABELS[proto]
        if not p_nodes:
            # 输出骨架空组（含 DIRECT 占位），保持分组结构一致
            proxy_groups.append({
                "name": f"{label}-All", "type": "select", "proxies": ["DIRECT"],
            })
            for tier in ("Fast", "Medium", "Slow"):
                proxy_groups.append({
                    "name": f"{label}-{tier}", "type": "select", "proxies": ["DIRECT"],
                })
            proto_group_names.append(f"{label}-All")
            continue
        groups = _build_proto_groups(proto, p_nodes, label)
        proto_group_names.append(f"{label}-All")
        proxy_groups.extend(groups)

    # PROXY 主选择组
    proxy_groups.insert(0, {
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
        "proxies": _clean_nodes(nodes),
        "proxy-groups": proxy_groups,
        "rules": rules,
    }
    return yaml.dump(config, allow_unicode=True, sort_keys=False)


def _clean_nodes(nodes: List[dict]) -> List[dict]:
    """去除内部字段（_latency_ms），只保留 Clash 字段。"""
    return [
        {k: v for k, v in nd.items() if not k.startswith("_")}
        for nd in nodes
    ]
