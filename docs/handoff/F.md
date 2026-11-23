# F 交接：手动发帖 / 回复（2026-10-22）

## 现状
- **手工发帖已关闭**（D36，用户原话「关闭手工发帖」）。不要再代发，也不要准备人工草稿，除非用户本人重新开放。
- 关闭时线上一共只有 5 条人工草稿，都是 Simon（6503）的第一批，全部 published：新帖 153678，回复 153820、154011、154199、154526。没有排队中的，也没有待确认的。
- **人工账号**：Simon Mercer（`simon_mercer`，6503，D18）。他的 `enabled=False`，所以调度、通知轮询和竞赛分配都会跳过他。`mode` 仍是 auto；如果以后重新开放，建议改成 review，作为第二道保险。
- **已上线**：
  - D26 人工排队，第三窗口，git `5d0b1a6`；
  - D29 preflight，第四窗口，git `b2004dd`，unified-20261022d；
  - E 的草稿卡"排队发布 / 取消排队"按钮，第四窗口。
- 完整审查和 25 项状态表见 `docs/MANUAL_POSTING_REVIEW.md`，第 6 节是最新状态。

## 已上线的功能怎么用

### 准备：`POST /api/agents/manual/prepare`（`agent_manual.prepare`）
原有字段：`agent_id`、`action`、`body`、`target_post_id` / `target_account_id`、`idempotency_key`。

D29 新增 3 个可选字段，不传时行为和幂等标识都和以前一样：
- `acknowledge_warnings`（布尔）：确认仍要回复有风险的目标。
- `fact_ids`（最多 6 个）：只接受 `facts.status='active'` 的编号，否则返回 400 并列出编号。
- `concern`：`fact_bank.CONCERNS` 之一，或 `none`。和 `fact_ids` 一起写进草稿，`post_attribution` 会读取。

规则：
- **@ 上限**：帖子和转发最多 4 个，回复最多 3 个，超过返回 400。
- **回复目标碰到下面任一种**，又没带 `acknowledge_warnings=true`，返回 **409**，不建草稿：
  - `bot_like_author`；
  - `thread_too_deep`（目标在第 7 层以后，我方回复会落到第 8 层及以后）；
  - `team_already_replied`（我方任何账号已回过这条帖子，包括 pending、sending、uncertain 状态的草稿）；
  - `target_check_unavailable`（核对时出错）。
  确认过的项目写在草稿的 `manual_warnings_acknowledged`。
- **只提示、不阻断**：`thread_reply_limit`（这个线程我方已有 2 条回复）、`answered_recently`（6 小时内答过同一作者的同一顾虑）、`not_persuadable`（对方已偏 Yes 或话题无关）。

预览（`preview`，也存在草稿的 `manual_preview`）新增：
- `warnings`：`[{code, blocking, message}]`；
- `style_warnings`：`agent_writing.warnings` 的结果，只提示，草稿本身的 `style_warnings` 仍为 `[]`；
- `context`，只对回复和转发提供：
  - `depth`、`stance`、`concerns`；
  - `thread`：上级对话，从根帖开始，最多 6 层；
  - `author_recent`：作者最近 3 条帖子，含疑似凭据的条目会被去掉；
  - 读不到时是 `{unavailable: true}`。

### 发布
- **直接发布**：`POST /api/agents/publish {draft_id}`。走 `agent_actions.execute(automatic=False)`，额度用 D 的 `publish_priority`（人工 = 0）。额度不够时返回 429，需要人再点一次。
- **排队发布（D26）**：`POST /api/agents/manual/queue {draft_id, body?}`。`body` 必须和预览时的正文一致。取消用 `POST /api/agents/manual/queue/cancel {draft_id}`，草稿保留为 pending。
  - 排队信息存在草稿的 `manual_queue`：
    - `state`：waiting / sent / expired / cancelled / stopped；
    - 其他字段：`next_attempt_at`、`attempts`、`expires_at`、`reason`、`post_id`。
  - 有效期 15 分钟（`QUEUE_SECONDS`）。`agent_control.tick()` 里的 `agent_manual.drain_queue` 每 2 秒处理一次，在 `settings.enabled` 检查之前，所以队伍自动运行暂停时也照常处理；维护期间不处理。
  - **只发一次**：只有本地拒绝、草稿仍是 pending 时才按 `retry_after` 重试，包括额度等待、执行器忙、平台限流、角色租约占用。结果不确定、平台明确拒绝、过期、取消、丢弃都会结束排队，并调用 `agent_budget.release_priority` 释放占位。草稿正在 sending 时不动队列。
  - 每次重试前复查回复目标：出现准备时没确认过的阻断项（比如队友先回了），就停止排队，原因以"目标情况已改变："开头，不会发送。
