# ProxyGate v2.0.0

免费代理池：多源定时采集 → 异步并发探活 → IP 地理归属判定 → 按 **中国大陆 / 香港** 两条线输出 Clash 订阅、纯文本列表和 JSON API。只保留 **SOCKS5 / HTTPS** 两类协议，延迟超过 3 秒的节点直接淘汰。

支持两种跑法：自己服务器上常驻（带 HTTP 订阅端点），或交给 GitHub Actions 每 30 分钟跑一轮、把结果提交回仓库（无服务器，直接吃 raw 文件）。

> 仅采集互联网上公开的免费代理列表，不提供任何付费代理服务。

## 架构

```
APScheduler
  ├─ 采集   每 15 分钟   16 个源并行 → 解析 → 协议过滤 → SQLite
  ├─ 保活   每 5 分钟    存活 + 新增节点；socks5 原生握手探活
  ├─ 全量   每 1 小时    含已死节点，连续失败 10 次删除
  ├─ 区域   探活后       存活 IP 批量查 ip-api，写 cn / hk / other
  └─ 清理   每天 04:20   删除 48 小时未采集到且已死的行
                                    ↓
                     FastAPI :8080（token 鉴权）
```

## 端点

| 端点 | 说明 |
|---|---|
| `GET /sub/clash/cn` | 大陆节点 Clash 订阅 |
| `GET /sub/clash/hk` | 香港节点 Clash 订阅 |
| `GET /sub/clash/all` | 全部区域（旧路径 `/sub/clash` 等价） |
| `GET /sub/txt/cn` `hk` `all` | **纯文本，一行一个代理**（`fmt=hostport\|scheme`） |
| `GET /sub/raw/cn` `hk` | 带注释的文本（区域/国家/延迟/标签） |
| `GET /sub/v2ray/cn` `hk` | base64 编码列表 |
| `GET /api/list/cn` `hk` | JSON，支持 `type` `label` `country` `min_latency` |
| `GET /api/stats` | 存活、按区域/协议/来源统计 |
| `POST /api/update` | 手动触发采集 + 探活 + 区域判定 |
| `GET /healthz` | 健康检查（免鉴权） |

鉴权：`?token=xxx`、`X-Token` 或 `Authorization: Bearer xxx`。

## 纯文本输出

`/sub/txt/{region}` 只返回代理本体：没有注释、没有空行、结尾带换行，`grep`/`awk`/`xargs` 直接可用。

```bash
# 默认 ip:port —— curl -x、扫描器、浏览器扩展都能直接吃
curl -s "http://NAS_IP:8080/sub/txt/hk?token=xxx"
1.2.3.4:1080
5.6.7.8:3128

# fmt=scheme —— 带上协议，避免把 socks5 当 http 用
curl -s "http://NAS_IP:8080/sub/txt/hk?token=xxx&fmt=scheme"
socks5://1.2.3.4:1080
https://5.6.7.8:3128
```

支持的查询参数：`type=socks5|https|all`、`label=cn|foreign`、`min_latency`（毫秒）、`fmt=hostport|scheme`、`limit`。

## GitHub Actions 模式

`.github/workflows/update.yml` 每 30 分钟（`*/30 * * * *`，UTC）跑一轮完整流程，把存活代理写进 `public/` 并提交回仓库。公开仓库的 Actions 不计分钟数，可以长期挂着。

| 文件 | 内容 |
|---|---|
| `public/proxies.txt` | 全部存活，一行一个 `ip:port` |
| `public/proxies-cn.txt` / `proxies-hk.txt` | 按区域切分 |
| `public/proxies-socks5.txt` / `proxies-https.txt` | 按协议切分 |
| `public/clash.yaml` / `clash-cn.yaml` / `clash-hk.yaml` | Clash 订阅，可直接当订阅地址 |
| `public/meta.json` | 生成时间与各文件条数 |

直接用 raw 地址，不需要自己搭服务：

```
https://raw.githubusercontent.com/zowshili/proxygate/main/public/proxies.txt
https://raw.githubusercontent.com/zowshili/proxygate/main/public/proxies-hk.txt
https://raw.githubusercontent.com/zowshili/proxygate/main/public/clash-hk.yaml
```

