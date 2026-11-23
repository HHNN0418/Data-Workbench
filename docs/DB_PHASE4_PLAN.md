# 第四阶段：数据库调优方案（C，2026-10-22）

各步的 SQL 和回滚脚本都在 `scripts/db-phase4-*.sql`。下面的命令都在 Git Bash 里、`C:\Users\R2616\Desktop\CTN` 目录下执行，可以直接复制运行。

协调会话的决定（D32）：
- **C 步、B 步**：批准，由 C 执行（B 步要避开重载窗口）。
- **A 步**：批准，由协调会话在单独的窗口执行。
- **D 步**：不做。
- **E 步**：延后。
- **F 步**：不需要。

## 现状（只读实测，2026-10-22 10:1x 悉尼时间）

| 项目 | 数值 |
|---|---|
| WSL 虚拟机 | 32 GB 内存，16 核；PG 进程目前约占 0.9 GB |
| 数据库大小 | 1.86 GB；磁盘剩余 951 GB |
| `shared_buffers` | 128 MB（`postgresql.conf` 的默认值；`postgresql.auto.conf` 里只有文件头，没有设置项） |
| 表的缓存命中率 | **88.3%**：大部分读要走磁盘或操作系统缓存 |
| 容器 `/dev/shm` | 64 MB（Docker 默认值） |
| 扩展 | `pg_trgm` 1.6 和 `pg_stat_statements` 1.11 都可以装，目前都没装；`ctn` 是超级用户 |
| `meta` 表 | 33 MB，其中实际数据 3.5 MB；之前的 268 MB 膨胀已经消失 |
| `raw_records` | 458 MB、55 万行，**全部是最近 7 天的数据**；有 7 张表的外键引用它 |
| `api_call_logs` | 116 MB、48.6 万行，最早一条是 10-19 |
| `checkpoints` | 224 MB、39 万行，主要是 counter_attempt 17 万、coordination 11 万、worker_result 8.6 万 |
| 慢查询示例 | `/api/insights` 的主查询要 10 秒（17 万行，连接 7 张表） |

## A. PG 参数调整（需要重启，约 30 秒停机）

**目的**：让整个库都能放进缓存（命中率目标 ≥ 99%），并开启 `pg_stat_statements`，以后能直接看到慢 SQL。

**由协调会话在单独的窗口执行。** PG 重启时 CTN 服务必须是停着的：服务里的连接池在 PG 重启后会拿到已断开的连接。

### A0. 停 CTN 服务
按 `scripts/Reload-CTN.ps1` 的停服流程执行（先让 Agent 进入维护、排空正在跑的任务，再停服务）。

### A1. 备份当前配置（只复制文件，不改数据）
```bash
docker exec ctn-postgres-1 cp /var/lib/postgresql/data/postgresql.auto.conf /var/lib/postgresql/data/postgresql.auto.conf.bak-20261022
docker exec ctn-postgres-1 cat /var/lib/postgresql/data/postgresql.auto.conf.bak-20261022
```

### A2. 把容器的 `/dev/shm` 调到 1 GB
在 `compose.yaml` 的 `services.postgres` 下、`image:` 那一行后面加一行（缩进 4 个空格）：
```yaml
    shm_size: 1g
```

### A3. 写入参数（重启前只写进 postgresql.auto.conf，不会立即生效）
```bash
docker exec -i ctn-postgres-1 psql -U ctn -d ctn -v ON_ERROR_STOP=1 < scripts/db-phase4-a-settings.sql
```
写入的参数：shared_buffers 4GB、effective_cache_size 16GB、maintenance_work_mem 512MB、work_mem 16MB、random_page_cost 1.1、effective_io_concurrency 200、max_wal_size 4GB、checkpoint_timeout 15min、wal_compression lz4、shared_preload_libraries pg_stat_statements、pg_stat_statements.max 5000、track_io_timing on、jit off。

### A4. 重启
`up -d` 会按新的 compose 重建容器，数据卷 `ctn_ctn_postgres` 保留：
```bash
docker compose up -d postgres
docker exec ctn-postgres-1 pg_isready -U ctn -d ctn
```
如果没有改 compose，用 `docker restart ctn-postgres-1` 代替第一条。

### A5. 检查 PG
```bash
docker exec ctn-postgres-1 psql -U ctn -d ctn -c "SHOW shared_buffers" -c "SHOW shared_preload_libraries" -c "SHOW jit" -c "CREATE EXTENSION IF NOT EXISTS pg_stat_statements" -c "SELECT count(*) FROM pg_stat_statements"
docker exec ctn-postgres-1 df -h /dev/shm
```
预期：shared_buffers 为 4GB，shared_preload_libraries 为 pg_stat_statements，jit 为 off，/dev/shm 为 1.0G。

### A6. 启动 CTN 服务，确认 `/api/health` 正常
运行 30 分钟后由 C 复测：缓存命中率（目标 ≥ 99%）和 `/api/insights` 主查询耗时（目标 ≤ 5 秒）。

