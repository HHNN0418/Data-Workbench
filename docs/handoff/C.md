# 会话 C 交接（基础功能改进与性能优化，2026-10-22）

C 负责数据、后端接口、性能和数据库；页面 UI 归 E。交接时手上没有做到一半的改动，所有改动都已收尾到测试能通过的状态，**没有补丁文件**。git 基线：`d0ce99a`（第四窗口）。

## 1. 已完成、未部署的改动（等下一个重载窗口）

| 项目 | 文件 | 说明 |
|---|---|---|
| `GET /api/pipeline/status` | 新建 `ctn_data/pipeline.py`；`server.py` 加路由 | 首页全链路六段：采集、处理、分析、策略、执行、效果。每段返回 status ok/warn/bad/unknown、headline、metrics、updated_at，阈值是模块常量；缓存 60 秒，线上算一次约 2.75 秒。效果段含：向我方转变的占比、今日浏览额度 / 500、市民回复数；执行段含：原创:回复（目标 2:1）。字段说明已发给 E |
| `GET /api/clusters` | 新建 `ctn_data/clusters.py`；`server.py` 加路由 | 48 小时行为簇：同文 ≥3 个账号，加上转发圈（对同一簇转发 ≥4 次）。每簇有成员、每小时写入、类型占比、回复里回给市民的比例、立场、Trending 占比、市民互动分 / 小时、机器人数和原因、示例帖子，以及和 Blue 的关联（blue_candidates、blue_cluster_ids）。另有我方对照行 `own`。缓存 10 分钟，过期后在后台重算；冷启动约 20 秒。字段说明已发给 E |
| 市民判定统一 | 新建 `ctn_data/citizens.py` | 只是一层转接，直接调用 B 的 `agent_targeting.citizen_accounts`；pipeline 和 clusters 都用它 |
| D39 机器人新特征接入 | `annotations.refresh_accounts`；`db.py` / `postgres.py` 加列 `account_annotations.bot_metrics_json` | is_bot = behavior 命中，或 B 的 `bot_feature_flags` 四个特征命中 ≥2 个；本队账号一律不标为机器人；只有 duplicate_text 或只命中 1 个特征时只做标注。B 的函数出错时，这一轮整个跳过，保留上一轮的结果。**重载时 schema 指纹会变**，会自动补上这一列；SQLite 和 PG 的旧表补列都验证过 |
| is_bot 统一口径 | `queries.py`（posts、accounts）、`insights.py` | 去掉另外判断 'behavior' 的那一层，直接读 is_bot |
| `BlueCandidates.peek()` | `blue_view.py` | 只读缓存、不计算，供 pipeline 和 clusters 使用 |
| 测试 | `tests/test_pipeline.py`、`test_clusters.py`、`verify_pipeline_postgres.py`、`verify_clusters_postgres.py`；修改了 `test_annotations.py`、`test_nav_status.py`（审计事件补 id，PG 要求非空）、`test_blue_view.py` | 全部通过 |

### 重载后要检查
- `/api/pipeline/status` 返回 6 段，都不是 unknown。线上现在 overall 为 bad，原因属实：原创为 0；12 小时 26.5 分/时，低于 Poll 2 需要的 35 分/时。
- `/api/clusters`：冷启动后第一次请求约 20 秒；之后 `clusters_total` 约 11。
- `account_annotations` 有 `bot_metrics_json` 列。AnnotationWorker 跑一轮（约 60 秒）之后，我方 5 个账号的 `is_bot` 都为 0。
- 账号页 `bots=only` 的人数会变多：新增了"命中 ≥2 个特征"的机器人，B 试跑时新增约 279 个。

## 2. 剩下的任务

