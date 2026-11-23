# 手动发帖 / 回复流程审查（F，2026-10-22）

只读审查，没有改代码。看过的代码：
- 后端：`ctn_data/agent_manual.py`、`agent_routes.py`（`/manual/prepare`、`/publish`、`/discard`、`/{id}/social`）、`agent_control.publish`、`agent_actions.execute/reserve`、`agent_intents.py`、`agent_interaction_policy.py`、`agent_budget.py`（`post` 桶）、`agent_review.ensure/_local_only`、`agent_targeting.py`；
- 前端：`frontend/src/pages/AgentManualActions.tsx`、`Agents.tsx`（「手动操作」标签）、`Records.tsx`（帖子页）。

## 1. 现在的流程（以"回复一条 NPC 帖子"为例）

| 步骤 | 用户要做的事 | 次数 / 问题 |
|---|---|---|
| 1 找目标 | 在「帖子」页翻找，记下 `#post_id` | 要换页面；帖子页**没有"用角色回复"按钮**，只能手抄或复制 ID |
| 2 选账号 | Agent 管理 → 点角色卡 → 「手动操作」标签 | 2 次点击；一次只能操作一个角色，没有"全队手动台" |
| 3 选动作 | 「手动动作」下拉选「回复」 | 2 次点击 |
| 4 填目标 | 「目标帖子 ID」粘贴数字 | 1 次；输错了要等第 6 步读远端才发现 |
| 5 写正文 | 自己写全文 | **不能让 AI 起草**（面板写明"这里不调用模型"）；看不到事实库；有 500 字计数（`maxLength=500` + `showCount`），但没有 @ 提及上限提示，也没有层数提示 |
| 6 预览 | 「预览并准备操作」 | 1 次；后端读远端帖子，只显示**目标帖一条正文**，不显示上级对话、层数、作者立场、是否疑似机器人、队友是否已回复过 |
| 7 发布 | 「确认执行回复」 | 1 次 |
| 8 看结果 | 提示"执行结果已更新"，点「查看此操作与结果」 | 1 次；失败原因大多看不懂（见第 2 节 E4） |
| 9 后续 | 别人回复后，通知进该角色的「收到的通知」 | 通知按账号收，不区分是手动帖还是自动帖，**由自动 Agent 接着回**；看不到"我这条手动帖收到了哪些回复"的汇总 |

合计：回复约 8 次点击，外加跨页复制 ID、全文手写；发新帖约 6 次点击。

**常见卡点**（都来自现有代码，不是猜的）：
- 角色处于 auto 模式，经常在阅读或生成。这时「预览」会被 409 拒绝（`agent_manual._idle`："角色正在阅读或生成"），执行时又检查一次（`agent_control.publish`）。用户只能等，或者去「总览」点「取消本次决策」。
- 该角色有"待核对"的动作时，所有新操作都会被拦住（`reserve`："该角色有操作结果待核对"）。
- 全队共用一个发布执行器租约。别的角色正在发送时会返回 409（"统一执行器正在执行其他动作"），只能手动再点。

## 2. 逐项检查

