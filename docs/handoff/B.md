# B 会话交接：Agent 动作策略 / 目标池 / 决策 / 排班

交接时间：2026-10-22。交接原因：D44，由协调会话的子 agent 接手。

## 1. 现状一句话

第五窗口这批代码**已全部写完，未部署**，定向测试全部通过（见第 5 节）。交接时，`test_agent_*` 全组测试正在后台跑，结果写在 `C:\Users\R2616\AppData\Local\Temp\claude\C--Users-R2616-Desktop-CTN\1481fd0a-e6c2-4d19-9a2f-d3da80f514fb\scratchpad\agent_tests8.log`；拿不到这个文件时，就按第 5 节的命令重跑。**没有做到一半的改动，也没有存补丁。**

## 2. 已完成、未部署（第五窗口）

| 项 | 文件 / 函数 | 要点 |
|---|---|---|
| 2:1 配比（D35/D38） | `agent_decision.next_mix`，`POST_MIX_MIN_SAMPLES = 3`；`agent_control._generate` 里的 `forced_mix`，草稿字段 `mix_forced` | 最近两条都是原创就回复，否则发原创；没有合格的回复目标时改发原创；历史少于 3 条不强制。旧的 `must_post` 和 `forced_post` 已删除 |
| 回复延迟 3–10 分钟 | `agent_control.REPLY_DELAY_SECONDS = (180, 600)` | 只对自动回复生效；延迟期间不立即发布，到点由 tick 发布 |
| 只回市民（校准口径） | `agent_targeting.non_citizens`（签名不变，A 的 `post_attribution` 在用）、`citizen_accounts`、`_sequential_and_artefact_authors`；常量 `CITIZEN_MAX_WRITES = 24`、`RING_MIN_ACTIONS`、`SEQUENTIAL_NAME_MIN = 5`、`PRE_COMPETITION_MAX_ID = 2343`、`LLM_ARTEFACTS` | 口径来自 `reports/generated/deep-analysis/citizens.md`：整个数据集写入少于 25 条、非认证、不是转发圈（≥8 次动作且一半以上是转发）、没有成串编号（同一模式 ≥5 个且组里没有开赛前账号，排除平台的 `i_have_NN_x`）、没有 LLM 痕迹。线上 6862 个账号里 5363 个是市民 |
| 机器人口径统一 | `agent_targeting.bot_accounts` | 读 `account_annotations.is_bot=1`，只有 `duplicate_text` 的旧行忽略。和 C 的约定：is_bot = behavior，或下面四个特征命中 ≥2 个 |
| 同一市民一天一个号 | `taken_citizens`，原因 `citizen_taken`；`mark_blocked_targets(..., agent_id)` | 24 小时窗口，本号自己可以继续回 |
| 抢先回复 | `competitor_replies`、`contested_posts`、`_created_at`；原因 `crowded`（≥11 条别队回复）；`annotate` 里的 `fresh_uncontested`（帖子不到 10 分钟且无人抢先，+4）和 `contested`（−3） | |
| D37/D40 排班 | **新文件** `ctn_data/agent_schedule.py`：`DEFAULT_SCHEDULE`、`validate`、`current_shift`、`next_start`、`hourly_limit`、`gate`、`may_publish`；设置项 `settings['shift_schedule']` | A 组 5554 和 6464：06–13、16–22，每小时 8 条。B 组 5832 和 6494：12–17 每小时 8 条，21–06 每小时 4 条。高峰 06–08、09–11、12–14 额外 +2。支持 N 组。不在任何组里的账号不受限制。只在自动运行时拦截：`_generate` 开头调用 gate，tick 的发布受 `may_publish` 限制（有 15 分钟宽限），tick 不当班不提交生成。通知照常读 |
| D39 长时间活跃特征 | `agent_targeting.bot_feature_flags(db_path, *, now=None)`，常量 `FEATURE_*` | 四个 reason：`always_on`、`high_volume`、`fast_reply`、`regular_rhythm`；metrics 键：`active_hours`、`writes_per_day`、`max_hourly`、`reply_median_s`、`interval_cv`。缓存 600 秒，出错直接抛异常，不返回空。字段名已和 C 约定，**不要改**。C 负责在 AnnotationWorker 里接入，并加 `bot_metrics_json` 列（C 改 db.py 和 postgres.py） |
| @市民 | `agent_targeting.mention_candidates`、`mentioned_recently`；`agent_control._staged_decision` 只在 action=post 时设置 `decision['mention_candidates']`，草稿 meta 里记录用户名 | 最多 2 个：过去 1 小时提过同一顾虑的市民，同一人每天只 @ 一次，回复不带。**写作提示里怎么用这个字段归 A**（已发消息给 A） |
| 低浏览量 / 线程上限 / 被屏蔽资料 | 前几个窗口已经上线：`low_reach`、`thread_reply_limit`、`mark_decided`、`effective_actions`、`decision_problem` | 本批没有改它们的逻辑 |
| 提示词 | `agent_control._generate` 单次调用流程里 target_blocked 原因的说明（中文）；`agent_pipeline.DECIDE_SYSTEM` 的原因列表加了 `citizen_taken` | `crowded` 还没加进 DECIDE_SYSTEM，已请 A 加 |
| 截断 | `_generate` 里 reason 截到 500 字、memory 截到 4000 字 | 第四窗口已上线 |
| UNFOLLOW_BOTS | `docs/UNFOLLOW_BOTS.md` | 只列出 11 个对象，**没有执行**，由用户本人决定 |

