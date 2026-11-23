# C 接口字段说明：`/api/pipeline/status` 与 `/api/clusters`

给 UI（E 的工作线）用。两个接口都**只读**，都还没部署，等下一个重载窗口上线。代码：`ctn_data/pipeline.py`、`ctn_data/clusters.py`；市民口径：`ctn_data/citizens.py`（直接调用 B 的 `agent_targeting.citizen_accounts`）。

通用约定：
- 和其他 `/api/*` 一样需要登录会话；都没有请求参数。
- 时间都是 ISO 8601 UTC 字符串（`...Z`）。比例都是 0–1 的小数，前端自己格式化成百分比。
- `null` 表示"没有数据可算"，不等于 0；前端显示"—"。
- 阈值全在后端，前端只按 `status` 着色：`ok` 绿、`warn` 黄、`bad` 红、`unknown` 灰。

---

## 1. `GET /api/pipeline/status`

首页六段全链路和侧边栏状态点用它。每个进程缓存 **60 秒**；缓存过期后，第一个请求重算（线上约 2.75 秒），同时到达的其他请求直接拿上一次的结果。建议前端 60 秒轮询一次。

### 1.1 顶层

| 字段 | 类型 | 说明 |
|---|---|---|
| `generated_at` | string | 本次计算时间 |
| `overall` | `ok`/`warn`/`bad`/`unknown` | 已知各段中最差的状态；六段都是 unknown 时才是 unknown |
| `stages` | array[6] | 固定顺序：`collect` `process` `analyze` `strategy` `execute` `outcome` |
| `thresholds` | object | 后端用的阈值，只供显示（"低于 X 为黄"之类的提示），见 1.4 |

### 1.2 每段（`stages[i]`）

| 字段 | 类型 | 说明 |
|---|---|---|
| `key` | string | 上面六个之一。`outcome` 对应侧边栏的"效果 / results" |
| `label` | string | 采集 / 处理 / 分析 / 策略 / 执行 / 效果 |
| `status` | `ok`/`warn`/`bad`/`unknown` | `unknown` 只在这一段计算出错时出现 |
| `headline` | string | 一句中文摘要，可以直接显示；缺的数显示成 "—" |
| `metrics` | object | 见 1.3；`unknown` 时为 `{}` |
| `updated_at` | string / null | 这一段数据源的最新时间（每段含义见 1.3） |
| `error` | string | **只在 unknown 时有**：异常类名，例如 `RuntimeError` |

### 1.3 各段 metrics

**① collect 采集**（`updated_at` = 最新一次 Latest 采集时间）

| 字段 | 类型 | 说明 |
|---|---|---|
| `posts_total` | int | posts 表总行数 |
| `posts_last_hour` | int | 最近 1 小时发出的帖子（按 created_at） |
| `feed_age_minutes` | float / null | 距最近一次 Latest 采集多少分钟（只看 24 小时内） |
| `counter_age_minutes` | float / null | 距最近一次计数快照多少分钟 |
| `task_backlog` | int | 已到期、还没完成的采集任务 |
| `news_articles` | int | 新闻总数 |

状态：`feed_age_minutes` >5 黄、>15 红；`counter_age_minutes` >10 黄、>30 红；`task_backlog` >1000 黄、>5000 红；24 小时内没有 Latest 采集记录直接红。

**② process 处理**（`updated_at` = 最新一条帖子标注时间）

| 字段 | 类型 | 说明 |
|---|---|---|
| `annotated_posts` | int | 已标注帖子数 |
| `annotation_backlog` | int | 没标注或标注版本过期的帖子 |
| `bot_accounts` | int | `account_annotations.is_bot=1` 的账号数 |
| `pending_facts` | int | 待审事实 |
| `fact_extraction_enabled` | bool / null | 事实提取开关（来自 nav 状态，读不到为 null） |
| `last_fact_extraction_at` | string / null | 最近一次事实提取 |
| `fact_extraction_age_hours` | float / null | 距最近一次事实提取的小时数 |

状态：积压 >2000 黄、>10000 红；提取开着时，距上次提取 >6 小时黄、>24 小时红。

