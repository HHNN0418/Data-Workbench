# CTN 系统 · 迁移部署与运维指南

本指南配套「全量转移包」，用于把整套 CTN（Capture the Narrative）系统从原机器迁到另一台计算机并长期运行、运维。

> 适用快照：2026-10-25（Window 45 + 火箭模式开关）。原机已按用户要求删除 `.git` 和各类备份，本包内的代码即当时线上运行的代码。

---

## 1. 这是什么 / 架构总览

一套本地运行的竞赛运营系统：公开只读采集 + PostgreSQL 历史库 + 积分对账 + 分析看板 + 一批「角色 Agent」在赛事社交平台按人设阅读、生成并发帖/回复。

```
React 19 + TS + Vite 8  →(构建为静态 dist)
        │ 同源
FastAPI + Uvicorn (Python 3.12)  ──  PostgreSQL 17（Docker 持久卷）
        │                                    ▲
        ├─ 采集器（匿名只读 GET，三通道：feed / 互动 / 资料）
        ├─ Agent 控制（人设、排班、生成、发布；状态存在 PG 的 meta 表）
        ├─ AI 调用（OpenRouter / OpenAI / Jev，密钥在 data/secrets/*.key）
        ├─ 风险/影子复核、Blue 举报、文案识别
        └─ 隧道代理（可选，对外暴露，ctn.example.com）
浏览器扩展（可选）：在已登录本队标签页做只读 GET，同步排行榜
```

- **后端**：`python -X utf8 -m ctn_data --db-secret-file data/secrets/postgres_url serve --port 8765 [--auto-collect]`，只绑 `127.0.0.1:8765`。
- **数据库**：Docker 容器 `ctn-postgres-1`（compose 项目名 `ctn`、服务 `postgres`，`postgres:17-alpine`，库/用户都叫 `ctn`，持久卷 `ctn_postgres`，映射 `127.0.0.1:55432→5432`）。
- **托管**：Windows 计划任务 `CTN-AutoStart` + `Watch-CTN.ps1` 守护进程，后端挂了自动拉起。
- **Agent 全部状态**（人设、启用开关、排班、草稿、事实库等）存在 PG `meta` 表的 `agent_control_v1:*` 键里，随数据库一起迁移。

---

## 2. 转移包清单

| 路径 | 内容 |
| --- | --- |
| `bundle/`（本目录） | 运行所需的全部代码：`ctn_data/`、`frontend/`（含已构建 `dist/`）、`scripts/`、`docker/`、根启动脚本、`compose.yaml`、`pyproject.toml`、`requirements.lock.txt`、`pnpm-lock.yaml`、`docs/` | 
| `../ctn-db.dump` | PostgreSQL 全量导出（自定义格式 `-Fc`，约 446MB，含所有帖子/角色状态/事实/榜单） |
| `bundle/secrets.enc` | 加密的密钥包（openssl AES-256-CBC + PBKDF2/20 万次），34 项 |
| `bundle/secrets.manifest.txt` | 密钥包内容清单（只列路径、无值） |
| `bundle/MIGRATION_GUIDE.md` | 本指南 |

**未打包、需在新机自行补**（用户指定）：AI 的 3 个 API key（OpenRouter / OpenAI / Jev）、`worker_token`、`tunnel_bridge_token`、VPN（NordVPN/gluetun）、仪表盘登录会话。

**secrets.enc 里有的 34 项**：`data/secrets/postgres_url`、`postgres_password`、`data/runtime-secrets/postgres_password`，以及 `data/secrets/agents/` 下 31 个角色平台登录 + `team.key`。

---

## 3. 新机前置依赖

- Windows（计划任务托管是 Windows 专用；非 Windows 见 §8）。
- **Docker Desktop**（跑 PostgreSQL）。
- **Python 3.12+**。
- **Node 22.12+ 与 pnpm 11**（仅当要重建前端；`dist/` 已在包里，不改前端可不装）。
- **openssl**（解密 secrets.enc；Git for Windows 自带）。
- 联网（首次装 Python/前端依赖时）。

---

## 4. 部署步骤

### 4.1 放代码
把 `bundle/` 的内容解包到目标目录，例如 `C:\Users\<you>\Desktop\CTN`。下文命令都在该目录的 PowerShell 里执行。

### 4.2 起 PostgreSQL 并恢复数据库
数据库密码来自 `data/runtime-secrets/postgres_password`（见 §4.3 先把密钥落盘，compose 用它做 docker secret）。

```powershell
docker compose up -d postgres
# 等 healthcheck 变 healthy（约 10–20 秒）
docker compose ps
```

