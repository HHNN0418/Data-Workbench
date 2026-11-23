# 通过 Cloudflare Tunnel 访问 CTN

使用固定公网域名，将流量转发到独立的本机桥接，再访问已有 CTN 服务：

```text
https://ctn.example.com
    → Cloudflare Tunnel / cloudflared Windows 服务
    → 127.0.0.1:8766（公网桥接）
    → 127.0.0.1:8765（现有 CTN 服务）
```

桥接和登录自启任务独立于原有服务及采集进程，不会启动第二个后端或重复采集。后端更新在其服务生效后，桥接会继续转发到该服务。

## 1. 准备固定域名及隧道

将域名 `example.com` 接入 Cloudflare，使用固定公网地址 `https://ctn.example.com`。在 Cloudflare 控制台创建 Cloudflare Tunnel，选择 Windows，按页面给出的安装命令将 `cloudflared` 安装为 Windows 服务，并等待隧道连接成功。

安装命令中的隧道 Token 只在本机执行，不需要发送到聊天或保存到项目。`cloudflared` 由 Cloudflare 的安装步骤管理，本项目脚本不会安装、重启或修改它。

在隧道中添加 Published application / 公网应用路由：

| 设置 | 值 |
|---|---|
| 公网域名 | `ctn.example.com` |
| Service URL | `http://127.0.0.1:8766` |
| Path | 留空，覆盖整个站点 |
| HTTP Host Header / Host 改写 | 保持默认，不设置覆盖 |

公网路由需要指向桥接端口 `8766`。桥接使用原始公网 Host 和精确 HTTPS Origin 校验请求，之后以本机身份转发到已有后端。

官方步骤：[创建远程管理隧道](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/get-started/create-remote-tunnel/)。

## 2. 先验证桥接

先确认本机可以打开 `http://127.0.0.1:8765`。已有 CTN 服务正在运行时，无需再次执行原有启动脚本。

新开一个 PowerShell，进入项目目录后执行：

```powershell
.\Start-CTN-Tunnel.ps1 -PublicUrl 'https://ctn.example.com'
```

保持窗口运行，然后在浏览器中访问 `https://ctn.example.com`，使用项目原有账号登录。原有登录、会话和 CSRF 校验继续生效。公网用户经桥接转发后共用后端的登录失败限流额度，多人反复输错密码会相互影响。

`PublicUrl` 必须是具体的 HTTPS DNS 域名，不接受通配符、IP 地址、用户名密码、路径、查询参数或片段。默认桥接端口为 `8766`，后端端口为 `8765`；可通过 `-Port` 和 `-BackendPort` 修改，Cloudflare 路由中的目标端口也必须同步修改。

在此窗口按 `Ctrl+C` 只会停止桥接，不会停止 CTN 后端或采集进程。

## 3. 保存配置并启用登录后自动恢复

验证通过后，在手动桥接窗口按 `Ctrl+C` 释放 `8766`，再在项目目录执行一次：

```powershell
.\Install-CTN-TunnelAutostart.ps1 -PublicUrl 'https://ctn.example.com'
```

此脚本保存公网域名及端口到已被 Git 忽略的 `data/tunnel-config.json`，为当前 Windows 用户注册独立任务 `CTN-TunnelAutostart`，并立即启动桥接守护进程。任务使用当前用户的普通权限，不保存 Windows 密码，也不修改原有 CTN 自启任务。

守护进程等待现有后端可用，再启动自己的桥接；桥接退出时会重试。若端口已被其他程序占用，它会记录日志并等待，不会终止或接管占用进程。修改域名时重新执行安装命令，守护进程会读取新配置并重启自己启动的桥接；Cloudflare 的公网路由也需要相应更新。

配置保存后，临时手动启动桥接也可以直接运行：

```powershell
.\Start-CTN-Tunnel.ps1
```

使用手动启动前，先确保自启任务的桥接没有占用同一端口。

## 重启后的行为

域名和配置会保留。电脑重启后，`cloudflared` 由 Windows 服务恢复；CTN 后端按原有自启设置恢复，桥接则在同一 Windows 用户登录后由 `CTN-TunnelAutostart` 恢复。需要登录 Windows，不能仅开机停留在登录界面。原有 CTN 后端自启任务也需要保持启用。

电脑关机、休眠或断网期间，公网访问会中断。

## 排查

- 查看 `data/logs/tunnel-watch.log`：守护进程状态、后端等待、端口冲突与重试记录。
- 查看 `data/logs/tunnel-bridge-error.log` 和 `tunnel-bridge.log`：最近一次桥接进程的输出。
- 后端连接失败：确认 `http://127.0.0.1:8765` 可访问，或检查配置中的后端端口；桥接不会代替原有任务启动后端。
- 域名或来源被拒绝：确保浏览器域名、`PublicUrl` 与 Cloudflare 公网路由一致，且未覆盖 HTTP Host Header。
- 隧道已连接但网站打不开：确认 Cloudflare 的目标是 `http://127.0.0.1:8766`，并确认桥接正在运行。
