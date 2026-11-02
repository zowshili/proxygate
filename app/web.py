"""FastAPI Web 服务：按区域分开的订阅 + 管理 API（v2.0.0）。

端点：
  /sub/clash/{region}   region = cn | hk | all
  /sub/txt/{region}     纯文本，一行一个代理（fmt=hostport|scheme）
  /sub/raw/{region}
  /sub/v2ray/{region}
  /api/list/{region}
  /api/stats  /api/update  /healthz
兼容旧路径：/sub/clash?region=cn 与不带 region（等于 all）。
"""
from __future__ import annotations

import base64
import datetime
import logging
import threading
import time
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse

from .clash import build_clash_config
from .config import AppConfig
from .export import TXT_FORMATS, render_txt
from .store import Store

log = logging.getLogger(__name__)

_CACHE_TTL = 60  # 秒；同一订阅在 1 分钟内直接命中内存


def _bucket() -> int:
    """时间桶：同一分钟内所有请求共用一份生成结果。"""
    return int(time.time() // _CACHE_TTL)


def create_app(cfg: AppConfig, store: Store, scheduler, lifespan=None) -> FastAPI:
    app = FastAPI(title="proxypool", docs_url=None, redoc_url=None, lifespan=lifespan)

    cache: dict[tuple, tuple[float, str]] = {}
    cache_lock = threading.Lock()

    def _cached(key: tuple, producer) -> str:
        now = time.time()
        with cache_lock:
            hit = cache.get(key)
            if hit and now - hit[0] < _CACHE_TTL:
                return hit[1]
        text = producer()
        with cache_lock:
            if len(cache) > 64:
                cache.clear()
            cache[key] = (now, text)
        return text

    # —— token 鉴权 ——
    def verify_token(request: Request, token: Optional[str] = Query(default=None)):
        t = (token or request.headers.get("X-Token")
             or request.headers.get("Authorization", "").removeprefix("Bearer ").strip())
        if not cfg.web.token or not _const_eq(t, cfg.web.token):
            raise HTTPException(status_code=401, detail="invalid token")
        return True

    protected = Depends(verify_token)

    def _region(region: str) -> str:
        r = (region or "all").lower()
        allowed = set(cfg.regions) | {"all"}
        if r not in allowed:
            raise HTTPException(status_code=400, detail=f"region must be one of {sorted(allowed)}")
        return r

    def _query(region: str, type_: str, label: str, min_latency: Optional[int],
               limit: int, offset: int = 0):
        return store.list_alive(
            protocol=(type_ if type_ != "all" else None),
            label=(label or None),
            region=(region if region != "all" else None),
            min_latency=min_latency,
            limit=limit,
            offset=offset,
        )

    @app.get("/")
    def root():
        return {
            "app": "proxypool", "version": "2.0.0", "status": "running",
            "regions": cfg.regions,
            "endpoints": [
                "/sub/clash/cn", "/sub/clash/hk",
                "/sub/txt/cn", "/sub/txt/hk",
                "/sub/raw/cn", "/sub/raw/hk",
                "/sub/v2ray/cn", "/sub/v2ray/hk",
                "/api/list/cn", "/api/list/hk",
                "/api/stats", "/api/update", "/healthz",
            ],
        }

    # —— 订阅 ——
    def _clash_payload(region: str, type_: str, label: str,
                       min_latency: Optional[int], limit: int) -> str:
        key = ("clash", _bucket(), region, type_, label, min_latency, limit)
        def build() -> str:
            proxies = _query(region, type_, label, min_latency, limit)
            return build_clash_config(proxies, title="proxypool", region=region)
        return _cached(key, build)

    def _txt_payload(region: str, type_: str, label: str, fmt: str,
                     min_latency: Optional[int], limit: int) -> str:
        """纯文本订阅：一行一个代理，无注释、无空行。"""
        fmt = (fmt or "hostport").lower()
        if fmt not in TXT_FORMATS:
            raise HTTPException(status_code=400, detail=f"fmt must be one of {list(TXT_FORMATS)}")
        key = ("txt", _bucket(), region, type_, label, fmt, min_latency, limit)
        def build() -> str:
            return render_txt(_query(region, type_, label, min_latency, limit), fmt)
        return _cached(key, build)

    @app.get("/sub/clash", response_class=PlainTextResponse, dependencies=[protected])
    def sub_clash_all(
        region: str = Query("all", description="cn/hk/all"),
        type: str = Query("all", description="https/socks5/all"),
        label: str = Query("", description="cn/foreign"),
        min_latency: Optional[int] = Query(None, description="最大延迟 ms"),
        limit: int = Query(2000, ge=1, le=20000),
    ):
        return _clash_response(_clash_payload(_region(region), type, label, min_latency, limit))

    @app.get("/sub/clash/{region}", response_class=PlainTextResponse, dependencies=[protected])
    def sub_clash(
        region: str,
        type: str = Query("all", description="https/socks5/all"),
        label: str = Query("", description="cn/foreign"),
        min_latency: Optional[int] = Query(None, description="最大延迟 ms"),
        limit: int = Query(2000, ge=1, le=20000),
    ):
        return _clash_response(_clash_payload(_region(region), type, label, min_latency, limit))

    @app.get("/sub/txt", response_class=PlainTextResponse, dependencies=[protected])
    def sub_txt_all(
        region: str = Query("all", description="cn/hk/all"),
        type: str = Query("all", description="https/socks5/all"),
        label: str = Query("", description="cn/foreign"),
        fmt: str = Query("hostport", description="hostport → ip:port；scheme → protocol://ip:port"),
        min_latency: Optional[int] = Query(None, description="最大延迟 ms"),
        limit: int = Query(2000, ge=1, le=20000),
    ):
        return _txt_response(_txt_payload(_region(region), type, label, fmt, min_latency, limit))

    @app.get("/sub/txt/{region}", response_class=PlainTextResponse, dependencies=[protected])
    def sub_txt(
        region: str,
        type: str = Query("all", description="https/socks5/all"),
        label: str = Query("", description="cn/foreign"),
        fmt: str = Query("hostport", description="hostport → ip:port；scheme → protocol://ip:port"),
        min_latency: Optional[int] = Query(None, description="最大延迟 ms"),
        limit: int = Query(2000, ge=1, le=20000),
    ):
        return _txt_response(_txt_payload(_region(region), type, label, fmt, min_latency, limit))

    @app.get("/sub/raw/{region}", response_class=PlainTextResponse, dependencies=[protected])
    def sub_raw(
        region: str,
        type: str = Query("all"),
        label: str = Query(""),
        limit: int = Query(2000, ge=1, le=20000),
    ):
        r = _region(region)
        key = ("raw", _bucket(), r, type, label, limit)
        def build() -> str:
            proxies = _query(r, type, label, None, limit)
            return "\n".join(
                f"{p.protocol}://{p.ip}:{p.port}  # {p.region or '?'} {p.country} "
                f"{p.latency_ms}ms {','.join(p.labels)}"
                for p in proxies
            )
        return PlainTextResponse(_cached(key, build), media_type="text/plain; charset=utf-8")

    @app.get("/sub/v2ray/{region}", response_class=PlainTextResponse, dependencies=[protected])
    def sub_v2ray(
        region: str,
        type: str = Query("all"),
        label: str = Query(""),
        limit: int = Query(2000, ge=1, le=20000),
    ):
        r = _region(region)
        key = ("v2ray", _bucket(), r, type, label, limit)
        def build() -> str:
            proxies = _query(r, type, label, None, limit)
            payload = "\n".join(f"{p.protocol}://{p.ip}:{p.port}" for p in proxies)
            return base64.b64encode(payload.encode()).decode()
        return PlainTextResponse(_cached(key, build), media_type="text/plain; charset=utf-8")

    # —— API ——
    @app.get("/api/stats", dependencies=[protected])
    def stats():
        s = store.stats()
        s["last_fetch_at_str"] = _ts_str(s.get("last_fetch_at"))
        s["last_check_at_str"] = _ts_str(s.get("last_check_at"))
        s["regions"] = cfg.regions
        s["region_counts"] = {
            r: store.count_alive(region=r) for r in cfg.regions
        }
        return s

    @app.get("/api/list", dependencies=[protected])
    def api_list_all(
        region: str = Query("all"),
        type: str = Query("all"),
        label: str = Query(""),
        country: str = Query(""),
        min_latency: Optional[int] = Query(None),
        limit: int = Query(100, ge=1, le=20000),
    ):
        return _list_payload(_region(region), type, label, country, min_latency, limit)

    @app.get("/api/list/{region}", dependencies=[protected])
    def api_list(
        region: str,
        type: str = Query("all"),
        label: str = Query(""),
        country: str = Query(""),
        min_latency: Optional[int] = Query(None),
        limit: int = Query(100, ge=1, le=20000),
    ):
        return _list_payload(_region(region), type, label, country, min_latency, limit)

    def _list_payload(region: str, type_: str, label: str, country: str,
                      min_latency: Optional[int], limit: int) -> dict:
        proxies = _query(region, type_, label, min_latency, limit)
        if country:
            proxies = [p for p in proxies if country.lower() in (p.country or "").lower()]
        return {
            "success": True,
            "region": region,
            "count": len(proxies),
            "proxies": [p.to_dict() for p in proxies],
        }

    @app.get("/api/proxies", dependencies=[protected])
    def list_proxies(
        region: str = Query("all"),
        type: str = Query("all"),
        label: str = Query(""),
        min_latency: Optional[int] = Query(None),
        limit: int = Query(500, ge=1, le=20000),
        offset: int = Query(0, ge=0),
    ):
        r = _region(region)
        proxies = _query(r, type, label, min_latency, limit, offset)
        return {
            "total": store.count_alive(region=(r if r != "all" else None),
                                       protocol=(type if type != "all" else None)),
            "offset": offset, "limit": limit,
            "items": [p.to_dict() for p in proxies],
        }

    @app.post("/api/update", dependencies=[protected])
    async def trigger_update():
        try:
            result = await scheduler.trigger_update()
            return {"ok": True, "result": result}
        except Exception as e:
            log.exception("manual update failed")
            raise HTTPException(status_code=500, detail=str(e))

    @app.get("/healthz")
    def healthz():
        return {"ok": True, "version": "2.0.0", "alive": store.count_alive()}

    return app


def _txt_response(text: str) -> PlainTextResponse:
    return PlainTextResponse(
        text,
        media_type="text/plain; charset=utf-8",
        headers={"Cache-Control": "no-store"},
    )


def _clash_response(yaml_text: str) -> PlainTextResponse:
    return PlainTextResponse(
        yaml_text,
        media_type="application/yaml; charset=utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="proxypool.yaml"',
            "Profile-Update-Interval": "1",
            "Cache-Control": "no-store",
        },
    )


def _const_eq(a: str, b: str) -> bool:
    """定长时间比较，避免 token 逐字节比对侧信道。"""
    if len(a) != len(b):
        return False
    r = 0
    for x, y in zip(a.encode(), b.encode()):
        r |= x ^ y
    return r == 0


def _ts_str(ts) -> str:
    if not ts:
        return ""
    return datetime.datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M:%S")