- **前端**：Agent 页「操作记录」的草稿卡上有"排队发布 / 取消排队"按钮（E）。「手动操作」面板本身还没有接排队按钮、409 确认和预览新字段，E 计划在 `/agents/manual` 代发台里一起做。

### 代码和测试
- 代码：`ctn_data/agent_manual.py`（F）；`agent_routes.py` 的 `ManualPrepareRequest`、`/manual/queue`、`/manual/queue/cancel`；`agent_control.tick` 里 2 行钩子。
- 测试：`tests/test_agent_manual_preflight.py`（11 个）、`tests/test_agent_manual_queue.py`（14 个），另有原有的 `test_agent_manual_{api,execution,prepare,inventory}`。上线前，相关 25 个模块共 373 个测试通过。

## 还没做的项目（手工发帖已关闭，重新开放前都不用做）
编号对应 `MANUAL_POSTING_REVIEW.md` 第 3、5、6 节。

| # | 项目 | 负责 |
|---|---|---|
| 1 | 帖子页"用角色回复"，带 URL 参数预填 | E（+ 面板读参数） |
| 2 | AI 起草、人再改（`/manual/draft`，复用写作步骤） | F 接口 + A 模板 + B 流水线，要先商量 |
| 3 / 21 | 额度忙时倒计时，到点按钮自动可点 | E |
| 4 | 全队手动发布台（E 计划做成 `/agents/manual`） | E |
| 5 / 7 / 10 / 11 前端 | 409 时的"仍要回复"勾选框、警告和文风提示、上下文、事实和顾虑选择器 | E（后端已上线） |
| 6 自动部分 | 自动写作也检查 @ 上限 | A |
| 8 | 失败原因看得懂：保留状态码和平台的非敏感错误说明 | B |
| 9 / 15 剩余 | 超过 6 小时后同一帖仍可能被我方再回 | B |
| 12 | 按话题推荐账号 | E（只用 Simon 以后不重要） |
| 13 | 发布后跟进人工帖的回复（Simon 不轮询通知） | E + B |
| 14 | 角色类型"人工账号"（`manual_only`），调度跳过、启用按钮禁用、页面加标记 | B + E |
| 17 | 人工 / 自动统计接口 `/api/agents/origin-stats` | C |
| 18 | 人工 / 自动统计页面和筛选 | E |
| 19 | 竞赛归属保留手动发布（`record_published` 现在丢弃手动帖） | B |
| 22 | 区分"本条手动操作执行中"和"自动生成运行中" | E |
| 23 | 人工账号只给发帖和回复两个动作 | E |
| 24 | 手动操作面板恢复待确认草稿 | E |
| 25 执行部分 | 直接「确认执行」时也复查目标（`agent_actions.execute`） | B |

## 重新开放前要注意
- 代发要用户本人在对话里同意，并且只能通过工作台的手动操作接口（D21），不能直接调用平台或 SDK，这样才会写进草稿、审计和统计。
- 起草时用只读查询选目标，排除机器人、深线程和我方已回过的帖子，只用生效事实。注意协调板规则 6：对线上库跑脚本前，先把 `initialize_postgres` 和 `db.initialize` 替换成直接抛错的函数；只调用 `db.connect` 的只读查询不受影响。
- 全队 post 额度：「额度管理」里自定义上限曾是每分钟 1 条（D23 打算改为 2 条，要用户本人在页面上改）。人工和自动共用这个名额，人工优先级为 0。