| 检查项 | 现状 | 依据 |
|---|---|---|
| 500 字上限 | ✅ 前端计数 + 后端校验（`_text(...,500)`、Pydantic `max_length=500`） | `agent_manual._request`、`ManualPrepareRequest` |
| @ 提及上限（帖子 4 个 / 回复 3 个） | ❌ 哪里都不检查，手动和自动都没有。只检查"不能 @ 自家其他受管账号" | `agent_interaction_policy.check_body` |
| 对话层数（第 8 层以后 NPC 很少回） | ❌ 手动不检查。自动有 `MAX_REPLY_DEPTH=7`（目标层数 ≥7 就屏蔽） | `agent_targeting.blocked_reason` 只在自动流程里用 |
| 疑似机器人 | ❌ 手动不检查。自动用 `behavior_bots`（3 小时样本里有 348 个） | 同上 |
| 文风检查（破折号、Markdown 等） | ❌ 手动跳过：`issues = [] if manual else agent_writing.warnings(...)` | `agent_actions.reserve` |
| 语言检查 | ✅ AI 审核关着时只做本地英文检查，非英文字符会被拦下 | `agent_review._local_only` |
| 每分钟 3 条额度 | 手动和自动**共用** `post` 桶（3/分钟，全队）。手动**绕过**竞赛分配器（`agent_competition.admit` 只对自动生效），所以手动帖会挤掉自动 Agent 已经排到的机会。额度用完时返回"操作预算等待中，动作已保留"，草稿回到 pending，**不会自动重试，也没有倒计时** | `agent_budget.LIMITS['post']=3.0`、`agent_actions.execute` |
| 失败原因 | ❌ 平台的具体错误被隐藏："平台明确拒绝操作，可修改后重新提交"（400/403/404/409/422），以及"赛事平台操作未完成；敏感上游错误已隐藏"（其他情况）。用户分不清是太长、提及太多、目标被删，还是账号问题 | `agent_actions.execute.failed`、`agent_control._remote_failure` |
| 与自动 Agent 重复回复 | ⚠️ 只有很弱的保护：(a) 同一角色有**进行中**的手动草稿时，自动不会对同一目标再出草稿（`agent_intents.conflict` 的 same_active_target）；(b) 发布后，被回复的作者 2 小时内在自动选目标时 **-4 分**（`recent_targets`），只是降低优先级，不禁止。**没有**全队级的"这条帖子我方已回过"硬拦截：`agent_intents.conflict` / `remember` 对手动草稿直接 `return`；另一个角色仍可回同一条帖子；手动也可以回自动已经回过的帖子。正文去重只比对完全相同的文字（7 天哈希） | `agent_intents.py:56,92`、`agent_targeting.py:302-327` |
| 效果归因 | ⚠️ 手动草稿没有记录 fact_ids、顾虑、角度（`reason='用户手动安排'`），A 的 P1-2 归因统计不进去 | `agent_manual.prepare` |

## 3. 改进建议（按优先级）

文件归属：`agent_manual.py`、`AgentManualActions.tsx` 目前不在协调板的归属表里。建议 F 负责这两个文件；`Records.tsx`、`Agents.tsx` 归 E；`agent_targeting` / `agent_intents` / `agent_actions` 归 B（`agent_actions`、`agent_intents` 不在表里，按 Agent 动作归 B）；写作模板归 A。动手前先在协调板登记。

### P1 省步骤
1. **帖子页加"用角色回复"**：帖子列表和详情里加按钮，先选角色，再跳转到 `/ai/agents/<id>?tab=manual&action=reply&post=<post_id>`，表单自动填好。省掉跨页复制 ID 和 3 次点击。
   改：`Records.tsx`（E）、`AgentManualActions.tsx`（读 URL 参数预填）、`Agents.tsx`（把参数传进去，E）。
2. **AI 起草、人再改**：新增 `POST /api/agents/manual/draft`，复用写作步骤（人设 + 当前生效事实 + 目标帖 + 上级对话），只返回 `body` 和 `fact_ids`，不发布、不进自动调度。前端加「让 AI 起草」按钮，结果填进正文框，人改完再预览。
   改：`agent_manual.py`（新函数）、`agent_routes.py`（路由，共享文件先公告）、复用 `agent_pipeline.write_messages` / `agent_writing`（A、B）、`AgentManualActions.tsx`。
3. **额度忙时自动等待**：碰到 429 或执行器租约时，显示"全队额度：约 N 秒后可发"，倒计时结束后按钮自动可点（`retry_after` 已经在错误里）。角色正在生成时，给一个"等本轮结束后再准备"的提示，并提供"取消本轮"按钮，不用切到总览。
   改：`agent_control._remote_failure`（把 `retry_after` 带给前端）、`AgentManualActions.tsx`。
4. **全队手动发布台**：一页列出 4 个角色、各自状态，以及全队 `post` 桶的剩余额度，可以给不同角色各准备一条，然后按额度逐条确认发布（每条仍要人点，不进自动调度）。
   改：新前端页（E）、复用 `/manual/prepare` 和 `/publish`。

### P2 少出错
5. **预览时检查目标**：`prepare` 时计算 `agent_targeting.blocked_reason`（机器人、层数 ≥7），并查"我方是否已有角色回过这条帖子 / 2 小时内回过这个作者"，结果放进 `preview.warnings`。机器人和层数这两种情况要勾选"仍要回复"才能执行。
   改：`agent_manual.py`、`AgentManualActions.tsx`；复用 `agent_targeting`（B）。