**③ analyze 分析**（`updated_at` = 雷达生成时间）

| 字段 | 类型 | 说明 |
|---|---|---|
| `undecided_posts_6h` | int | 6 小时内犹豫立场的公民帖（不含本队账号和机器人） |
| `yes_share_6h` | float / null | 公民帖 Yes ÷ (Yes+No) |
| `citizen_posts_6h` | int | 6 小时内的公民帖总数（各立场合计） |
| `radar_generated_at` | string / null | 最近一份顾虑雷达 |
| `radar_age_hours` | float / null | 雷达是几小时前生成的 |
| `blue_plan_today` | int / null | Blue 今日计划条数；**只读 Blue 页已有的缓存，不会在这里计算**，没人打开过 Blue 页时为 null |

状态：雷达 >3 小时黄、>6 小时红；没有雷达为黄。

**④ strategy 策略**（`updated_at` = 最近一次发布）

| 字段 | 类型 | 说明 |
|---|---|---|
| `active_facts` | int | 生效事实数 |
| `recent_actions` | int | 最近 50 次发布（post_attribution）里实际有几条 |
| `recent_posts` / `recent_replies` | int | 其中原创 / 回复 |
| `post_share` | float / null | 原创占比 |
| `target_pool_6h` | int | 6 小时内非本队的帖子数 |
| `target_blocked_share` | float / null | 其中不能当回复目标的比例（机器人、第 7 层及更深、Yes 立场；agent_targeting 规则的近似） |

状态：`post_share` <0.4 黄、<0.1 红；`target_blocked_share` >0.9 黄、>0.97 红。

**⑤ execute 执行**（`updated_at` = 最近一次发布）

| 字段 | 类型 | 说明 |
|---|---|---|
| `decisions_last_hour` / `published_last_hour` | int | 最近 1 小时决策完成数 / 发布数 |
| `decisions_6h` / `published_6h` | int | 最近 6 小时 |
| `failure_rate_6h` | float / null | 失败 ÷ (完成 + 失败) |
| `top_failures_6h` | array | 至多 3 条 `{reason（≤160 字）, count}` |
| `step_median_ms` | object | `{purpose: 毫秒中位数}`，例如 `{"decide": 1200, "write": 3400}`，只算成功的模型调用 |
| `llm_error_rate_6h` | float / null | 模型调用出错比例 |
| `pending_actions` / `inbox_unresolved` | int / null | 发布队列长度 / 未处理通知（来自 nav 状态） |
| `minutes_since_publish` | float / null | 距上次发布的分钟数 |
| `posts_6h` / `replies_6h` | int | 6 小时内我方原创 / 回复 |
| `post_reply_ratio_6h` | float / null | **原创 ÷ 回复**，目标 2.0（D35 的 2:1）。没有回复时等于原创数；两者都是 0 时为 null |
| `post_reply_target` | float | 固定 2.0 |

状态：失败率 >0.3 黄、>0.6 红；队伍开着却 2 小时没发布为红；6 小时内有发布时，`post_reply_ratio_6h` <1.0 黄、<0.25 红。

**⑥ outcome 效果**（`updated_at` = 最新积分快照时间）

| 字段 | 类型 | 说明 |
|---|---|---|
| `points_per_hour_6h` / `points_per_hour_12h` | float / null | 官方总分的每小时增速（扣除 polling 奖励） |
| `required_points_per_hour` | float / null | 下一次 Poll 达到 7000 分需要的每小时增速 |
| `next_poll` | int / null | 下一次 Poll 编号，例如 2 |
| `gap_to_threshold` | **string** / null | 离门槛还差多少分（十进制字符串，例如 `"2385"`） |
| `on_track` | bool / null | 按当前速度能否赶上 |
| `total` | **string** / null | 官方总分（十进制字符串） |
| `score_stale` | bool | 积分快照太旧 |
| `views_per_hour_6h` | float | 官方 red 日志里 6 小时的 NPC 浏览 ÷ 6 |
| `changes_towards_6h` / `changes_away_6h` / `changes_neither_6h` | int | 6 小时内官方记录的观点变化：转向我方 / 远离我方 / 两边都不是 |
| `towards_share_6h` / `towards_share_24h` | float / null | **转向我方 ÷ 全部观点变化**；没有观点变化时为 null |
| `views_today` | int | **悉尼日**（悉尼午夜重置）至今入账的 NPC 浏览数 |
| `view_cap` | int | 固定 500（全队每天浏览上限） |
| `view_cap_used` | float | `views_today ÷ 500` |
| `citizen_replies_6h` / `citizen_replies_24h` | int | 回复我方帖子的**校准市民**回复数（口径见第 3 节） |
| `replies_to_us_6h` / `replies_to_us_24h` | int | 回复我方帖子的全部回复数（分母，用来显示"市民 4 / 共 130"） |
| `attribution_total` / `attribution_settled` | int | 归因记录总数 / 已结算数 |