| 任务 | 状态 | 说明 |
|---|---|---|
| E 要的 `GET /api/pipeline/history?hours=24` | 未开始 | 每小时一个点，`{generated_at, buckets[], series{collect, process, analyze, strategy, execute, outcome}}`，每段一个主指标，缺数据的小时填 null。E 说不急；没有这个接口时首页不显示走势图 |
| clusters 补连接方式 | 可选 | 深度分析报告还用了"30 分钟活跃度相关"和"创建时间相近"两种连接方式，这里没有做（`method.not_used` 已写明），所以簇比报告少 |
| 第四阶段 C 步：autovacuum 表参数 | **等用户本人确认** | 脚本 `scripts/db-phase4-c-autovacuum.sql`，回滚脚本 `scripts/db-phase4-c-rollback.sql`。协调会话已批准（D32），但用户给 C 定的规矩是"写线上库之前先问用户"，C 已经在会话里问了，用户还没回答。**接手的子 agent 也要等用户本人确认后才能执行** |
| 第四阶段 B 步：pg_trgm + 帖子正文 GIN 索引（CONCURRENTLY） | **等用户本人确认**，并且要避开重载窗口 | 脚本 `scripts/db-phase4-b-trgm.sql`，回滚脚本 `scripts/db-phase4-b-rollback.sql`。建完后核对搜索 "vote" 的耗时，目标 ≤ 30 ms |
| 第四阶段 A 步：PG 重启（shared_buffers 4 GB、pg_stat_statements、shm_size 1 g） | 由协调会话单独开窗口执行 | 步骤和回滚方法：**`docs/DB_PHASE4_PLAN.md` 的 A 节**；参数脚本 `scripts/db-phase4-a-settings.sql`，回滚脚本 `scripts/db-phase4-a-rollback.sql`。执行完后复测缓存命中率（目标 ≥ 99%，现在 88%）和 `/api/insights` 主查询耗时（目标 ≤ 5 秒，现在 10 秒） |
| 第四阶段 D 步：保留期限清理 | 不做 | 这是用户的决定；而且现在也清不出空间 |
| 第四阶段 E 步：checkpoints 拆表 | 延后 | worker_result 已查过，属于正常 |
| N7：帖子和账号列表加 retainPrevious | 等 E 开工 S3 时再做 | 在 `api.ts` 的 useApi 调用上把 retainPrevious 设为 true |

## 3. 规则和注意事项

- **对线上库跑脚本前，先禁用 DDL**（规则 6）。`AgentStore(...)`、`analytics.analyze`、`scoring.score_overview` / `score_pace` 都会调用 `initialize()`；工作目录的 SCHEMA 比线上新时，会在线上执行建表语句。做法：
  ```python
  from ctn_data import postgres
  postgres.initialize_postgres = lambda *a, **k: (_ for _ in ()).throw(RuntimeError('no DDL on prod'))
  ```
  对已经在模块里 import 了 `initialize` 的地方，要单独替换，例如 `analytics.initialize = lambda *a, **k: None`。读操作用 `BEGIN READ ONLY`。
- 读事务不要跨好几秒（D31）；大批量读取要分批，读完马上关闭游标。
- 共享文件（`server.py`、`db.py`、`postgres.py`、`main.tsx`、api 客户端）改之前，先在协调板的「变更公告」写一行。
- 改 B 的 `agent_targeting` 只能调用、不能改；`classify` 的正则变化会通过 `annotations.classifier_version()` 自动触发重新标注。

## 4. 测试命令

后端，C 相关的模块，SQLite：
```bash
.venv/Scripts/python.exe -m unittest tests.test_pipeline tests.test_clusters tests.test_annotations tests.test_insights tests.test_nav_status tests.test_blue_view tests.test_server tests.test_scoring tests.test_analytics tests.test_analysis_cache tests.test_system_analytics_performance tests.test_agent_targeting
```

PG 临时库验证（每个脚本都会建一个随机库，跑完删除，不碰线上库）：
```bash
for t in annotations insights pipeline clusters analysis_cache; do .venv/Scripts/python.exe tests/verify_${t}_postgres.py 2>&1 | tail -1; done
```

编译检查：
```bash
.venv/Scripts/python.exe -m compileall -q ctn_data
```

前端：C 这次没有改前端。按协调板的规则，全量测试由协调会话在重载窗口里跑；本机并行跑会有约 17 个超时假失败，请用 `--no-file-parallelism`。

## 5. 线上观察（只读，2026-10-22）

- 最近 6 小时原创:回复 = 0:286；今日浏览额度 46/500；最近 6 小时回给我方的回复 130 条，其中市民 4 条；转向我方的比例 6 小时为 0、24 小时为 3.7%。
- Poll 2：总分 4615，还差 2385 分；12 小时 26.5 分/时，6 小时 30.8 分/时，需要 35 分/时；on_track 为 false。
- 聚类：11 个簇、231 个账号；我方 9.6 分/时（市民口径），回复里 9.2% 回给了市民；排第一的簇是 45.6 分/时。