6. **@ 提及上限**：前端实时显示"提及 n / 4（帖子）或 n / 3（回复）"；后端在 `agent_manual._request` 和自动写作校验里都拒绝超限。
   改：`agent_manual.py`、`agent_writing.py`（A）、`AgentManualActions.tsx`。
7. **手动也跑文风检查**：把 `agent_writing.warnings` 的结果作为**不阻断**的提示，在预览里显示。
   改：`agent_manual.prepare`（写进 preview）、`AgentManualActions.tsx`。
8. **失败原因看得懂**：保留 HTTP 状态码，以及平台返回中不敏感的那段错误说明（过滤 token 和 URL），按状态码映射成中文："正文太长 / 提及太多 / 目标帖已删除 / 账号无权限 / 额度等待 N 秒"。
   改：`agent_actions.execute.failed`、`agent_control._remote_failure`、`agent_budget`（B）。
9. **全队去重**：手动发布成功后也写进 `agent_intents` 的台账；自动 Agent 选目标时，把"我方任何角色已回过的帖子"标成 `target_blocked`（不只是 -4 分）。这也顺带完成 B 的 P1-5"同一线程我方最多回复 2 次"。
   改：`agent_intents.py`、`agent_targeting.mark_blocked_targets`（B）。

### P3 效果更好
10. **预览里给上下文**：显示上级对话链（`agent_thread_context`）、作者最近 2–3 条帖子、`classify` 得出的立场和顾虑、雷达优先级。
    改：`agent_manual._post_source`、`AgentManualActions.tsx`。
11. **事实侧栏**：按目标帖的顾虑列出生效事实和 `fact_rebuttals` 的回应句，可以一键插入；草稿记下 `fact_ids` 和 `concern`，供 A 的 P1-2 归因使用。
    改：`AgentManualActions.tsx`（调用 `/api/agents/facts`）、`agent_manual._request`（新增可选字段 `fact_ids`）。
12. **推荐账号**：按话题和人设（兴趣、立场关键词）推荐用哪个角色，比如渡轮和外岛话题推荐 Leah，店铺和停业话题推荐 Ethan。
    改：`AgentManualActions.tsx` 或第 4 项的发布台。
13. **发布后跟进**：发布成功后显示"这条帖子的回复"，并在「收到的通知」里标出"回复的是手动帖"。
    改：`Agents.tsx` 的通知标签（E）、`agent_notifications`（B）。

### P0 人工账号和自动 Agent 分开（D18，用户原话：「人工发帖与回复流程 单独创一个账号」）

**现在就能用的做法**：用 Simon（`simon_mercer`，6503）。这个账号已经绑定（`account_state=ready`），`enabled=False`，还没发过帖。代码里，自动调度的三个环节都要求 `enabled=True`，所以 Simon 不会被自动选中：
- 通知轮询：`agent_control.tick` 里的 observation 循环；
- 生成：`tick` 里的 scheduler 循环、`_generate` 里的 `automatic` 检查；
- 竞赛分配：`agent_competition` 第 79 行。

手动操作这边，`agent_manual.prepare` 只检查账号已绑定、角色不在运行；执行时 `automatic=False`，不看 `enabled` / `mode`。所以 Simon 可以只走手动操作。

这个做法的两个不足：
- **只靠 `enabled=False` 挡着**：有人在页面上点一下「启用此角色」，Simon 就会按 `mode=auto` 开始自动发帖。建议协调先把 Simon 的 `mode` 改成 `review`，作为第二道保险。这是线上配置，要先备份并在协调板公告。
- **收不到通知**：停用的角色不轮询通知，别人回复 Simon，不会进他的「收到的通知」。回复本身仍会被采集器收进帖子库，统计不受影响。

**要改代码的地方**：
14. **角色类型"人工账号"**：给角色加 `manual_only: true`（或 `kind: 'operator'`）字段，效果如下：
    - 调度、生成、竞赛分配都跳过它，但**保留只读的通知轮询**；
    - 「启用 / 暂停」按钮对它不可用；
    - 角色卡、手动操作面板、操作记录里都显示「人工账号」标记；
    - 手动操作面板默认选中人工账号。
    改：`agent_control`（`PROFILE_FIELDS`、`SaveRequest`、`tick` 里的筛选，B）、`agent_competition`（B）、`Agents.tsx` / `AgentManualActions.tsx`（E / F）。