## 3. 文件归属与交接状态

- `agent_pipeline.py` **已交给 A**（消息 bfb0f781）。B 本批只改了 DECIDE_SYSTEM 的一行。
- B 负责的文件：`agent_targeting.py`、`agent_decision.py`、`agent_control.py`（除 A/D 登记过的段落）、`agent_schedule.py`（新文件），以及测试 `test_agent_targeting.py`、`test_agent_schedule.py`（新文件）、`test_agent_pipeline.py` 中 B 写的部分。
- 本批修改过的测试：`tests/test_agent_pipeline.py`（`next_mix` 相关、2:1 集成测试、mention、截断）、`tests/test_agent_targeting.py`（市民、抢先回复、features、自检）、`tests/test_agent_schedule.py`（新文件）。

## 4. 剩下的任务（按顺序）

1. 确认 `agent_tests8.log` 全组测试通过。如果有失败，多半是测试样例的作者写入超过 24 条，或者样例里有成串编号用户名，导致被判成非市民：改样例文字或作者，不改断言。
2. 把协调板上 B 的状态改成 green，告诉协调会话可以纳入第五窗口。
3. 上线约 2 小时后复测：`.venv/Scripts/python.exe -X utf8 scripts/measure_kpis.py --label after-window5 --hours 2`。重点看：原创和回复是否接近 2:1；回复对象是否都是市民；我方 4 个账号在 `bot_feature_flags` 里是否不再出现 `always_on`、`fast_reply`、`regular_rhythm`（`high_volume` 只做标注，可以接受，D40）。
4. 等 C 接好 `bot_feature_flags` 后，核对 `account_annotations.bot_reasons_json`、`is_bot` 和协调会话 Blue（`blue_reports.py`）的读取方式一致。
5. 待定：D23（post 额度改成每分钟 2 条）被 B 会话的权限检查拦下，已转告用户本人在"额度管理"里修改。**被拦下的操作不能由别的会话代办。**

## 5. 测试命令

```bash
.venv/Scripts/python.exe -m unittest tests.test_agent_targeting tests.test_agent_schedule tests.test_agent_pipeline tests.test_post_attribution
```

```bash
.venv/Scripts/python.exe -m unittest discover -s tests -t . -p "test_agent_*.py"
```

```bash
.venv/Scripts/python.exe -m compileall -q ctn_data
```

交接前最后一次定向测试：targeting + schedule + post_attribution 共 30 个，pipeline 共 43 个，全部通过。

## 6. 关键设计决定（为什么）

- **2:1 用"最近两条"判断**，不用"最近 3 条里有 2 条"，后者会退化成 1:1（D38）。
- **排班默认值不是严格的"每班 6 小时"**：要同时满足"始终有人在线"和"每人每天连续休息 ≥6 小时"，两组的休息时段不能重叠。按严格 6 小时排，会有一组只能休 4–5 小时（D40 已同意）。
- **排班只拦自动运行**，用户手动运行不受限制。
- **只命中一个特征的账号只做标注**：例如 `fast_reply` 单独会命中 225 个低量市民账号；A 组按每小时 8 条也会触发 `high_volume`（D40 接受）。
- **被屏蔽的资料立即记为已处理**（`mark_decided`）：只写两行小数据，不重写整份状态，以免锁超时；context_only 的资料除外。
- **浏览量数据缺失的帖子不按低浏览量屏蔽**：98% 的新帖都有浏览数。
- **对线上库跑脚本时**，先把 `db.initialize` 和 `postgres.initialize_postgres` 替换成直接抛错的函数，再 `BEGIN READ ONLY`（协调板规则 6）。

## 7. 交接之后的更新（以此为准）

- **全组测试结果**（`agent_tests8.log`）：`test_agent_*` 共 934 个，1 个出错，其余通过；相关模块 47 个通过；compileall 通过。
  - 出错的是 `tests.test_agent_review_integration.ReviewDeliveryTests.test_disabled_review_allows_emoji_faces_and_symbols_without_ai`。这个测试走单流程，连发 4 条 post。按 2:1 规则，连发两条原创后，只要有合格的回复目标，下一轮就只允许 reply，所以模拟模型返回的 post 被判为"动作无效"。
  - **线上行为是对的（D38），只改测试**：在这个测试里把账号的 `allowed_actions` 设为 `['post']`。这样配比轮到 reply 时找不到可用的 reply，会退回发原创，断言不用改。
  - 这次测试开始得比 A 最后几次改动早。修完上面这个测试后，要在合并后的代码上重跑一遍全组测试。
- **A 已完成，不用再跟进**：DECIDE_SYSTEM 已加 `crowded`；写作 payload 已用 `decision['mention_candidates']`（MENTION）；回复带 @ 会被 `local_issues` 打回，原创帖超过 2 个 @ 也会打回。A 还改了 `agent_control.py` 里的一行：`local_issues(body, action, recent, chosen)`。A 已交接，见 `docs/handoff/A.md`。
- `agent_pipeline.py` 最后由 A 交回给 B。B 已交接，所以它现在由协调会话的子 agent 接手。

## 8. 未送达的消息（已不需要）

交接时，B 会话因为"跨会话消息过多"被系统暂停发信，下面这条还没发出。

**给 A 的确认：** `non_citizens(db_path, author_ids) → frozenset` 和 `bot_accounts(db_path) → frozenset` 的签名和语义保持稳定；`agent_pipeline.py` 已经交给 A；请在 DECIDE_SYSTEM 的原因列表里加上 `crowded`。
