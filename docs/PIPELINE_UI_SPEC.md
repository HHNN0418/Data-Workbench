# 全链路 UI 重构规格（协调会话，2026-10-22）

用户原话：「我这个 ui 可完全重构，我要的是从数据获取到数据处理分析到 agent 流程，整个串连起来的视图，而且顺序得对」。

这份规格替代 `docs/UX_IA_PLAN.md` 里按功能分组的侧边栏。E 负责页面，C 负责数据接口。

## 1. 原则
- **按数据流向排列，不按功能类型排。** 侧边栏从上到下，就是一条帖子从被采集到产生得分的路径。
- **每一段都回答三个问题**：进来多少、出去多少、卡在哪里。
- **首页就是全链路视图。** 一眼看到瓶颈在哪一段，点进去就是那一段的详细页面。
- 效果数据要回流到策略：页面上画出这条回路，标出现在用的是哪一版雷达和归因结果。

## 2. 侧边栏（顺序固定）

```
◎ 全链路总览            /                    ← 首页
① 采集
   采集运行               /operations          采集任务、代理、Feed 覆盖、延迟
   帖子流                 /posts               原始帖子，Latest / Trending
   账号                   /accounts
   新闻与话题             /news
② 处理
   标注                   /processing          立场、顾虑、线程深度（post_annotations 进度和分布）
   机器人识别             /processing/bots     account_annotations、行为规则、重复文本
   事实提取               /ai/facts?tab=extraction
③ 分析
   舆论与洞察             /analysis            insights：立场走势、发帖时机、内容效果、Trending 特征
   顾虑雷达               /ai/analysis         雷达报告、priorities、measured
   Blue 候选              /blue
④ 策略
   事实库                 /ai/facts
   目标与配比规则         /strategy            effective_actions、low_reach、发帖 / 回复配比、层数和线程上限（先做成只读）
   提示词与模板           /ai/facts?tab=prompts
   资料筛选               /ai/reading
⑤ Agent 执行
   流水线监控             /agents/pipeline     选读 → 决策 → 写作 → 检查 → 发布队列，每段的耗时、失败和排队
   角色                   /ai/agents
   待处理                 /ai/agents?tab=inbox  通知和操作记录
   人工代发               /ai/agents?tab=manual
   发布审核               /ai/review
   额度                   /ai/agents?tab=quotas
⑥ 效果
   得分与目标             /score               官方得分、Poll 2 门槛速度预警、排行榜差距
   效果归因               /results/attribution  post_attribution：哪些顾虑和角度带来回复、转发、向我方转变
   曝光与说服             /results/exposure     浏览、进 Trending、向我方转变、发帖 / 回复比例（measure_kpis 的 exposure 段）
⚙ 系统（默认折叠）
   AI 连接 / 规则与情景 / 设置中心 / 维护
```

- 旧 URL 全部保留并重定向。新页面的路径必须同时加进 `server.py` 的 `SPA_PATHS`，`test_spa_routes` 会检查。
- 侧边栏每一段标题旁边显示状态点（绿 / 黄 / 红），数据来自 `/api/pipeline/status`。

## 3. 首页：全链路视图（核心）

一条横向流水线，窄屏时竖排。共 6 个节点，节点之间的箭头上写"流量"。示例：

```
① 采集          ② 处理           ③ 分析           ④ 策略          ⑤ 执行                 ⑥ 效果
15.0万帖  ──▶  已标注 15.0万  ──▶ 犹豫帖 6413/6h ──▶ 生效事实 70  ──▶ 决策 52/h ─▶ 发布 48/h ──▶ 浏览 4/h ─▶ 转变 0/h
+820/h         机器人 335        雷达 2h 前       配比 发帖 0.3%     失败 7%      队列 2       得分 28/h  (需 34/h)
延迟 40s       积压 0            Yes 占比 0.50    ⚠ 配比未达标                                ⚠ 曝光瓶颈
                                                        ▲─────────── 归因结果回流（measured） ───────────┘
```

- **每个节点显示**：主数字（吞吐）、1–2 个辅助数字、状态色、最近一次更新时间，点击进入对应段落的页面。
- **自动标出瓶颈**：转化率最低的一段，或者低于阈值的一段，标红，并写一句为什么（例如"浏览 4 / 小时：发帖占比 0.3%，回复不进信息流"）。
- **顶部一行**：官方得分、Poll 2 所需速度和当前速度（复用 PaceAlert）、排行榜差距（手动录入，或者显示最近一次的快照）。
- **下方**：最近 24 小时每段的小走势图（sparkline），一眼看出哪一段在下滑。

## 4. 数据接口（C 负责）：`GET /api/pipeline/status`

一次返回 6 段，每段都带 `status`（ok / warn / bad）、`headline` 和 `metrics{}`，`updated_at`。全部从已有的表里算，缓存 60 秒：

| 段 | 来源 | 指标 |
|---|---|---|
| 采集 | posts、collection_tasks、counter_sampling、news_articles | 帖子总数、每小时新增、采集延迟、积压任务数 |
| 处理 | post_annotations、account_annotations、facts / fact_extractions | 已标注数和积压、机器人账号数、最近一次事实提取时间 |
| 分析 | 雷达报告、insights 缓存、blue 候选 | 犹豫帖（6 小时）、雷达报告的生成时间、Yes 占比、Blue 今日建议数 |
| 策略 | facts、Agent 设置 | 生效事实数、发帖 / 回复配比（最近 50 条）、被屏蔽的目标比例 |
| 执行 | agent_audit_events、llm_calls、发布队列 | 每小时决策数和发布数、各步耗时中位数、失败率和主要原因、队列长度、未确认通知数 |
| 效果 | score_snapshots、official_activity、post_attribution | 每小时得分（6h 和 12h）、所需速度、每小时浏览、向我方转变次数、归因结算数 |

阈值写在后端的常量里，例如发帖占比低于 0.4 为 warn、低于 0.1 为 bad，得分速度低于所需速度为 bad。前端只负责显示。

## 5. 分阶段上线
1. **S-A**（E）：按第 2 节的顺序重排侧边栏，旧路由全部保留；新路径先用占位页，写清楚"这一段的数据在建设中"。
2. **S-B**（C）：`/api/pipeline/status` 加测试。
3. **S-C**（E）：首页全链路视图，加上侧边栏状态点。
4. **S-D**（E 和 C）：/processing、/strategy、/agents/pipeline、/results/* 这些段落页面。能复用的现有组件尽量复用。
- 每一步都要通过 tsc、相关测试和 `test_spa_routes`，状态改成 green 后进入下一个重载窗口。