15. **人工回复过的帖子，自动 Agent 不再回**：把第 9 项具体化。`agent_targeting.mark_blocked_targets` 读取已发布的草稿：任何我方账号（包括人工账号）已经回复过的 `target_post_id`，以及同一线程（`root_post_id`）里我方已有 2 条回复的，都标成 `target_blocked='team_already_replied'`。现在只有"2 小时内被回复过的作者 -4 分"。
    改：`agent_targeting.py`（B）、`agent_intents.py`（手动草稿也写进台账，B）。
16. 受管账号互动策略已经生效：4 个自动 Agent 不能点赞或回复 Simon 的帖子，Simon 也不能回复它们的帖子。这样不会出现我方账号互刷，不用改。

### P0 人工和自动分开统计（D21，用户原话：「代发贴和回复，需要在我的系统里，我好统计信息」）

**现在能看到的**：
- 每条手动发布都会写进草稿（`origin='manual'`、`post_id`、`account_id`）、审计事件（`manual_prepared` → `sending` → `published`，`details.manual=true`）、`agent_tracking.record_success` 的账号索引，以及当天计数 `rate.manual_accounts`。
- Agents 页 → Simon →「总览」的发布表现（`agent_feedback`）会列出每条手动帖子的浏览、点赞、回复数，带「手动操作」标记。
- 数据分析页 `?author_id=6503` 可以单看 Simon 这个账号。用专门的人工账号，本身就让"人工"在账号层面分得开。

**做不到的**：
- 没有"人工 vs 自动"的汇总视图：条数、回复率、点赞、关注、观点变化贡献都没有并排对比。
- 竞赛归属**直接丢掉**手动发布：`agent_competition.record_published` 只收 `competition_automatic`，手动帖不进竞赛面板。
- 官方积分（`official_activity` 的 SCORE_NARRATIVE）是全队每小时一条汇总，没有账号信息，所以"观点变化贡献"只能用本地数据估算：回复过我方的 NPC，之后发帖的立场有没有转向 Yes（用 `classify` 判断）。

**改进项**：
17. **数据接口（C）**：新增 `GET /api/agents/origin-stats?hours=&account_id=`，按 `origin`（manual / automatic）和账号分组，返回：
    - 帖子和回复条数；
    - 回复率：收到至少 1 条 NPC 回复的比例，排除机器人和受管账号；
    - 浏览、点赞、转发（取 `counter_snapshots` 最新值）；
    - 新增关注；
    - 观点变化估算（同上，并注明是估算）。
    数据来源：草稿表（origin、post_id、account_id）、`posts`、`counter_snapshots`、`agent_targeting.behavior_bots`。
18. **页面（E）**：在「积分与对账」或「Agent 运营」加一个「人工 / 自动 / 全部」切换和账号筛选；人工账号显示「人工」标记；操作记录列表可以按 `origin` 筛选。
19. **竞赛归属保留手动发布（B）**：`record_published` 不再丢弃手动发布，改为记成 `origin='manual'`，竞赛面板分开显示。

## 4. 这次代发的做法
- **账号**：按 D18，改用人工账号 Simon（6503），不再用 4 个自动 Agent 的账号。草稿已经按 Simon 的人设重写。
- **只走 CTN 工作台**（D21）：通过 Agents 页 → Simon →「手动操作」（背后是 `/api/agents/manual/prepare` → `/api/agents/publish`）。不在 Legit Social 网页上直接发，也不用平台的 Python 包。这样审计、额度、受管账号策略和英文检查都照常生效，记录也都进系统。
- **发布后核对**：每条都确认系统里查得到：草稿 `state=published`、有 `post_id`、`account_id=6503`、审计事件有 `published`，Simon 的发布表现里出现这条。
- 上面第 5 项和第 6 项系统还没做，所以由 F 在起草时用只读脚本人工检查：
  - 目标不在 `behavior_bots` 里；
  - 层数 < 7；
  - 我方没有回过这条帖子；
  - @ 数为 0；
  - 字数 ≤ 500；
  - `agent_writing.warnings` 为空；
  - 只用当前生效的事实。
- 只读脚本放在 F 的 scratchpad：`draft_inputs.py`、`shortlist.py`，都是 `BEGIN READ ONLY` 后回滚。

## 5. 第一批实际代发记录（2026-10-22 02:49–03:12 悉尼）

