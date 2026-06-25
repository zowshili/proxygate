# ProxyPool 部署指南（Debian 服务端）

## 架构

```
[服务器]                          [你的设备 Clash Verge]
proxypool 服务 (systemd 常驻)          订阅 URL:
  - 采集器: 15±1 分拉 8 个代理源        http://<服务端_IP>:8080
  - 探活器: 5 分保活/30 分全量           /sub/clash?token=<TOKEN>
  - 网关: socks4→socks5 桥接端口        ↓ 每 1 小时自动刷新
  - Web: 8080 提供订阅                  拿到最新存活节点(按协议×延迟分档)
```

## 一、首次部署

### 0. 前提
你已经在当前开发机上跑通了整个项目，现在把项目复制到 服务端 上。

如果项目直接就在 服务端 上（比如在 服务端 上编辑的），跳到第 1.5 步装依赖。

### 1. 复制项目到 服务端（从开发机到 服务端）
```bash
# 在 开发机 上执行，把项目 scp 到 服务端
scp -r /home/zowshili/桌面/工作目录/proxypool user@服务端_ip:~/proxypool/
```

### 1.5 创建虚拟环境并装依赖（在 服务端 上执行）
```bash
cd ~/proxypool
python3 -m venv .venv
.venv/bin/pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt
```

### 2. 修改配置（必做）

```bash
cd ~/proxypool
# 生成 32 位随机 token
python3 -c "import secrets; print(secrets.token_urlsafe(24))"
# 输出类似: Fx3h7gJk9mNq2pR5sT8vWxYz4aB6cDe
```

编辑 `config.yaml`，改这三项：

```yaml
web:
  host: "0.0.0.0"                       # 局域网可访问（不要改）
  token: "Fx3h7gJk9mNq2pR5sT8vWxYz4aB6cDe"   # ← 换成你刚生成的 token

clash:
  gateway_host: "192.168.1.100"          # ← 改成 服务端 的局域网 IP
```

### 3. 安装 systemd 服务

```bash
# 允许用户服务在未登录时也运行（开机自启关键）
loginctl enable-linger $USER

# 拷贝 service 文件
mkdir -p ~/.config/systemd/user
cp ~/proxypool/deploy/proxypool.service ~/.config/systemd/user/

# 重载 + 启用 + 启动
systemctl --user daemon-reload
systemctl --user enable --now proxypool
```

### 4. 确认服务启动

```bash
# 看状态（刚启动时 Status 会显示 active (running)）
systemctl --user status proxypool

# 看实时日志（首次会自动采集+探活，2-4 分钟后有存活节点）
journalctl --user -u proxypool -f
# 看到 "check batch done: XXX/5879 alive" 说明首次探活完成
# 看到 "gateway updated: XXX bridges active" 说明 socks4 网关起来了
```

### 5. 开放防火墙端口

```bash
# 8080: Web 订阅端口
# 30000-31999: socks4 网关桥接端口（局域网 Clash 连接用）
sudo ufw allow 8080/tcp
sudo ufw allow 30000:31999/tcp
```

**安全建议**：网关监听 0.0.0.0 无认证，建议限制 30000-31999 段只允许你的设备 IP：
```bash
# 例: 只允许 192.168.1.200（你的 Clash 设备）
sudo ufw deny 30000:31999/tcp
sudo ufw allow from 192.168.1.200 to any port 30000:31999 proto tcp
```

### 6. 验证服务

```bash
# 健康检查（在 服务端 上执行）
curl http://127.0.0.1:8080/healthz
# 期望: {"ok":true}

# 看统计（等首次探活完，alive > 0 说明成功）
curl "http://127.0.0.1:8080/api/stats?token=你的token" | python3 -m json.tool

# 查看网关状态
curl "http://127.0.0.1:8080/api/gateway/status?token=你的token"
# 期望 active_bridges > 0
```

## 二、Clash Verge 配置

### 1. 添加订阅（关键步骤）

打开 Clash Verge → 订阅 → 添加：

```
名称: ProxyPool
URL: http://192.168.1.100:8080/sub/clash?token=你的token
```

> 把 `192.168.1.100` 换成 服务端 的实际局域网 IP。

### 2. 设置自动刷新（非常重要）

添加订阅后，Clash Verge 默认**不会自动拉**更新。必须手动设置：
- 点开这个订阅的设置
- 开启「**自动更新**」（开关）
- 更新间隔设为 `1` 小时

### 3. 首次导入手动刷新

添加后点一下「更新」按钮立即拉取。

### 4. 确认分组

导入成功后，Clash 代理组应该能看到这些 19 个分组：

