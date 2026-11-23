# CTN 改进方案：数据 → 分析 → Agent 行为（2026-10-22）

协调会话「多聊天管理与系统优化」整理。下面的数字都来自对线上库的只读查询，查询时间是 2026-10-22 01:25–01:40（悉尼时间）。本方案没有改动业务代码，也没有写库。

## 0. 结论

1. **加分几乎全靠互动，说服分是零。** 已导入的官方日志里，Red 分主要来自浏览、点赞和回复。"向我方转变"的贡献只有 2 次，"转离我方"也是 2 次，相互抵消；直接促成转变的（primary）是 0 次。可分数最高的是说服：每次 160 分，Day 13 起乘 4；赢下公投后还要按说服贡献分 10 万分。这是最大的缺口。
2. **Agent 看不到自己的哪种做法有效。** 系统记录了"发了什么"（草稿、顾虑、fact_ids）和"之后的反应"（计数、回复、官方小时日志），但两者没有关联起来，也没有反馈给决策步骤。
3. **同一份判断各模块各算各的。** 立场、顾虑、机器人、线程深度这些标签，目标池、顾虑雷达、分析页各自现场计算。`post_labels` 表已经建好，但是空的。这样既拖慢性能，也会让各处口径对不上。
4. **生成环节浪费最多。** 过去 8 小时生成成功 467 次、失败 117 次（20%）。失败里有 51 次是"互动目标不在本次阅读资料中"，每次白白耗掉约 66 秒和约 2.4 万输入 token。另外，42% 的成功决策是点赞，点赞不加分（会话 B 已经改掉）。
5. **性能瓶颈在模型调用，不在数据库或 CPU。** 后端稳态只用约 0.11 核，PG 约 2%。单次决策中位数 85 秒，8 小时用了 1060 万输入 token。思考 token 占输出的 82%。目前没有记录缓存命中，所以无法判断提示词缓存是否生效。
6. **Poll 2（10 月 25 日）门槛还有风险。** 现在 4366 分，还差 2634 分。按最近 12 小时 74 分/小时的速度，约 36 小时能够到 7000；如果回落到前天 30 分/小时的水平，就会赶不上。

## 1. 多会话协调（已执行）

- 新建 `docs/SESSION_BOARD.md`，写明文件归属、共享文件、重载规则和各会话状态。已经给 A、B、C 三个会话各发了一条说明，A 和 B 已在按规则更新状态和变更公告。
- 关键风险：
  - 项目不是 git 仓库；
  - `Reload-CTN.ps1` 会部署整个目录；
  - A 之前用 Python 脚本给共享文件打补丁，这种方式会绕过"读后被改"检查。
- **建议（需要你同意）：** 在项目目录 `git init`，用现有 `.gitignore` 并补上 `data/`、`backups/`、`.venv/`、`frontend/node_modules/`、`frontend/dist/`，然后提交一次快照。之后每次重载前提交一次，部署报告里记录 commit。这样有 diff 可查，也能一条命令回滚，不再依赖手工备份目录。这一步不影响运行中的服务。
- **统一重载：** A 已经是 green，在等统一重载。B 和 C 都变成 green 后，由协调会话检查，再请你批准，一次性重载。

## 2. 现状数据（只读实测）

| 项目 | 数值 |
|---|---|
| 官方总分 | 4366（14:24 UTC 浏览器观测）= Poll 1 不分胜负的 3000 + Red 1366 |
| Red 速度 | 前半段平均 30 分/小时，后半段 63 分/小时，最近 12 小时 74 分/小时 |
| Red 构成（已导入日志） | 浏览 340、点赞 70、回复 29、关注 5、转发 4；观点贡献：中立 21、向我方 2、转离 2、primary 向我方 0 |
| 8 小时草稿动作 | 点赞 193、发帖 141、回复 113、关注 12、转发 4 |
| 生成 | 成功 467 次，中位数 85 秒，p90 136 秒；失败 117 次，中位数 66 秒 |
| 失败原因 | 目标不在资料中 51、服务请求失败 17（402 额度）、正文超长 7、两次没通过检查约 6 |
| token（8 小时） | 输入 1060 万（中位数 23.6k/次），输出 123 万，其中思考 101 万 |
| 机器负载 | 后端稳态 0.11 核；PG 容器 2%；库总量约 1.8 GB（raw_records 398 MB、meta 268 MB） |
| 平台额度 | 全队每分钟 3 条帖子/回复/转发，即 180 条/小时；我们实际约 58 次决策/小时。每天浏览上限 500，实际远低于上限 |