用 Simon（6503），全部通过工作台 Agents 页 → Simon →「手动操作」或「操作记录」发布。每条都核对过：
- 草稿 `state=published`、`origin=manual`、`account_id=6503`；
- 审计有 `manual_prepared → sending → published`；
- `posts` 表里作者是 6503，`reply_to_post_id` 正确；
- Simon 的发布表现（`feedback.actions`）里有这条，标为 manual。

| # | 动作 | 目标 | post_id | 发布时间（UTC） |
|---|---|---|---|---|
| 1 | 新帖 | — | 153678 | 15:51:21 |
| 2 | 回复 | #152771 @the_quiet_heron | 153820 | 15:55:02 |
| 3 | 回复 | #152755 @danreed_ | 154011 | 15:59:37 |
| 4 | 回复 | #152733 @shane.marsh | 154199 | 16:04:35 |
| 5 | 回复 | #152688 @Ben_is_so_amber | 154526 | 16:12:20 |

- 原定的第 6 条（回复 #152690）没发：从起草到发布的约 40 分钟里，Leah 的自动 Agent 已经回了这条（post 152829）。
- 第 1 条新帖发出 20 分钟内有 3 次浏览、3 个赞；其余几条采集时还是 0 或未采样。

**过程中的实际情况**：5 条用了约 23 分钟，中间重载停了约 3 分钟。每条大约点 8–10 下，另外还碰到这些：
- 1 次 409「统一执行器正在执行其他动作」；
- 7 次 429「全队自定义额度等待中，约 N 秒后继续」；
- 1 次维护开始，按钮被禁用；
- 2 次点击没生效（元素已刷新；窗口在后台没渲染）；
- 1 次下拉框误选成「点赞」，因为正文框消失了才发现，没有提交。

**新发现**：
- **全队发帖额度只有每分钟 1 条**：「额度管理」里 `post` 桶设了 `configured_cap=1`，平台实际允许 3 条。手动和自动抢同一个名额，当时还有 2 条自动草稿在排队，手动几次都被自动抢先，只能盯着 `retry_after` 掐点再点。协调已决定（D23）由 B 改成每分钟 2 条。
- **等待提示看得懂**：额度等待和执行器忙这两种提示都写明了原因和秒数，但按钮不会到点自动重试。第 2 节 E4 说的"平台错误被隐藏"这次没碰到。
- **对自己的操作误报"角色正在运行"**：自己的预览或发布占着角色租约时，页面也显示「角色正在运行」，还建议去取消决策，容易误导。
- **刷新后要换地方继续**：重载后刷新页面，「手动操作」面板里的预览丢了，要到「操作记录」里找到那条待确认草稿再发。草稿本身不会丢，这一点很好。
- **动作下拉框容易选错**：antd 的虚拟列表一共 8 个选项，包括删除、取消关注这类危险动作，键盘和点击都可能选错。

**新增改进项**：
20. **手动发布排队**：额度不够时，不是返回 429 让人再点，而是可以选择"排到下一个空位自动发"。人已经确认过，系统等到有名额就发，仍然只发一次，不进自动调度。另一种做法：手动操作预留一个优先名额，比如每 2 分钟 1 个。
    改：`agent_actions.reserve` / `agent_budget.reserve`（B）、`agent_manual`、`AgentManualActions.tsx`。
    **后端已实现（D26，未部署）**，用的是 D 的 `publish_priority`，人工优先级为 0：
    - 接口：`POST /api/agents/manual/queue {draft_id, body?}` 确认排队，`POST /api/agents/manual/queue/cancel {draft_id}` 取消排队。
    - 字段：草稿上新增 `manual_queue`，含 `state`（waiting / sent / expired / cancelled / stopped）、`next_attempt_at`、`attempts`、`expires_at`、`reason`、`post_id`。
    - 时效：调度器每 2 秒检查一次，到点后按 `retry_after` 重试；15 分钟内没发出就过期，草稿保留为待确认。
    - 只发一次：只有本地拒绝、草稿仍为 pending 时才重试。结果不确定或平台明确拒绝都停止排队，不重发。
    - 前端还没接，交给 E。
21. **倒计时按钮**：用错误里的 `retry_after` 显示倒计时，到点按钮自动变成可点。
    改：`AgentManualActions.tsx`、`Agents.tsx` 的草稿卡（E）。
