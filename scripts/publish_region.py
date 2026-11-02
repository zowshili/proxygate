"""从线上库导出 cn/hk 订阅（大陆侧发布用）。

用途：GitHub Actions 的 runner 在境外，探活大陆代理的命中率很低。
把 NAS 上常驻实例的库做一次快照，用大陆出口的探活结果覆盖仓库里的
proxies-cn.txt / proxies-hk.txt / clash-cn.yaml / clash-hk.yaml。

只读快照，不会和常驻服务抢 SQLite 写锁。

    python scripts/publish_region.py -c config.yaml --out /root/proxygate-publish/public
"""
from __future__ import annotations

import argparse
import logging
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import load_config          # noqa: E402
from app.export import export_all           # noqa: E402
from app.store import Store                 # noqa: E402


def snapshot(src_db: str) -> str:
    """sqlite3 backup API 做一致性快照，避免读到 WAL 中间态。"""
    tmpdir = tempfile.mkdtemp(prefix="proxygate-pub-")
    dst = os.path.join(tmpdir, "snapshot.db")
    src = sqlite3.connect(src_db, timeout=20)
    out = sqlite3.connect(dst)
    try:
        with out:
            src.backup(out)
    finally:
        out.close()
        src.close()
    return dst


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("-c", "--config", default="config.yaml")
    ap.add_argument("--out", required=True, help="导出目录（仓库里的 public/）")
    ap.add_argument("--db", default=None, help="默认取 config 里的 db")
    ap.add_argument("--fmt", default="hostport", choices=["hostport", "scheme"])
    ap.add_argument("--limit", type=int, default=20000)
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    cfg = load_config(args.config)
    db = args.db or cfg.db
    snap = snapshot(db)
    try:
        store = Store(snap)
        counts = export_all(cfg, store, args.out, args.limit, args.fmt, scope="region")
    finally:
        try:
            os.unlink(snap)
            os.rmdir(os.path.dirname(snap))
        except OSError:
            pass

    print("EXPORT " + " ".join(f"{k}={v}" for k, v in counts.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
