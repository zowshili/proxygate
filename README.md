# ProxyGate — 代理采集与订阅框架

免费代理池。自动采集多个国内可达的免费代理源，异步并发探活检测（CN/外网双标签），提供 Clash 订阅和 JSON API。SOCKS4 自动桥接为 SOCKS5，HTTP 代理需通过 CONNECT 隧道验证

## 声明

> **本工具仅供学习与研究使用。使用者应遵守当地法律法规，不得将本工具用于非法用途，内置采集源为示例配置，不保证持续可用**
> 
> 本项目采集的均为互联网公开的免费代理列表，不提供任何付费代理服务。
> 免费代理存在不稳定、速度慢、可能泄露隐私等风险，请自行评估使用。
> 作者不对因使用本工具产生的任何问题承担责任。

## 特点

- **自动采集**：每 15 分钟从多个源爬取免费代理，总量 5000+
- **异步探活**：并发检测（CN/外网双标签），记录延迟，自动剔除死代理
- **协议全面**：HTTP / HTTPS / SOCKS5 / SOCKS4（HTTP 代理需通过 CONNECT 隧道检测才标记存活）
- **SOCKS4 桥接**：Clash 不支持 socks4 出站，项目内建 socks4→socks5 网关自动转换
- **Clash 订阅**：YAML 格式，按协议×延迟（Fast/Medium/Slow）分 19 组
- **JSON API**：对外工具友好接口，支持协议/延迟/国家过滤
- **轻量**：SQLite 存储，Python 3.10+，依赖少

## 架构

```
+-------------------------------------------------------------------+
|                           服务端                                  |
|  proxypool (systemd 常驻)                                         |
|  +--------+     +---------+     +----------+     +--------------+ |
|  | 采集器 | --> | SQLite  | --> |  探活器  | --> |  Web 服务    | |
|  | 15±1分|     |  数据   |     | 5分/30分 |     | :8080        | |
|  +--------+     +---------+     +----------+     +--------------+ |
|                                                                   |
|                                               +-----------+       |
|                                               | socks4网关|       |
|                                               | 30000+端口|       |
|                                               +-----------+       |
+-------------------------------------------------------------------+
        |
        | (订阅/API 主动拉取)
        v
+------------------+     +-----------------+
| Clash Verge      |     | 其他工具/脚本   |
| (YAML 订阅)      |     | (JSON API)      |
+------------------+     +-----------------+
```

## 快速开始

### 环境要求

- Python 3.10+
- Linux / macOS / Windows

### 安装

```bash
git clone https://github.com/你的用户名/proxypool.git
cd proxypool
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

### 配置

```bash
# 生成随机 token
python3 -c "import secrets; print(secrets.token_urlsafe(24))"
```

编辑 `config.yaml`：

```yaml
web:
  host: "0.0.0.0"
  token: "刚才生成的随机串"       # 必改！

clash:
  gateway_host: "127.0.0.1"     # Clash 在同一台机器时；在其他设备改服务端 IP
```

### 运行

```bash
nohup .venv/bin/python main.py > /tmp/proxypool.log 2>&1 & disown
```

或用 systemd（开机自启＋崩溃重启）：

```bash
sudo cp deploy/proxypool.service /etc/systemd/system/
sudo sed -i 's|%h|/home/你的用户名|g' /etc/systemd/system/proxypool.service
sudo systemctl daemon-reload && sudo systemctl enable --now proxypool
journalctl -u proxypool -f
```

### 防火墙放行

```bash
sudo ufw allow 8080/tcp              # Web 订阅
sudo ufw allow 30000:31999/tcp        # socks4 网关
```

### 验证

```bash
curl http://127.0.0.1:8080/healthz
# → {"ok":true}