## 3. 断点：哪些环节没有接上

| # | 断点 | 现在 | 影响 |
|---|---|---|---|
| G1 | 效果没有回流 | `agent_feedback` 只给原始计数；雷达（A）开始统计"我们回复后的反应"，但只能整体看，决策步骤不知道哪个顾虑、角度或事实有效 | 无法持续改进，说服分一直是 0 |
| G2 | 分析页只给人看 | `analytics.py` 只被 `analysis_cache` → server 使用，Agent 不读取 | 39 秒算出来的结果对 Agent 没用 |
| G3 | 标签重复计算 | `agent_targeting.classify` 是关键词规则，目标池、雷达、C 计划中的分析页各自现算；`behavior_bots` 每次 1.5 秒；`post_labels` 为空 | 慢，而且口径不一 |
| G4 | 机器人识别只用于"不回复" | B 识别出 335 个机器人账号，但没有用于 Blue 举报，也没有用于自我防守 | 漏掉一条得分轨道；我们自己的账号也有被举报的风险（见 P1-5） |
| G5 | 写法和说服研究脱节 | 写作步骤有事实库和检查，但没有按"回应对方具体理由、给出可核对事实"的结构写；还会回复本来就偏 Yes 的帖子 | 官方明确说同意不算改变观点 |
| G6 | 失败浪费 | 目标校验在生成全部完成后才做，一旦失败整轮作废 | 每 8 小时浪费约 1 小时生成时间 |
| G7 | 门槛没有监控 | 总览页不显示"距 7000 还差多少、需要多快" | Poll 2 拿不到就少 6000 分 |

## 4. 研究依据：发现和对 CTN 的含义

| 来源 | 发现 | 对 CTN 的含义 |
|---|---|---|
| Hackenburg 等，Science 2025 | 最有效的手段是**信息密度**，也就是多给可核对的事实；提示词能提升约 27%，个性化作用很小；但越有说服力，准确性越低 | 每条帖子给 2–3 个来自事实库的具体事实，少用模糊表态；保留事实检查这道关，不能为了说服力放松 |
| Costello、Pennycook、Rand，Science 2024 | 针对对方自己说出的理由给出反证，能让信念下降约 20%，两个月后仍然有效 | 回复时先复述对方的具体顾虑，再给对应事实；雷达的顾虑和事实对应表就是做这个用的 |
| Salvi 等，Nature Human Behaviour 2025 | GPT-4 掌握对方的个人信息后，辩论胜率明显提高 | 不需要人口学信息；把目标作者最近 2–3 条帖子放进回复上下文，就是我们能拿到的"个性化" |
| Chuang 等，NAACL Findings 2024 | LLM 模拟的人群天然向科学共识收敛；加入确认偏误提示后会分裂 | NPC 很可能也是 LLM：用证据和共识讲道理是顺着它的倾向；嘲讽或施压更可能造成"转离" |
| Debunking Handbook 2020 | "事实—谬误—错误推理—事实"结构；要给出替代解释；不要反复强调谬误 | 写作模板：先给事实，谬误只提一次，再讲它错在哪里，最后回到事实 |
| PMIYC（Bozdag 等，2025） | 用"说服者 / 被说服者"多轮对话自动测量说服力和易受说服程度 | 可以搭一个离线评测：用真实 NPC 帖子做被说服者的人设，上线前先比较写作提示词的效果 |
| Li 等，WWW 2010（LinUCB） | 带上下文的 bandit 比不看上下文的方法点击率高 12.5% | 用 Thompson 抽样在"顾虑 × 角度 × 发帖/回复"之间分配决策，代替靠提示词笼统地要求"自主优化" |
| DSPy（MIPROv2 / GEPA） | 给定指标，可以自动搜索更好的提示词 | 等 P1-2 积累出效果标签、P2 有了离线评测之后再用，现在先不做 |
| Pacheco 等，ICWSM 2021 | 根据共同的行为痕迹（同文、时间、共同转发）构建协同网络，找出背后由同一方操控的账号群 | 把机器人按"所属队伍"聚成簇。Blue 举报的递减规则按队伍计算，需要这个分组 |
| OASIS（CAMEL-AI） | 开源的社交平台模拟器，有推荐算法和 23 种动作 | 比赛只剩 11 天，搭模拟器成本太高，仅作参考 |
| CTN 官方计分页 | 同一会话里同一观点再次改变会变难；超过 8 层很少回复；转离我方扣 80；Blue 举报全程 ×1，每个被举报队伍每天前 5 次全额 | 目标要分散到更多 NPC；对话层数设上限（B 已在做）；举报宁缺毋滥 |
| OpenRouter 官方文档 | DeepSeek 自动缓存，缓存读取按 0.1 倍价格计费；要保持消息开头稳定；默认按"第一条 system + 第一条非 system 消息"做黏性路由，也可以传 `session_id`；带 `openrouter_in_flight_budget` 的 402 可能附带 Retry-After；`provider.sort=throughput` 或模型名加 `:nitro` 可以按吞吐量选服务商 | 我们的第一条 user 消息每次都在变，黏性路由可能失效，缓存命中率可能很低，需要先测量；402 应该按 Retry-After 等待，而不是笼统地报 502 |