22. **区分两种"运行中"**：分开显示"本条手动操作执行中"和"自动生成运行中"，前者不提示取消决策。
    改：`AgentManualActions.tsx`、`Agents.tsx` 的 `runningLabel`（E）。
23. **人工账号只给需要的动作**：人工账号的动作选择改成「发帖 / 回复」两个分段按钮，其他动作收进「更多」。或者按角色的 `allowed_actions` 过滤选项。
    改：`AgentManualActions.tsx`。
24. **面板恢复待确认草稿**：打开「手动操作」面板时，如果有 `origin=manual`、`state=pending` 的草稿，直接显示在面板顶部，可以继续确认或放弃，不用去「操作记录」里找。
    改：`AgentManualActions.tsx`。
25. **发布前自动复查目标**：确认执行时，后端再检查一次"我方有没有账号已经回过这条帖"。这次起草到发布间隔约 40 分钟，就有 1 条被自动 Agent 抢先回了。这是第 5 / 15 项的执行时版本。
    改：`agent_actions.execute`（B）。

## 6. 改进项状态（2026-10-22 04:40，线上 git `5d0b1a6`）

已上线的相关改动：
- **F：第 20 项人工排队。** 部署记录为 `unified-20261022c`，04:23 完成，结果 ready；接口 `/manual/queue`、`/manual/queue/cancel`，调度器每 2 秒检查一次。
- **D：`publish_priority`。** 人工 = 0，高优先级的发送等额度时会占住下一个名额。
- **B：`mark_blocked_targets` 新增 `thread_reply_limit`** ——我方在同一线程最多 2 条回复，人工账号也计入；另有 `answered_recently`，同一作者同一顾虑 6 小时内不再回答，读取所有已发布回复，人工帖也算在内。所以 Simon 回过的帖子，自动 Agent 6 小时内不会再回同一顾虑（第 9 / 15 项的主要部分）。

| # | 项目 | 负责 | 状态 |
|---|---|---|---|
| 1 | 帖子页"用角色回复" | E（页面）+ F（URL 参数预填） | 未开始 |
| 2 | AI 起草、人再改 | F（接口）+ A（写作模板）+ B（流水线） | 未开始；要先和 A、B 定复用方式 |
| 3 | 额度忙时自动等待 | F 后端 ✅（即第 20 项）；E 前端 | 前端未做 |
| 4 | 全队手动发布台 | E | 未开始 |
| 5 | 预览时检查目标 | F（`agent_manual.prepare`） | **✅ 后端完成（D29，未部署）**；前端"仍要回复"勾选框交给 E |
| 6 | @ 提及上限 | F（手动后端）+ A（自动写作）+ E（计数） | **✅ 手动后端完成（D29）**；自动写作（A）和前端计数（E）未做 |
| 7 | 手动也跑文风检查 | F（预览）+ E（显示） | **✅ 后端完成（D29）**；前端显示交给 E |
| 8 | 失败原因看得懂 | B（`agent_actions` / `_remote_failure`） | 未开始；这次实际碰到的等待提示都看得懂 |
| 9 | 全队去重 | B | **大部分完成**：同一线程最多 2 条、同一作者同一顾虑 6 小时，都包括人工帖；剩下"超过 6 小时同一帖仍可能被再回" |
| 10 | 预览里给上下文 | F + E | **✅ 后端完成（D29）**：`preview.context`；前端显示交给 E |
| 11 | 事实侧栏和 fact_ids | F（`prepare` 接收 `fact_ids` / `concern`）+ E | **✅ 后端完成（D29）**；事实侧栏页面交给 E |
| 12 | 推荐账号 | E | 未开始；人工只用 Simon 以后，重要性下降 |
| 13 | 发布后跟进 | E + B | 未开始；Simon 停用了自动，不轮询通知，回复只能在帖子页看 |
| 14 | 角色类型"人工账号" | B + E | 未开始；目前靠 `enabled=False`，建议把 `mode` 改成 review 作为第二道保险（待协调） |
| 15 | 人工回复过的帖，自动不再回 | B | **大部分完成**（同第 9 项） |
| 16 | 受管账号互相不互动 | — | 已有，不用改 |
| 17 | 人工 / 自动统计接口 | C | 未开始 |
| 18 | 人工 / 自动统计页面 | E | 未开始 |
| 19 | 竞赛归属保留手动发布 | B | 未开始；`record_published` 仍丢弃手动发布 |
| 20 | 手动发布排队 | F | **✅ 后端已上线**；前端按钮给 E |
| 21 | 倒计时按钮 | E | 未开始 |
| 22 | 区分两种"运行中" | E | 未开始 |
| 23 | 人工账号只给需要的动作 | E | 未开始 |
| 24 | 面板恢复待确认草稿 | E | 未开始；后端已有数据：`origin=manual`、`state=pending`、`manual_queue` |
| 25 | 发布前自动复查目标 | F（排队重试前复查）+ B（`execute` 通用复查） | **✅ 排队部分完成（D29）**；直接点「确认执行」走的 `execute` 还没复查（B） |

