"""FastAPI Web 服务：订阅 + 管理 API，带 token 鉴权。"""
from __future__ import annotations

import base64
import logging
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse, Response

from .clash import build_clash_config
from .config import AppConfig
from .gateway import Gateway
from .store import Store

log = logging.getLogger(__name__)


def create_app(cfg: AppConfig, store: Store, scheduler, gateway: Gateway,
               lifespan=None) -> FastAPI:
    app = FastAPI(title="proxypool", docs_url="/docs", redoc_url=None, lifespan=lifespan)

    # —— token 鉴权依赖 ——
    def verify_token(request: Request, token: Optional[str] = Query(default=None)):
        t = (token or request.headers.get("X-Token")
             or request.headers.get("Authorization", "").removeprefix("Bearer ").strip())
        if t != cfg.web.token:
            raise HTTPException(status_code=401, detail="invalid token")
        return True

    protected = Depends(verify_token)

    @app.get("/")
    def root():
        return {"app": "proxypool", "status": "running",
                "endpoints": ["/sub/clash", "/sub/v2ray", "/sub/raw",
                              "/api/list", "/api/proxies", "/api/stats",
                              "/api/update", "/api/gateway/status"]}

    # —— 订阅 ——
    @app.get("/sub/clash", response_class=PlainTextResponse, dependencies=[protected])
    def sub_clash(
        type: str = Query("all", description="http/socks4/socks5/all"),
        country: str = Query("", description="按国家/地区关键字过滤"),
        label: str = Query("", description="cn/foreign"),
        min_latency: Optional[int] = Query(None, description="最大延迟 ms"),
        limit: int = Query(2000, ge=1, le=20000),
    ):
        proxies = store.list_alive(
            protocol=(type if type != "all" else None),
            label=(label or None),
            min_latency=min_latency,
            limit=limit,
        )
        if country:
            proxies = [p for p in proxies if country.lower() in (p.country or "").lower()]
        yaml_text = build_clash_config(
            proxies, gateway=gateway,
            gateway_host=cfg.clash.gateway_host, title="proxypool",
        )
        return PlainTextResponse(
            yaml_text,
            media_type="application/yaml; charset=utf-8",
            headers={
                "Content-Disposition": 'attachment; filename="proxypool.yaml"',
                "Profile-Update-Interval": "1",
                "Cache-Control": "no-store",
            },
        )

    @app.get("/sub/v2ray", response_class=PlainTextResponse, dependencies=[protected])
    def sub_v2ray(
        type: str = Query("all"),
        label: str = Query(""),
        limit: int = Query(2000, ge=1, le=20000),
    ):
        proxies = store.list_alive(
            protocol=(type if type != "all" else None),
            label=(label or None),
            limit=limit,
        )
        lines = [f"{p.protocol}://{p.ip}:{p.port}" for p in proxies]
        payload = "\n".join(lines)
        b64 = base64.b64encode(payload.encode()).decode()
        return PlainTextResponse(b64, media_type="text/plain; charset=utf-8")

    @app.get("/sub/raw", response_class=PlainTextResponse, dependencies=[protected])
    def sub_raw(
        type: str = Query("all"),
        label: str = Query(""),
        limit: int = Query(2000, ge=1, le=20000),
    ):
        proxies = store.list_alive(
            protocol=(type if type != "all" else None),
            label=(label or None),
            limit=limit,
        )
        lines = [f"{p.protocol}://{p.ip}:{p.port}  # {p.country} {p.latency_ms}ms {','.join(p.labels)}"
                 for p in proxies]
        return PlainTextResponse("\n".join(lines), media_type="text/plain; charset=utf-8")

    # —— API ——
    @app.get("/api/stats", dependencies=[protected])
    def stats():
        s = store.stats()
        s["last_fetch_at_str"] = _ts_str(s.get("last_fetch_at"))
        s["last_check_at_str"] = _ts_str(s.get("last_check_at"))
        s["gateway"] = gateway.stats()
        return s

    @app.get("/api/proxies", dependencies=[protected])
    def list_proxies(
        type: str = Query("all"),
        label: str = Query(""),
        min_latency: Optional[int] = Query(None),
        limit: int = Query(500, ge=1, le=20000),
        offset: int = Query(0, ge=0),
    ):
        proxies = store.list_alive(
            protocol=(type if type != "all" else None),
            label=(label or None),
            min_latency=min_latency,
            limit=limit + offset,
        )
        slice_ = proxies[offset: offset + limit]
        return {
            "total": len(proxies),
            "offset": offset,
            "limit": limit,
            "items": [p.to_dict() for p in slice_],
        }

    @app.get("/api/list", dependencies=[protected])
    def api_list(
        type: str = Query("all", description="http/https/socks4/socks5/all"),
        label: str = Query("", description="cn/foreign"),
        country: str = Query("", description="国家/地区关键字"),
        min_latency: Optional[int] = Query(None, description="最大延迟 ms"),
        limit: int = Query(100, ge=1, le=20000),
    ):
        """对外工具友好的 JSON API。返回存活代理列表（无分页）。"""
        proxies = store.list_alive(
            protocol=(type if type != "all" else None),
            label=(label or None),
            min_latency=min_latency,
            limit=limit,
        )
        if country:
            proxies = [p for p in proxies if country.lower() in (p.country or "").lower()]
        return {
            "success": True,
            "count": len(proxies),
            "proxies": [p.to_dict() for p in proxies],
        }

    @app.get("/api/gateway/status", dependencies=[protected])
    def gateway_status():
        return gateway.stats()

    @app.get("/api/gateway/bridges", dependencies=[protected])
    def gateway_bridges():
        return {"bridges": gateway.list_bridges()}

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
        return {"ok": True}

    return app


def _ts_str(ts) -> str:
    if not ts:
        return ""
    import datetime
    return datetime.datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M:%S")