curl "http://127.0.0.1:8080/api/stats?token=你的token"
# → alive > 0 说明运行正常
```

## Clash 订阅

所有端点均需 `?token=xxx` 鉴权（或 `X-Token` / `Authorization: Bearer` header）

| 端点                     |                                           说明 |
|:--------------------------|:------------------------------------------------|
| `GET /sub/clash?token=x` | Clash YAML 订阅（Profile-Update-Interval: 1h） |
| `GET /sub/v2ray?token=x` | base64 编码逐行 ip:port                        |
| `GET /sub/raw?token=x`   | 纯文本带注释                                   |

### Clash 订阅参数

```
/sub/clash?token=x&type=socks5&label=foreign&min_latency=3000&limit=200
```

| 参数          | 默认  | 说明                                     |
|:---------------|:-------|:------------------------------------------|
| `type`        | `all` | 协议过滤：http / https / socks4 / socks5 |
| `label`       | —    | 标签过滤：cn / foreign                   |
| `min_latency` | —    | 最大延迟（ms），例 3000 只取 3 秒以内的  |
| `limit`       | 2000  | 最大节点数                               |

### Clash 分组结构（19 组）

```
PROXY (select 主选择)
├── AUTO-Foreign (url-test: 可通外网的节点)
├── AUTO-CN     (url-test: 可通百度的节点)
│
├── HTTP-All (select)          ← HTTP 代理（需通过 CONNECT 检测）
│   ├── HTTP-Fast   (< 1s)
│   ├── HTTP-Medium (1~4s)
│   └── HTTP-Slow   (≥ 4s)
│
├── HTTPS-All                 ← 页面标记为 HTTPS 的 HTTP CONNECT 代理
│   ├── HTTPS-Fast / Medium / Slow
│
├── SOCKS5-All                ← 原生 SOCKS5 节点
│   ├── SOCKS5-Fast / Medium / Slow
│
└── SOCKS4-All                ← 经本地网关桥接的 SOCKS4 节点
    ├── SOCKS4-Fast / Medium / Slow  (server: 127.0.0.1, port: 30000+)
```

### Clash Verge 配置

1. 添加订阅：`http://你的IP:8080/sub/clash?token=你的token`
2. 开启「自动更新」，间隔 1 小时，建议用时手动更新确保时效
## JSON API

与 Clash 订阅共用 token，适合工具/脚本调用。

### 获取代理列表（实例）

```
GET /api/list?token=x&type=socks5&limit=10
```

**参数**：

| 参数          | 默认  | 说明                           |
|:---------------|:-------|:--------------------------------|
| `type`        | `all` | http / https / socks4 / socks5 |
| `label`       | —    | cn / foreign                   |
| `country`     | —    | 国家关键字过滤                 |
| `min_latency` | —    | 最大延迟 ms                    |
| `limit`       | 100   | 最大数量                       |

**返回示例**：

```json
{
  "success": true,
  "count": 3,
  "proxies": [
    {
      "ip": "1.2.3.4",
      "port": 1080,
      "protocol": "socks5",
      "country": "中国",
      "source": "66daili-socks5",
      "latency_ms": 120,
      "alive": 1,
      "cn_reachable": 1,
      "foreign_reachable": 1,
      "last_check": 1700000000
    }
  ]
}
```

### 统计

```bash
curl "http://127.0.0.1:8080/api/stats?token=x"
```

### 手动触发更新

```bash
curl -X POST "http://127.0.0.1:8080/api/update?token=x"
```

### 网关状态

```bash
curl "http://127.0.0.1:8080/api/gateway/status?token=x"
curl "http://127.0.0.1:8080/api/gateway/bridges?token=x"
```

## 代理源

当前配置的代理源（可在 `config.yaml` 自由增减）：

| 源 | 类型 | 协议 | 备注 |
|---|---|---|---|
| 89ip | HTML 表格 | http | 国内站 |
| kuaidaili | HTML 表格 | http | 快代理免费列表 |
| ip3366 | HTML 表格 | http | 国内站 |
| qiyunip | HTML 表格 | http / https | 国内站（类型列标注） |
| 66daili | HTML 卡片 | http / https / socks4 / socks5 | 质量最高 |
| francevpn | HTML 表格 | socks4 / socks5 | GitHub Pages |
| TheSpeedX | 纯文本 ip:port | socks4 / socks5 | GitHub 聚合（经国内镜像代理，需要服务端可访问 ghproxy.net） |
| proxy.scdn.io | JSON API | http / https / socks4 / socks5 | API 源（只取中国地区） |

> 代理站经常变动。某源持续抓 0 个时在 `config.yaml` 的 `fetch.sources` 里移除或替换即可。

## 探活说明

### 探活目标