状态：需要速度为 null 或 0 时绿；12 小时速度低于需要速度为红；只有 6 小时速度低于时为黄；积分快照过旧时至少为黄。

### 1.4 thresholds

数组都是 `[warn, bad]`：`feed_age_minutes [5,15]`、`counter_age_minutes [10,30]`、`task_backlog [1000,5000]`、`annotation_backlog [2000,10000]`、`fact_extraction_age_hours [6,24]`、`radar_age_hours [3,6]`、`post_share [0.4,0.1]`（越低越差）、`target_blocked_share [0.9,0.97]`、`failure_rate_6h [0.3,0.6]`、`post_reply_ratio_6h [1.0,0.25]`（越低越差）。标量：`idle_hours 2`、`post_reply_target 2.0`、`view_cap 500`。

### 1.5 还没有的

`GET /api/pipeline/history?hours=24`（走势图）还没做；没有它时首页不显示 sparkline。

---

## 2. `GET /api/clusters`（D41）

账号簇页用它。窗口固定 **48 小时**，不接受 `hours` 参数。

缓存：每个进程一份，**10 分钟**有效。
- 服务启动后的第一个请求现场计算，线上约 20 秒，前端要显示加载状态。
- 过了 10 分钟，先返回旧结果并带 `stale: true`，同时在后台重算。
- 后台重算失败时继续返回旧结果（`stale` 一直为 true），下一个请求会再试。

**页面上必须显示 `note`**：簇是按行为聚出来的账号组，不等于已确认的队伍。

### 2.1 顶层

| 字段 | 类型 | 说明 |
|---|---|---|
| `generated_at` | string | 计算时间 |
| `window` | object | `{hours: 48, from, to}` |
| `note` | string | "簇是按行为聚出来的账号组，不等于已确认的队伍" |
| `own` | object | **我方对照行**，字段和簇一样，另有 `label: "我们自己（Agent Zero）"`、`cluster_id: "own"`、`is_own: true`。建议固定放在表格第一行 |
| `clusters` | array | 至多 30 个，按 `citizen_points_per_hour` 降序（相同时按 `writes` 降序） |
| `clusters_total` | int | 聚出的簇总数（可能多于 30） |
| `clustered_accounts` | int | 所有簇的账号总数 |
| `citizen_accounts` | int | 窗口里涉及的校准市民数（不含簇成员和本队） |
| `method` | object | `link[]`（连接方式说明）、`not_used[]`（没用上的方法："30 分钟活跃度相关"、"创建时间相近"）、`min_accounts`（3）、`citizen_points`（计分口径说明）。可以放在"方法说明"折叠区 |
| `stale` | bool | 见上面的缓存说明 |

### 2.2 每个簇（以及 `own`）

协调要求的字段对照：id → `cluster_id`，size → `accounts`。