恢复全量导出（容器名 `ctn-postgres-1`）：

```powershell
docker cp ..\ctn-db.dump ctn-postgres-1:/tmp/ctn-db.dump
docker exec ctn-postgres-1 pg_restore -U ctn -d ctn --clean --if-exists --no-owner /tmp/ctn-db.dump
docker exec ctn-postgres-1 rm -f /tmp/ctn-db.dump
```

> 恢复进空库时 `--clean --if-exists` 无害；重复恢复也安全。

### 4.3 解密并落盘密钥
解密（口令由协调单独给你，**不在本包里**）：

```powershell
openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 -in bundle\secrets.enc -out secrets.json -pass pass:<口令>
```

`secrets.json` 是 `{相对路径: 明文}` 字典。按每个键的路径把值写到对应文件，并用 DPAPI 加密保护（Windows 用项目自带的保护脚本）：

```powershell
# 先把明文写到各自路径（示例，用脚本遍历 secrets.json 落盘），再：
.\Protect-CTNSecrets.ps1    # 把明文密钥转成当前用户 DPAPI 保护格式
```

- `data/secrets/postgres_url`、`postgres_password`、`data/runtime-secrets/postgres_password`、`data/secrets/agents/*.key`（31 个 + `team.key`）都要落到原路径。
- `data/runtime-secrets/postgres_password` 是 compose 的 docker secret，必须先于 §4.2 存在；若你先跑了 compose，补好后 `docker compose up -d` 重建一次。
- 用完请删除桌面/本地的明文 `secrets.json`。

> 原机桌面上的 `ctn-secrets.json` 就是这份明文（34 项），可直接用来落盘，省去解密。

### 4.4 补未打包的密钥
- **AI key**：进工作台 → AI 配置，给 OpenRouter / OpenAI / Jev 三个服务重新填 API key（服务定义已在数据库里，只缺 key）。
- **worker_token / tunnel_bridge_token**：首次启动会按需重新生成；用隧道才需要 tunnel token。
- **VPN**：如需 NordVPN/gluetun，自行在 `data/runtime-secrets/` 补 `nord_username`/`nord_password`/`gluetun-control.toml`。

### 4.5 装依赖、构建前端
```powershell
.\Setup-CTN.ps1          # 建 .venv、pip 装 requirements.lock.txt、pnpm 装前端、tsc、vite build
# 只重建前端：.\Setup-CTN.ps1 -BuildOnly
```
`dist/` 已在包里，不改前端可跳过构建；但首次装 Python 依赖仍需 `.\Setup-CTN.ps1`（或手动建 venv 装 requirements.lock.txt）。

### 4.6 启动后端
```powershell
.\Install-Autostart.ps1  # 安装并启动 CTN-AutoStart 计划任务（托管后端，挂了自动拉起）
# 或仅本次前台启动：.\Start-CTN.ps1        （加 -NoAutoCollect 只看板不采集）
```
打开 http://127.0.0.1:8765/ 。使用本地配置的成员账号登录（详见 `docs/LOGIN.md`）。

### 4.7 隧道（可选，对外访问）
```powershell
.\Start-CTN-Tunnel.ps1   # 需要 tunnel_bridge_token
```

---

## 5. 日常运行与运维

- **健康检查**：`curl http://127.0.0.1:8765/api/health` → `ok=true`、`collection.active=true`、`stalled=false`。
- **火箭模式开关**（本次新增）：工作台 → AI 工作流 → Agent 管理，页面顶部「🚀 火箭模式」开关（在「持续运行」旁）。
  - **开**=快照当前状态后，全员启用 + 取消一切限速（排班覆盖成「无限制」）。
  - **关**=按快照还原（排班回默认、各角色/全局启用状态回到开之前）。
  - 后端接口：`POST /api/agents/rocket-mode {on: true|false}`（登录保护）；状态在 `settings.rocket_mode`。
  - 当前随包迁移的数据库里 `rocket_mode=true`、30 角色全开；想回常规排班点「关」即可。