| 标签 | URL | 用途 |
|---|---|---|
| cn | `http://www.baidu.com` | 检测国内可达性，作为 CN 标签 |
| foreign | `http://www.gstatic.com/generate_204` | 检测外网可达性，作为 Foreign 标签 |
| https_check | `https://www.baidu.com` | HTTP 代理专用：验证 CONNECT 隧道能力 |

### 存活判定

- **SOCKS4 / SOCKS5**：cn 或 foreign 任一通过即标记存活
- **HTTP 代理（http/https）**：必须通过 https_check（CONNECT 隧道），否则标记死亡
- **连续失败 10 次**：从数据库删除
- **连续失败 5 次**：标记 alive=0，不再出现在订阅中

### 调度周期

| 环节 | 间隔 | 说明 |
|---|---|---|
| 采集 | 15 ± 1 分 | 从源站拉取代理 |
| 保活探活 (check_new) | 5 分 | 刷新已存活 + 检测新入库代理 |
| 全量探活 (check_full) | 30 分 | 含已死亡代理，连续失败达阈值则删除 |
| Clash 拉订阅 | 取决于客户端设置 | 响应头 `Profile-Update-Interval: 1`（小时） |

## 配置参考

```yaml
web:
  host: "0.0.0.0"
  port: 8080
  token: "your-secret"

clash:
  gateway_host: "127.0.0.1"         # socks4 桥接节点地址

fetch:
  interval_seconds: 900              # 采集间隔（秒）
  jitter_seconds: 60                 # 随机抖动
  sources: [...]                     # 代理源清单

check:
  new_interval: 300                  # 保活间隔（秒）
  full_interval: 1800                # 全量探活间隔
  concurrency: 300                   # 异步并发数
  timeout: 8                         # 单次请求超时（秒）
  fail_threshold: 10                 # 连续失败阈值（删除）
  alive_fail_to_dead: 5             # 连续失败阈值（标记死亡）
  url_cn: "http://www.baidu.com"
  url_foreign: "http://www.gstatic.com/generate_204"
  url_https_check: "https://www.baidu.com"

gateway:
  listen_host: "0.0.0.0"
  port_start: 30000
  port_end: 31999

allowed_protocols: ["http", "https", "socks4", "socks5"]
```

## 故障排查

### 订阅为空（0 节点）
1. 首次启动需等待 2-4 分钟完成首次采集+探活
2. 检查统计：`curl "http://127.0.0.1:8080/api/stats?token=x"`，看 `alive` 数量
3. 手动触发：`curl -X POST "http://127.0.0.1:8080/api/update?token=x"`
4. 看日志：`journalctl -u proxypool -f`

### Clash 连不上 SOCKS4 节点
检查 `config.yaml` 里的 `clash.gateway_host`：
- Clash 与服务端同机器：`127.0.0.1`
- Clash 在其他设备：服务端的局域网 IP（如 `192.168.1.100`）

### 存活率低
- 免费代理池存活率通常 1-5%，属正常
- 可在 `fetch.sources` 增加新源
- 检查服务端能否正常访问探活目标 URL

### 某源持续 0 个代理
- 该源可能改版或下线，日志可见 `fetcher xxx got 0 proxies`
- 在 `config.yaml` 移除或替换即可

## 开发

```bash
# 虚拟环境
python3 -m venv .venv
source .venv/bin/activate

# 安装依赖
pip install -r requirements.txt

# 启动
python main.py
```

### 添加新代理源

1. 在 `app/fetcher/` 下创建 spider 文件，继承 `BaseFetcher` 并实现 `parse()`
2. 在 `app/fetcher/__init__.py` 的 `_SPIDERS` 字典中注册
3. 在 `config.yaml` 的 `fetch.sources` 中添加配置

## 免责声明

1. **本工具仅供学习与技术研究使用**
2. 本项目采集的均为互联网公开的免费代理，不代理任何商业服务
3. 使用者应遵守当地法律法规，不得将本工具用于非法用途
4. 免费代理存在不稳定、速度慢、可能泄露隐私等风险，请自行评估
5. 作者不对因使用本工具产生的任何直接或间接损失承担责任
6. 使用本工具即代表你同意以上条款

## License

MIT