### A 的回滚
**情况 1：PG 能启动**
```bash
docker exec -i ctn-postgres-1 psql -U ctn -d ctn -v ON_ERROR_STOP=1 < scripts/db-phase4-a-rollback.sql
docker restart ctn-postgres-1
```

**情况 2：PG 起不来**，比如某个参数写错了。用备份文件覆盖回去：
```bash
docker compose stop postgres
docker run --rm -v ctn_ctn_postgres:/data alpine sh -c "cp /data/postgresql.auto.conf.bak-20261022 /data/postgresql.auto.conf && chown 70:70 /data/postgresql.auto.conf"
docker compose up -d postgres
```
- 在 postgres:17-alpine 镜像里，postgres 用户的 uid 和 gid 都是 70。

**撤回 shm 改动**：删掉 compose 里那一行，再执行 `docker compose up -d postgres`。

以上回滚都不涉及表数据。

## B. 正文搜索索引（不需要重启；由 C 执行，要避开重载窗口）

**目的**：帖子搜索（`LOWER(body) LIKE '%词%'`）和新闻账号帖子搜索，现在都是全表扫描，耗时 280 ms。

```bash
docker exec -i ctn-postgres-1 psql -U ctn -d ctn -v ON_ERROR_STOP=1 < scripts/db-phase4-b-trgm.sql
```
- 不要加 `-1`：`CONCURRENTLY` 不能在事务里执行。
- 建索引期间不锁写入，预计需要 1–2 分钟，索引约 60–100 MB。
- 查询代码不用改：现有的 `LOWER(p.body) LIKE LOWER(?)` 就能用上这个索引。
- 建完后测一下搜索 "vote"，目标 ≤ 30 ms。
- 后续：把这两条语句加进 `postgres.py` 的 PG 专用 DDL，以后重建库时也会自动创建。`postgres.py` 是共享文件，要先登记变更公告。
- **回滚**：
  ```bash
  docker exec -i ctn-postgres-1 psql -U ctn -d ctn < scripts/db-phase4-b-rollback.sql
  ```
  如果并发建索引失败、留下无效索引（indisvalid = false），也用这条清理。

## C. 热点表的 autovacuum（不需要重启；由 C 执行）

**目的**：几张高频更新的表死元组堆积较多（checkpoints 3.4 万、posts 2.9 万），默认要到 20% 的行变成死元组才会触发清理。

```bash
docker exec -i ctn-postgres-1 psql -U ctn -d ctn -v ON_ERROR_STOP=1 < scripts/db-phase4-c-autovacuum.sql
```
- 执行前这 5 张表的 reloptions 都是 NULL（已只读核实）。
- **回滚**：
  ```bash
  docker exec -i ctn-postgres-1 psql -U ctn -d ctn -v ON_ERROR_STOP=1 < scripts/db-phase4-c-rollback.sql
  ```

## D. 保留期限清理：**不做**（这一项要由用户本人决定）

- `raw_records` 现在全部是 7 天内的数据，比赛 19 日才开始，所以设 7 天保留期一行也删不掉。
- 按每天约 150 MB 的增长估算，到 11 月 2 日比赛结束时约有 2.5 GB；那时 7 天保留期大约能释放 1 GB。
- 磁盘还剩 951 GB，空间不是问题。
- `raw_records` 被 posts、counter_snapshots 等 7 张表的外键引用。要删就得先把引用置空，会丢掉"每条数据来自哪次请求"的证据链。
- `api_call_logs`（116 MB）同理，运行页的统计要用它。
- **C 不会删除任何数据。**

## E. checkpoints 拆表：**延后**

- counter_attempt 和 coordination 这两类数据合计 28 万行、约 23 MB，按主键查找很快，不是瓶颈。
- 拆表要改 `collector.py` 和 `coordination.py`（都是共享文件）的写入路径，在比赛期间风险大于收益。
- `worker_result` 的 8.6 万行已经查过，属于正常现象：最早一条正好是 24 小时前写入的，说明 retention 在按 24 小时清理，这个数量就是一天的量（每小时约 3600 行）。

## F. meta 表的 VACUUM FULL：**不需要了**

现在 33 MB（实际数据 3.5 MB），日常的 autovacuum 已经能维持住。VACUUM FULL 会加排他锁，阻塞所有 Agent 的读写，不值得做。

## 相关修复：分析页的长读事务（C，2026-10-22，未部署）

协调会话发现有一个 `ctn_stream_` 游标在只读事务里挂了 4.5 分钟（D31），会挡住 VACUUM。

- **来源**：`analytics.analyze`（分析页）把整个计算放在一个 `BEGIN` 里，冷启动时还会用一个游标边读边算。
- **修复**：
  - 去掉外层事务；
  - 快照改为每批 500 帖读取，读完马上关闭游标，然后再计算。
- **线上实测**：整个分析耗时 52.9 秒（和以前差不多），但其中最长的单个读事务只有 2 秒。
- **测试**：分析相关的 75 个测试通过，PG 验证 16 个也通过。
- **新的取舍**：帖子、快照和关系这几次读取不再共享同一个快照时刻，可能有几秒的时间差，对统计分析的影响可以忽略。
