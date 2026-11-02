# ProxyGate

免费代理采集池：定时抓取多个公开免费代理源，异步并发探活（CN / 外网双标签），输出 Clash 订阅与 JSON API。仅保留 **SOCKS5 / HTTPS** 两类协议，面向中国大陆可用性优化。

> 本项目采集的均为互联网上公开的免费代理列表，不提供任何付费代理服务。

## 特点

- 自动采集：多源并行抓取，约 15 分钟一轮，含 GitHub 聚合源与国内站点
- 异步探活：SOCKS5 走原生隧道、HTTPS 走 CONNECT 隧道验证，自动剔除死代理
- 高延迟淘汰：超过延迟上限（默认 3 秒）的节点自动判死，不入订阅
- Clash 订阅：按协议 × 延迟自动分组
- JSON API：支持协议 / 标签 / 国家过滤
- 轻量：Python 3.10+，SQLite 存储

## 快速开始

```bash
git clone https://github.com/你的用户名/proxypool.git
cd proxypool
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

编辑 `config.yaml`：

```yaml
web:
  host: "0.0.0.0"                  # 仅本机用 127.0.0.1
  port: 8080
  token: "换成强随机字符串"          # 必改

clash:
  gateway_host: "127.0.0.1"        # Clash 在其他设备时改为本机局域网 IP
```

生成随机 token：`python3 -c "import secrets; print(secrets.token_urlsafe(24))"`

运行：

```bash
nohup .venv/bin/python main.py > /tmp/proxypool.log 2>&1 & disown
```

验证：

```bash
curl http://127.0.0.1:8080/healthz
curl "http://127.0.0.1:8080/api/stats?token=你的token"
```

## 订阅与 API

所有端点需 `?token=xxx` 鉴权（或 `X-Token` / `Authorization: Bearer`）。

| 端点 | 说明 |
|---|---|
| `GET /sub/clash` | Clash YAML 订阅，可带 `type` / `label` / `min_latency` / `limit` 参数 |
| `GET /sub/raw` | 纯文本列表 |
| `GET /api/list` | JSON 列表，支持 `type` / `label` / `country` / `min_latency` 过滤 |
| `GET /api/stats` | 统计 |
| `POST /api/update` | 手动触发采集 |

示例：`/sub/clash?token=x&type=socks5&label=cn&min_latency=3000`

## 代理源

| 源 | 类型 | 协议 |
|---|---|---|
| kuaidaili（快代理） | HTML | https |
| qiyunip | HTML | https / socks5 |
| zdaye（站大爷） | HTML | https / socks5 |
| seofangfa | HTML | https / socks5 |
| francevpn (GitHub Pages) | HTML | socks5 |
| geonode API | JSON | https / socks5（country=CN） |
| proxy.scdn.io API | JSON | https / socks5（CN） |
| TheSpeedX / monosans | 文本 | socks5 |

某源持续抓取 0 条时，在 `config.yaml` 的 `fetch.sources` 中注释或替换即可；探活会自动过滤抓到的失效/假冒代理与延迟超过上限的节点。

## 免责声明

1. **本项目仅供学习、研究与技术交流，请在下载后 24 小时内自行评估是否保留。**
2. **关于采集行为**：本项目仅对互联网上完全公开、无需登录即可访问的页面进行只读抓取，不破解验证、不绕过反爬或访问控制、不抓取任何非公开数据。
3. **关于用户行为**：本项目仅提供代理采集与维护工具，不内置、不推荐、不指向任何具体用途。用户下载、部署及使用本项目所实施的一切行为及其产生的一切后果，均由用户自行承担，与本项目作者及贡献者无关。
4. 用户应遵守所在国家/地区的法律法规；因使用免费公开代理导致的隐私泄露、数据丢失、账号风险等后果由用户自行承担。
5. 本项目按「现状」提供，不承诺任何可用性、稳定性或准确性。作者不对任何直接或间接损失承担责任。
6. 一旦复制、下载或运行本项目，即视为你已阅读并同意以上全部条款。

## License

MIT