## 5. 改进清单（按优先级；"负责"指协调板上的会话）

### P0（这两天做；直接影响分数或浪费最大）

| # | 做什么 | 负责 | 指标 |
|---|---|---|---|
| P0-1 | **门槛守护**：总览驾驶舱显示"距 7000 还差多少、按当前速度预计几点达到、需要的每小时速度"；低于需要的速度时标红 | C（已经在做总览驾驶舱，顺带加上） | 页面能看到预计达到时间 |
| P0-2 | **目标失败的浪费**：<br>① 资料用短编号（S1–S20）给模型，代码再换回 post_id，减少模型编造长整数 id；<br>② 分段流程在**决策步骤**就校验目标，不合格只重试决策这一步（约 9.5k token），不再整轮作废；<br>③ 被屏蔽的目标不列进 enum（B 已在做） | B | "目标不在资料中"从 51 次/8h 降到 <5 次/8h |
| P0-3 | **测量缓存命中**：`ai_providers._safe_usage` 记下 `prompt_tokens_details.cached_tokens`、`cache_write_tokens` 和 `cost` | B（先在协调板公告，因为 `ai_providers.py` 是共享文件） | 能在审计日志里看到命中率 |
| P0-4 | **提高缓存命中**：消息顺序改成 [固定规则] → [人设 + 生效事实库，同一角色固定不变] → [本轮动态资料]；请求带 `session_id = agent id`，固定黏性路由 | B，在 P0-3 测出基线之后做 | 命中率 >50%，首 token 时间下降 |

### P1（本周；补上"数据 → 分析 → Agent"的回路）

| # | 做什么 | 负责 | 指标 |
|---|---|---|---|
| P1-1 | **统一标注层**：每条帖子只算一次立场、顾虑、是否机器人、线程深度，写进 `post_labels`（或一张专用表），每 5–10 分钟增量更新。目标池、雷达、分析页、反馈都从这里读 | C 负责表和增量任务；判定规则沿用 B 的 `classify` 和 `behavior_bots`，不改逻辑 | `behavior_bots` 从 1.5 秒变成直接查表；分析页 warm 时间（现在 28 秒）明显下降 |
| P1-2 | **效果归因和学习**：每条发布记录特征（顾虑、角度、fact_ids、目标立场、目标是 NPC 还是机器人、层数、长度、时段）。6 小时内统计 NPC 回复和回复立场、点赞、关注，再对照官方小时日志里 Red 的变化，按"顾虑 × 角度 × 发帖/回复"做 Thompson 抽样，把结果写进决策步骤的 `priorities` | A 负责统计（雷达已经有"我们回复后的反应"）；B 负责让决策步骤读取 | 每天一张"哪些做法有效"表；向我方转变的贡献次数上升 |
| P1-3 | **说服写法**：<br>① 只回复犹豫或偏 No 的 NPC；<br>② 回复里复述对方的具体顾虑，给 2–3 个事实库里的事实，按"事实—谬误—错误推理—事实"结构写；<br>③ 回复上下文里加入目标作者最近 2–3 条帖子；<br>④ 同一 NPC 6 小时内同一顾虑最多回复一次 | A 负责写作提示词和事实；B 负责目标池和频次 | 转离我方：0；向我方转变的贡献增加 |
| P1-4 | **Blue 举报（需要你决定做不做）**：用机器人识别结果加协同聚类，生成高把握的候选清单，**每条由你在页面上确认后再举报**。排除认证账号和本队账号；每个被举报队伍每天只提交前 5 条；一旦被裁定"真实账号"，就会停举 6 小时，所以只报把握最高的 | 新会话（新模块和新页面，不和现有文件冲突） | 被裁定为不真实的比例 >85% |
| P1-5 | **防守**：B 的数据显示，我们自己的账号在行为上像机器人（回话率 62–76%、约 5 分钟就回、常在深线程）。我们的账号操作都已超过 100 次，所以只要有一条帖子被永久隐藏，再有人举报这个账号，就会被封。<br>措施：回复延迟加随机等待；同一线程我方最多回复 2 次；层数设上限（B 在做） | B | 被举报或隐藏的帖子为 0 |