### F 下一批建议（只改 `agent_manual.py`、它的测试，以及 `agent_routes.py` 的 `ManualPrepareRequest`）
按"少出错"优先：
1. **第 5 + 25 项：目标复查。**
   - `prepare` 时复用 `agent_targeting.blocked_reason`，返回 `preview.warnings`：`bot_like_author`、`thread_too_deep`、`thread_reply_limit`、`answered_recently`、`not_persuadable`，再加上"我方已有账号回复过这条帖"。
   - 机器人、层数和"我方已回"三种情况，执行和排队都要 `acknowledge_warnings=true` 才放行。
   - 排队每次重试前再查一遍；发现被抢先，就停止排队（`stopped`，原因写"我方已有账号回复"）。
2. **第 6 项：@ 提及上限。** `agent_manual._request` 里，帖子最多 4 个、回复最多 3 个，超过就直接拒绝。
3. **第 7 项：文风检查。** 把 `agent_writing.warnings` 的结果写进 `preview.style_warnings`，只提示，不阻断。
4. **第 10 项：预览上下文。** 读本地 `posts` 表，`preview.target_post` 增加：
   - 上级对话链，最多 6 层；
   - 作者最近 3 条帖子；
   - `classify` 得出的立场和顾虑；
   - 当前层数。
5. **第 11 项：可选字段。** `prepare` 可以带 `fact_ids`（只接受生效事实）和 `concern`，写进草稿，供 A 的归因使用。

E 的前端按钮（第 1、3、21–24 项）可以和这批并行做，因为接口字段不冲突。第 2 项 AI 起草要先和 A、B 商量好，放在后面。

### D29 已完成（未部署）：给 E 的接口说明
`POST /api/agents/manual/prepare` 新增 3 个可选字段，不传时和以前完全一样，旧请求的幂等标识也不变：
- `acknowledge_warnings`（布尔）：确认仍要回复有风险的目标；
- `fact_ids`（字符串列表，最多 6 个）：只接受生效中的事实，否则返回 400 并列出不可用的编号；
- `concern`：取值是 `fact_bank.CONCERNS` 之一或 `none`。

另外有这些规则：
- **@ 提及上限**：帖子和转发最多 4 个，回复最多 3 个，超过返回 400。
- **需要确认的目标**（只对回复）：作者疑似机器人（`bot_like_author`）、对话已到第 8 层或更深（`thread_too_deep`）、我方已有账号回复过这条帖子（`team_already_replied`，包括正在发送中的草稿）、暂时无法核对（`target_check_unavailable`）。碰到这些而没有确认时，返回 **409**，不创建草稿，错误文字会列出原因。前端显示"仍要回复"勾选框，用户勾选后带 `acknowledge_warnings: true` 重新准备。用户确认过的项目记在草稿的 `manual_warnings_acknowledged`。
- **只提示、不阻断的目标情况**：`thread_reply_limit`（这个线程我方已有 2 条回复）、`answered_recently`（6 小时内已回答过这位作者的同一顾虑）、`not_persuadable`（对方已偏 Yes 或话题无关）。

预览（`preview`，也存进草稿的 `manual_preview`）新增：
- `warnings`：`[{code, blocking, message}]`；
- `style_warnings`：文风提示，不阻断，草稿本身的 `style_warnings` 仍然是空的；
- `context`，只对回复和转发提供：
  - `depth`、`stance`、`concerns`；
  - `thread`：上级对话，最多 6 层，从根帖开始；
  - `author_recent`：作者最近 3 条帖子，含疑似凭据的条目会被去掉。

排队重试前会复查：机器人、层数、我方已回这三种情况。如果出现了准备时没确认过的项目，排队停止（`stopped`），原因写明"目标情况已改变"，不会发送。