手动触发：仓库 Actions 页 → *Update proxy lists* → *Run workflow*，可选 `hostport` / `scheme`。

两个注意点：

1. GitHub 会在仓库连续 60 天无**用户**活动后停用定时任务，机器人用 `GITHUB_TOKEN` 的提交不算活动。加一个 scope 为 `contents:write` 的 fine-grained PAT 到 `secrets.PROXYGATE_PAT`，工作流会自动优先用它推送，定时任务就不会被停。
2. 定时任务在高峰期可能延后几分钟，属于 GitHub 调度行为，不是脚本卡住。

本地也能复现同一套流程：

```bash
python main.py --once -c config.action.yaml --out public --fmt hostport
```

`--once` 跑完即退出：采集 → 探活（排空未检队列）→ 区域判定 → 导出文件，不监听端口。

## 快速开始（常驻服务）

```bash
python3 -m venv .venv
.venv/bin/pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt
# 改 config.yaml：web.token（必改）
nohup .venv/bin/python main.py > /tmp/proxypool.log 2>&1 & disown

curl http://127.0.0.1:8080/healthz
curl "http://127.0.0.1:8080/api/stats?token=xxx"
```

systemd 常驻：

```bash
sudo cp deploy/proxypool.service /etc/systemd/system/proxypool.service
sudo systemctl daemon-reload && sudo systemctl enable --now proxypool
journalctl -u proxypool -f
```

首次启动会立即执行一轮采集 + 探活，约 1–3 分钟后订阅才陆续有节点。

Clash 分组固定 11 组，刷新订阅不会冲掉你的选择：`PROXY`、`AUTO-Foreign`、`AUTO-CN`、`HTTPS-All/Fast/Medium/Slow`、`SOCKS5-All/Fast/Medium/Slow`。

## 区域判定

- 源配置里的 `region` 只是初始值；存活代理的 IP 通过 `ip-api.com/batch` 批量反查（100 个/批，批间隔 4.5s，缓存 7 天）。
- `CN → cn`，`HK → hk`，其余 `other`；只对存活节点查询，每轮几十到几百个，命中缓存后零请求。
- 外网不可达时保留源标签，订阅不中断；代价是区域划分精度下降。

## 代理源

| 类别 | 源 |
|---|---|
| 大陆 | 快代理、齐云代理、站大爷、geonode(CN)、scdn(CN)、proxifly(CN) |
| 香港 | geonode(HK)、scdn(HK)、proxifly(HK) |
| 全球（geo 判定） | monosans、TheSpeedX、proxifly、hideip.me、francevpn |

GitHub 源统一走 `gh-proxy.com` 镜像。某源持续抓 0 条时，在 `config.yaml` 的 `fetch.sources` 里注释掉即可，探活会自动过滤失效/假冒代理。

## v2.0.0 改动

1. 删除 socks4 → socks5 网关（含 30000-31999 端口段）：socks4 早已不采集，网关长期空转且是无认证开放中继。
2. 新增区域维度与 `/sub/*/{cn,hk}` 双端点。
3. 新增纯文本输出 `/sub/txt/{region}` 与 `--once` 导出（`public/`）。
4. socks5 探活改为原生 asyncio 握手，去掉每代理一个 `aiohttp_socks` 会话，实测吞吐约 2.5 倍。
5. 入库不再把已有节点重置为"未检"，避免每 15 分钟把上万条代理全部回炉。
6. SQLite 写操作移出事件循环，订阅响应 60 秒内存缓存，新增 `(alive,last_check)`、`(region,alive,latency_ms)` 索引。
7. 修复 `zip(sources, tasks)` 在源 type 未注册时的结果错位；token 改为定长时间比较；关闭 `/docs`。

## 注意

- `web.host: 0.0.0.0` 表示整个局域网都能拉订阅，token 必须足够强。
- 免费代理存活率通常 5%–15%，订阅里节点数量少属正常；延迟上限可在 `check.max_alive_latency_ms` 调整。
- 修改 `regions` 时需同步调整端点逻辑：目前只支持 `cn` / `hk` 两个区域。

## License

MIT