- **持续运行总开关** + **每角色启用**：都在 Agent 管理页；两者都为真、且排班放行，角色才会自动跑。
- **排班**：默认排班写在 `ctn_data/agent_schedule.py` 的 `DEFAULT_SCHEDULE`（分 A–E 组、各有班次与每小时上限）；`settings.shift_schedule` 可覆盖，删除覆盖即回默认。
- **统一重载**（改代码后生效）：`scripts\Reload-CTN.ps1`，排空在途任务 → 停旧后端 → 计划任务拉起新进程 → 验证健康。约 1–2 分钟，期间短暂中断。
- **诊断**（登录后调）：`GET /api/diagnostics/cpu`、`GET /api/diagnostics/memory`（后者 `?trace=start` / `?trace=top` / `?trace=stop` 查内存增长来源）。
- **采集暂停/间隔**：运行状态页可暂停或设 Latest 间隔（30–60 秒）。
- **排行榜同步**：靠浏览器扩展（`browser-extension/`，v1.4.0）在已登录本队标签页每 10–15 分钟同源 GET 一次，交 `/api/browser-capture`；换机后需在 Chrome 里重新加载扩展并登录本队。

---

## 6. 关键设计点（便于排障）

- **Agent 状态全在 PG `meta` 表**（`agent_control_v1:agents`=人设+每角色 enabled；`...:settings`=全局设置含 shift_schedule / rocket_mode；还有 drafts、rate、leases、operations、events）。锁写用 `AgentStore.change`，读用 `store.read()`（每次从 DB 读最新）。
- **维护脚本禁止跑 initialize**：对线上库写操作的一次性脚本，要先把 `ctn_data.db.initialize` / `ctn_data.postgres.initialize_postgres` / `ctn_data.agent_control.initialize` 打桩成抛错，用 `AgentStore.open_existing`（不跑 DDL），避免维护进程重复建表。
- **写作流水**：决策 → 写作 → 方向/事实检查（不通过就重写/放弃）；公众人物职责用短标签、不照抄；原创给一条具体 Yes 理由；场所顾虑用 FACTS 里的安全保障回答。
- **风险/影子复核（D120）**：帖子 404 后第 5、20 分钟各复核一次，满 10 分钟仍 404 判定隐藏；20 分钟那次盯 10 分钟误判。
- **登录**：仅控制本地工作台访问，不向比赛平台提交；会话存 `data/auth/dashboard_sessions_*.sqlite3`（迁移不带，新机重新登录）。

---

## 7. 故障排查

- **PG `/dev/shm` DiskFull（55P03 之外的满盘）**：满负荷下积压计数的 Parallel Hash 共享哈希表可能超 64MB 的 `/dev/shm`。热修：`ALTER SYSTEM SET enable_parallel_hash=off;` + reload（已在本快照生效）。永久修：给容器 `shm_size: 1gb` 重建。
- **Agent 存储锁争用 / 执行失败**：已做 tick 查询共用、局部窗口共用、轻量心跳事务；满负荷下在等会话已大幅下降。若又出现大量等待，看 `/api/diagnostics/cpu` 和锁等待。
- **后端内存**：满负荷下约 +350–400MB/小时（疑似泄漏，未定位根因）；真正红线是系统「提交额度」。缓解=重启后端（内存归零），重载即可。用 `/api/diagnostics/memory?trace=start`→隔几小时 `?trace=top` 定位。
- **采集卡住**：PG 重启后采集线程可能断连；已做自动重连。`/api/health` 的 `collection.stalled` 为 true 或 `quiet_minutes` 很大时，重载后端。
- **角色被封**：帖子被举报/隐藏到阈值会被平台停权；调度器运行时自动跳过已停权账号。满负荷会提高封号风险。

---

## 8. 非 Windows 简述

计划任务托管、`Protect-CTNSecrets.ps1`（DPAPI）是 Windows 专用。非 Windows 上：
- 密钥用 `PRIVATE1\n` + 明文 的格式写到对应路径（`ctn_data/secret_files.py` 的 `read_secret` 认这个头）。
- 用进程管理器（systemd / supervisor）托管 `python -m ctn_data --db-secret-file data/secrets/postgres_url serve --port 8765 --auto-collect`。
- PostgreSQL 仍用 `docker compose up -d postgres` + §4.2 恢复。

---

## 9. 快速清单

1. 解包 `bundle/` → 目标目录。
2. 落盘 `data/runtime-secrets/postgres_password`（解密 secrets.enc 得到）。
3. `docker compose up -d postgres` → `pg_restore ... ctn-db.dump`。
4. 落盘其余密钥 + `Protect-CTNSecrets.ps1`；补 AI key / VPN。
5. `.\Setup-CTN.ps1`（装依赖 + 构建）。
6. `.\Install-Autostart.ps1` → 打开 http://127.0.0.1:8765/ 登录。
7. 需要的话点「火箭模式」开关；扩展重新加载同步排行榜。
8. 删除本地明文 `secrets.json` / `ctn-secrets.json`。