| 字段 | 类型 | 说明 |
|---|---|---|
| `cluster_id` | string | `c<账号id>`，例如 `c5521`；`own` 行为 `"own"`。**每次重算可能变化**，只当页面内的 key 用；和 Blue 的 cluster_id 不是一回事 |
| `is_own` | bool | 我方对照行为 true |
| `label` | string | 只有 `own` 有 |
| `accounts` | int | 成员数（size） |
| `members_total` | int | 同 `accounts` |
| `members` | array | **全部成员**，按 `writes` 降序：`{account_id, username（可能为 null）, is_bot, bot_reasons[], writes}`。账号页链接 `/accounts?q=<account_id>` |
| `writes` | int | 48 小时内原创 + 回复 + 转发 |
| `hourly_writes` | int[48] | 每小时写入，**最旧在前**，下标 0 是 `window.from` 所在的那个小时 |
| `kinds` | object | `{post, reply, repost}` 计数 |
| `post_share` / `reply_share` | float / null | 原创 / 回复占全部写入的比例 |
| `post_reply_ratio` | float / null | **原创 ÷ 回复**，规则和 pipeline 的 `post_reply_ratio_6h` 一样：没有回复时等于原创数，都为 0 时为 null。可以和我方目标 2.0 对照 |
| `replies_to_citizens_share` | float / null | 成员回复里回给校准市民的比例；没有能找到父帖作者的回复时为 null |
| `stance` | object | `{yes, no, undecided}` **比例**（来自 post_annotations，不含 unrelated）；`stance_posts` 为 0 时三个值都是 null |
| `stance_posts` | int | 有这三种立场之一的帖子数（比例的分母） |
| `trending_share` | float / null | 48 小时内原创帖里上过 Trending 的比例；没有原创时为 null |
| `citizen_replies` / `citizen_reposts` | int | 校准市民对成员帖子的回复 / 转发数 |
| `citizen_points_per_hour` | float | 估算的市民互动分：(回复 ×15 + 转发 ×10) ÷ 48。点赞和浏览没法归到作者，不计，所以是**下限估计，不是官方分数**。列名建议"市民互动分 / 时" |
| `bot_flagged` | int | 被标为机器人的成员数 |
| `bot_share` | float / null | `bot_flagged ÷ accounts` |
| `bot_reasons` | object | `{原因: 人数}`，只统计被标为机器人的成员。原因取值：`behavior`、`always_on`、`high_volume`、`fast_reply`、`regular_rhythm`、`duplicate_text` |
| `blue_candidates` | int / null | 成员中有几个出现在 Blue 候选里；Blue 页还没算过（没有缓存）时为 null |
| `blue_cluster_ids` | string[] | 这些候选在 Blue 里的 cluster_id；跳到 Blue：`/blue?clusters=<逗号连接>` |
| `examples` | array | 至多 2 条 `{post_id, why, excerpt}`。`why`：`shared_text`（同文）/ `citizen_replies`（市民回复最多）/ `original`（普通原创）。`excerpt` 已合并空白，最多 120 个字符，截断时以 "…" 结尾 |

成员级 `bot_reasons` 会列出所有已存的原因，包括只做标注、没有判成机器人的情况（例如只有 `duplicate_text`）；判定以 `is_bot` 为准。

### 2.3 聚类方法（`method.link`）

复用 `blue_reports._texts` 的同文连接：
1. 同一段文字（≥40 字，不含接龙）被 ≥3 个账号发布，这些账号连成一组；
2. 对同一组转发 ≥4 次的账号并入该组；
3. 至少 3 个账号才算一个簇。

本队账号不参与聚类，只出现在 `own` 行。

---

## 3. 校准市民口径（两个接口共用）

来源 `agent_targeting.citizen_accounts`（`reports/generated/deep-analysis/citizens.md`）。一个账号同时满足下面各条才算市民：
- 不是本队账号（`team_accounts.is_own=1`）；
- 没有被标为机器人（`account_annotations.is_bot=1`，并且原因不只是 `duplicate_text`）；
- 全部数据里原创 + 回复 + 转发 **少于 25 次**；
- 不是转发圈账号（≥8 次动作，其中至少一半是转发）；
- 不是批量编号用户名：去掉数字后同一模式 ≥5 个账号，并且都是比赛开始后注册的；
- 不是认证账号，正文里没有 LLM 痕迹（"Sure, here…""as an AI language model" 等）。

`/api/clusters` 另外把所有簇成员排除在市民之外。
