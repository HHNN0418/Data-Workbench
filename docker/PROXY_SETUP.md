# Docker 代理采集

直连和两个代理均可承担公开采集。Docker worker 领取 Feed 与互动任务；作者资料、关联补采和新闻由后端执行任务逻辑，在直连和健康的 Gluetun 出口之间轮转。Chrome 仅采集本队总分与活动流水。两个 Gluetun 实例均选择澳大利亚：A 固定 `au1012.nordvpn.com`（悉尼），B 固定 `au753.nordvpn.com`（墨尔本），使用 OpenVPN UDP 默认端口；每个 worker 固定使用自己的代理。节点来自 2026-10-19 通过 Gluetun 官方更新器下载的实时 NordVPN 清单。旧内置目录的 au529/au569 已不在实时列表，不能只依据镜像内置目录选择。两实例使用各自的持久目录，每480小时更新节点资料。所有 worker 向 FastAPI 领取同一队列中的任务并申请同一个限流许可，代理数量不增加请求预算。

2026-10-19 实测两个代理出口分别为 `187.15.118.189`、`144.48.37.18`，Cloudflare trace 均返回 `AU`；出口可能随重新连接变化，以控制API实时值为准。TCP443曾被服务器重置，官方单独ovpn文件列出的自定义端口不被Gluetun内置Nord provider支持，最终使用已验证的UDP默认配置。

## 启动

先将 NordVPN **服务凭据**分别存入 `data/secrets/nord_username` 与 `data/secrets/nord_password`（不是网页登录密码）。后端与容器共享 `data/secrets/worker_token`；不要将这些文件提交或输出到日志。

后端继续监听 `127.0.0.1:8765`。当前 Docker Desktop 环境已验证容器可通过 `host.docker.internal:8765` 访问，无需改为局域网监听。只有三个 `/api/worker/*` 路由接受 Docker 的 Bearer token，普通页面/API继续限制本地访问。

```powershell
docker compose -p ctn -f compose.yaml -f compose.proxy.yaml config --quiet
docker compose -p ctn -f compose.yaml -f compose.proxy.yaml up -d --build
docker compose -p ctn -f compose.yaml -f compose.proxy.yaml ps
```

VPN 健康后 worker 才启动。后端断线时指数退避；VPN断线时请求失败并延期，不改用直连。待提交结果保存在独立 Docker volume；重试使用原始观测时间和 ID，不重复抓取来冒充原结果。

## 重启与检查

`restart: unless-stopped` 在 Docker 引擎重启后恢复容器。`Watch-CTN.ps1` 随 Windows 用户登录运行，每15秒检查 Docker、PostgreSQL和后端；存在 `data/proxy-enabled` 时还会恢复代理容器。VPN启动等待独立执行，不阻塞后端恢复。此配置从用户登录后生效，不保证未登录时启动。

watch会重新启动启用状态下被手动停止的代理；需停用自动代理时先移除 `data/proxy-enabled`，再停止对应容器。不要运行 `docker compose down -v`，该命令会删除数据库及worker持久卷。

本机调试代理分别为 `http://127.0.0.1:18881` 和 `http://127.0.0.1:18882`，不向局域网开放。控制接口只绑定本机 `127.0.0.1:18001/18002`，通过 `data/secrets/gluetun-control.toml` 中的 API key 授权，角色仅允许 `GET /v1/vpn/status` 与 `GET /v1/publicip/ip`。不允许切换节点、停 VPN 或读取含凭据的 VPN 设置。统一调度通过 CTN 后端完成。

`python -m ctn_data.proxy_status` 读取两个控制接口，输出连接状态及实际 IP，不输出控制凭据；后端可调用 `get_proxy_status()`（10秒缓存）。控制API状态与容器进程健康不同：只有真实公共IP可用后才能确认出口不同。

已验证双澳洲不同出口、两个worker真实互动入库、单worker重启后继续入库，以及控制API只读鉴权。验收记录见 `reports/generated/docker-proxy-verification-20261019.json`。帖子与互动由后端及Docker worker采集；浏览器脚本仅同步本队总分和活动流水，不采集Feed和互动。浏览器代理扩展不再使用。

## 官方资料

- [Gluetun NordVPN](https://github.com/qdm12/gluetun-wiki/blob/main/setup/providers/nordvpn.md)
- [Gluetun SecretFile 配置](https://github.com/qdm12/gluetun-wiki/blob/main/setup/advanced/docker-secrets.md)
- [Gluetun HTTP 代理](https://github.com/qdm12/gluetun-wiki/blob/main/setup/options/http-proxy.md)
- [Gluetun 控制服务器与鉴权](https://github.com/qdm12/gluetun-wiki/blob/main/setup/advanced/control-server.md)
