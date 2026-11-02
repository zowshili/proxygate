"""把存活代理导出成可分发文件（CI 定时任务 / 离线分发用）。

产物（默认写到 public/）：
    proxies.txt            全部存活，一行一个 ip:port
    proxies-cn.txt         中国大陆
    proxies-hk.txt         香港
    proxies-socks5.txt     按协议切分
    proxies-https.txt
    clash.yaml             全部区域 Clash 订阅
    clash-cn.yaml
    clash-hk.yaml
    meta.json              生成时间与统计

txt 只有代理本体，没有注释行、没有空行，方便 grep / awk / sed 直接吃。
"""
from __future__ import annotations

import datetime
import json
import logging
import os
import tempfile
from typing import Dict, Iterable, List

from .clash import build_clash_config
from .config import AppConfig
from .models import Proxy
from .store import Store

log = logging.getLogger(__name__)

# hostport: ip:port（curl -x / 大多扫描器 / 浏览器扩展直接可用）
# scheme  : protocol://ip:port（喂给支持协议的客户端，不会把 socks5 当 http 用）
TXT_FORMATS = ("hostport", "scheme")


def render_txt(proxies: Iterable[Proxy], fmt: str = "hostport") -> str:
    fmt = (fmt or "hostport").lower()
    if fmt not in TXT_FORMATS:
        raise ValueError(f"fmt must be one of {TXT_FORMATS}")
    if fmt == "scheme":
        lines = [f"{p.protocol}://{p.ip}:{p.port}" for p in proxies]
    else:
        lines = [f"{p.ip}:{p.port}" for p in proxies]
    if not lines:
        return ""
    return "\n".join(lines) + "\n"


def _write(path: str, text: str) -> None:
    """原子写：先写同目录临时文件再 rename，避免 CI 提交半截文件。"""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp-", suffix=".swap")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def export_all(
    cfg: AppConfig,
    store: Store,
    outdir: str = "public",
    limit: int = 20000,
    txt_fmt: str = "hostport",
    scope: str = "full",
) -> Dict[str, int]:
    """生成导出文件，返回 {文件名: 条数}。

    scope="full"   全部文件：全局 + 协议 + 区域 + meta.json（GitHub Actions 用）
    scope="region" 只写 cn/hk 四个文件 + meta-region.json（大陆侧发布用，
                   避免和 Actions 抢同一批文件）
    """
    if scope not in ("full", "region"):
        raise ValueError("scope must be full or region")

    by_region: Dict[str, List[Proxy]] = {
        r: store.list_alive(region=r, limit=limit) for r in cfg.regions
    }
    counts: Dict[str, int] = {}

    def _txt(name: str, proxies: List[Proxy]) -> None:
        _write(os.path.join(outdir, name), render_txt(proxies, txt_fmt))
        counts[name] = len(proxies)

    def _clash(name: str, proxies: List[Proxy], region: str | None) -> None:
        _write(os.path.join(outdir, name),
               build_clash_config(proxies, title="ProxyGate", region=region))
        counts[name] = len(proxies)

    all_proxies: List[Proxy] = []
    if scope == "full":
        all_proxies = store.list_alive(limit=limit)
        by_proto: Dict[str, List[Proxy]] = {
            p: store.list_alive(protocol=p, limit=limit) for p in ("socks5", "https")
        }
        _txt("proxies.txt", all_proxies)
        for proto, proxies in by_proto.items():
            _txt(f"proxies-{proto}.txt", proxies)
        _clash("clash.yaml", all_proxies, None)

    for region, proxies in by_region.items():
        _txt(f"proxies-{region}.txt", proxies)
        _clash(f"clash-{region}.yaml", proxies, region)

    now = datetime.datetime.now(datetime.timezone.utc)
    meta = {
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "generated_at_cst": now.astimezone(
            datetime.timezone(datetime.timedelta(hours=8))
        ).strftime("%Y-%m-%d %H:%M:%S"),
        "scope": scope,
        "txt_format": txt_fmt,
        "counts": {
            "all": len(all_proxies),
            **{r: len(ps) for r, ps in by_region.items()},
        },
        "files": counts,
        "regions": list(cfg.regions),
        "max_alive_latency_ms": cfg.check.max_alive_latency_ms,
    }
    if scope == "full":
        meta["counts"].update({p: len(ps) for p, ps in by_proto.items()})
        meta["stats"] = store.stats()
    meta_name = "meta.json" if scope == "full" else "meta-region.json"
    _write(os.path.join(outdir, meta_name),
           json.dumps(meta, ensure_ascii=False, indent=2) + "\n")

    log.info("export[%s]: %s → %s", scope,
             ", ".join(f"{k}={v}" for k, v in counts.items()), outdir)
    return counts