### P2（性能；按会话 C 的四个阶段推进，再补两项）

- C 的第一到第四阶段照原计划做：总览接口瘦身、分析页分页、轮询调整、DataTable、分包、`pg_trgm`、`shared_buffers`、`pg_stat_statements`、保留期限、`checkpoints` 迁表、`meta` VACUUM。
- 补充一：**分步使用不同的思考强度**。写作和检查这两步，把 reasoning 设为 low 或关闭；决策这一步保留 auto。思考 token 占输出的 82%，这是最直接能省下的时间。
- 补充二：**吞吐量**。402 处理好之后，每个角色可以同时有 2 个生成在跑；决策调用可以试用 `provider.sort=throughput`，对比每秒 token 数，效果不好就撤回。

## 6. 顺序和分工

1. **现在**：B 完成全量测试，C 完成第一阶段，两者变 green 后由协调会话统一检查，再请你批准**一次**重载，把 A、B、C 的改动一起上线。
2. **重载后**：B 接手 P0-2、P0-3、P0-4；C 在驾驶舱加 P0-1，然后做 P1-1；A 做 P1-2 的统计和 P1-3 的写作部分。
3. **P1-4 Blue 举报**：你同意后再开一个新会话。
4. 每完成一项，用同样的只读查询复测：失败率、token 用量、缓存命中、转变次数、Red 每小时得分。

## 7. 需要你决定

1. 要不要 `git init` 并提交一次快照（只是加上版本控制，不影响服务）？
2. P1-4 Blue 举报做不做（每条人工确认）？
3. 要不要给决策调用设 `max_tokens`？之前你说过 402 先不管，但它每 8 小时会造成约 17 次失败。

## 来源

- [Hackenburg et al., The Levers of Political Persuasion with Conversational AI (arXiv 2507.13919)](https://arxiv.org/pdf/2507.13919) · [Oxford 新闻稿](https://www.ox.ac.uk/news/2025-12-11-study-reveals-how-conversational-ai-can-exert-influence-over-political-beliefs)
- [Costello, Pennycook, Rand, Durably reducing conspiracy beliefs through dialogues with AI（MIT Sloan 介绍）](https://mitsloan.mit.edu/node/48296)
- [Salvi et al., On the conversational persuasiveness of GPT-4 (arXiv 2403.14380)](https://arxiv.org/abs/2403.14380v1)
- [Chuang et al., Simulating Opinion Dynamics with Networks of LLM-based Agents (arXiv 2311.09618)](https://arxiv.org/abs/2311.09618)
- [The Debunking Handbook 2020](https://sciencelearning.umd.edu/files/2020/10/DebunkingHandbook2020.pdf)
- [Bozdag et al., Persuade Me if You Can (arXiv 2503.01829)](https://arxiv.org/abs/2503.01829)
- [Li et al., A Contextual-Bandit Approach to Personalized News Article Recommendation](https://microsoft.com/en-us/research/wp-content/uploads/2016/02/p661.pdf)
- [Pacheco et al., Uncovering Coordinated Networks on Social Media (ICWSM 2021)](https://ojs.aaai.org/index.php/ICWSM/article/view/18075)
- [OASIS: Open Agent Social Interaction Simulations (arXiv 2411.11581)](https://arxiv.org/abs/2411.11581)
- [OpenRouter Prompt Caching](https://openrouter.ai/docs/features/prompt-caching) · [OpenRouter Errors](https://openrouter.ai/docs/api-reference/errors) · [OpenRouter Provider Routing](https://openrouter.ai/docs/provider-routing) · [OpenRouter Structured Outputs](https://openrouter.ai/docs/structured-outputs)
- [Cybertec：pg_trgm GIN 和 GiST 的对比](https://cybertec-postgresql.com/en/?p=19463)
- CTN 官方页面快照：`.tmp/ctn-official-scoring-20261021.md`、`.tmp/ctn-official-rules-20261021.md`、`.tmp/ctn-official-getting-started-20261021.md`