```
PROXY           (主选择)
AUTO-Foreign    (自动测速-外网)
AUTO-CN         (自动测速-国内)
HTTP-All        (HTTP 代理/目前为空骨架)
  HTTP-Fast     (延迟<1s)
  HTTP-Medium   (1~4s)
  HTTP-Slow     (>4s)
HTTPS-All       (HTTPS CONNECT 代理)
  HTTPS-Fast/Medium/Slow
SOCKS5-All      (原生 SOCKS5 代理)
  SOCKS5-Fast/Medium/Slow
SOCKS4-All      (经网关桥接的 SOCKS4 代理)
  SOCKS4-Fast/Medium/Slow
```

**使用方式**：在 PROXY 组里选 e.g. `SOCKS5-Fast` 只用低延迟节点，或 `AUTO-Foreign` 自动测速。

## 三、日常运维

| 操作 | 命令 |
|---|---|
| 查看状态 | `systemctl --user status proxypool` |
| 实时日志 | `journalctl --user -u proxypool -f` |
| 最近 100 行日志 | `journalctl --user -u proxypool -n 100` |
| 重启服务 | `systemctl --user restart proxypool` |
| 停止服务 | `systemctl --user stop proxypool` |
| 手动触发更新 | `curl -X POST "http://127.0.0.1:8080/api/update?token=你的token"` |
| 查看代理统计 | `curl "http://127.0.0.1:8080/api/stats?token=你的token"` |
| 查看网关状态 | `curl "http://127.0.0.1:8080/api/gateway/status?token=你的token"` |

## 四、技术说明

### 调度周期
| 环节 | 间隔 | 说明 |
|---|---|---|
| 采集（拉取代理源） | 15 ± 1 分钟 | 8 个源并行 |
| 新代理/保活探活 | 5 分钟 | 已存活 + 未检过的 |
| 全量探活 | 30 分钟 | 含已标记死亡的，连续失败 10 次剔除 |
| Clash 拉订阅 | 1 小时 | 取决于 Clash 端自动更新设置 |

### 代理协议处理
- **HTTP 代理**：探活时用 CONNECT 隧道验证，必须能通 `https://www.baidu.com` 才算存活。免费站基本不支持，所以目前为骨架空组
- **HTTPS 代理**：66daili 标的"HTTPS"实际是 HTTP CONNECT 代理，Clash 里 `type: http`，目前少量存活
- **SOCKS5 代理**：直接作为 `type: socks5` 节点
- **SOCKS4 代理**：Clash 不支持 socks4 出站，脚本在本地开 SOCKS5 桥接端口转发。Clash 节点为 `type: socks5, server: 服务端_IP, port: 30000+`

### 订阅分组（19 组）
```
PROXY (主选择) → 包含 AUTO-Foreign/AUTO-CN + 4 个协议 All 组
├── AUTO-Foreign (url-test, 可通外网节点)
├── AUTO-CN     (url-test, 可通百度节点)
├── HTTP-All → HTTP-Fast / HTTP-Medium / HTTP-Slow
├── HTTPS-All → HTTPS-Fast / HTTPS-Medium / HTTPS-Slow
├── SOCKS5-All → SOCKS5-Fast / SOCKS5-Medium / SOCKS5-Slow
└── SOCKS4-All → SOCKS4-Fast / SOCKS4-Medium / SOCKS4-Slow
```

### 节点名称格式
```
001|s5|CN|1.2.3.4:1080|cn-foreign|1.2s
``` 
每段含义：序号 | 协议标签(s5/s4/http/https) | 国家 | IP:端口 | 可用标签 | 延迟

## 五、故障排查

### 订阅为空（0 节点）
1. 看日志：`journalctl --user -u proxypool -f`
2. 首次启动需 2-4 分钟采集+探活，等一会
3. 手动触发：`curl -X POST "http://127.0.0.1:8080/api/update?token=..."`
4. 检查统计：`curl "http://127.0.0.1:8080/api/stats?token=..."`，看 `alive` 数量

### Clash 连不上 socks4 网关节点
1. 检查 `clash.gateway_host` 配置是否是 服务端 局域网 IP（不是 127.0.0.1）
2. 检查防火墙是否开放 30000-31999
3. 查网关状态：`curl "http://127.0.0.1:8080/api/gateway/status?token=..."`，看 `active_bridges`

### 探活存活率低
- 免费代理存活率约 5-15%（测试已确认），属正常
- 如果 socks4 全死（0 存活），检查 `config.yaml` 的 `allowed_protocols` 是否包含 `"socks4"`
- 用 curl 对照验证：`curl --socks4 <ip>:<port> http://www.baidu.com`

### 手动改源
代理站经常变动。某源持续抓 0 个的话，在 `config.yaml` 的 `fetch.sources` 里增删即可。type 支持：
`89ip` / `kuaidaili` / `ip3366` / `qiyunip` / `daili66` / `francevpn` / `text` / `zdaye`
