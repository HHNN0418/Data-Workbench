# 积分与分析实施报告

核验日期：2026-10-19。模块：`ctn_data/imports.py`、`scoring.py`、`analytics.py`。生产依赖仅 Python 标准库。

## 1. 重读并落实规则

来源：[2026 Scoring Guide](https://capturethenarrative.com/info/competitions/2026/scoring/)；争议以[正式规则](https://capturethenarrative.com/info/competitions/2026/rules/)为准。规则快照通过 `/api/rules` 返回来源、核验日期及完整参数；后续组织方改动需重新核验。

- Red：浏览1、点赞5、转发10、回复15、关注20、取关−20；去重单位为帖子/公民/会话，全队每日仅前500次浏览计分。
- 观点变化：疫苗正向160/贡献池80；反向−80/−40；中性与其他观点20/10。贡献按全部贡献帖子数等分；可无决定性帖子。不同帖可让同队同时获决定性与贡献分。
- 悉尼日期：10月19日为Day 0，10月26日午夜起Red×2，11月1日午夜起×4，负分同乘。按入账时间取倍率；浏览上限及Blue同目标队伍日计数按悉尼午夜重置。
- Blue：帖子首报25、其余前五队5；同目标队伍当天第1–5次全额、第6–10次半额、11次起10%。裁决等待从首报时间算，3/1.5/0.75/0.375/0.1875小时（2026-10-20下午官网更新）。判真实暂停举报6小时，临时隐藏不算永久隐藏，自己的对象不给分。
- 账号审核：bot/spam原因；裁决时至少1篇永久隐藏帖、100次动作且报告仍未关闭，才给每个报告队15；驳回无分无暂停、以后不补发。3篇被裁决永久隐藏会触发账号停用。
- Polling：Poll 1计票时需≥1分，Poll 2/3需≥7000分；只按Yes/No归一化，52–48及更接近双方合资格队各3000，否则领先方6000。公投平票不给分；赢家达7500分或曾有合资格正向疫苗观点改变/贡献可得20000。
- 公投100000分池权重：每次合资格改变=1；每个贡献=0.5÷该次全部贡献帖数；只包含赢家阵营。不能用贡献总次数代替分母。
- 非NPC互动、热门分、公开计数均不能视为官方积分。提示注入筛查、原帖转发归属、取消点赞不扣分等限制进入规则说明；系统不执行任何发帖、举报或提示注入。

Windows缺少IANA时区库时采用NSW现行DST公式；已测2026年4月/10月切换和比赛倍率午夜。该回退针对当前规则，未来时区法变更需复核。

## 2. 官方CSV与总分对账

- 精确支持 `When (UTC),Type,Message`，支持BOM、引号、逗号和多行Message；整份原始CSV保存至`raw_records`并记录文件SHA256。
- 相同导出文件不重复导入。重叠文件按“标准UTC时间＋类型＋原文”的指纹、同文件出现序号去重；单文件两个合法相同事件保留两条。
- 无官方事件ID时，完全相同的独立事件与重叠导出无法绝对区分；采用各导出的最大重复出现次数，不伪称可恢复唯一事件身份。
- 只有明确记录的实得积分金额进入`points_decimal`。纯NPC事件数、×倍率、贡献次数、累计余额、条件句及多金额歧义不转成积分。多个独立金额即使数值相同也标未解析，正则对同一数字位置的重复匹配会去重。
- 旧版`SCORE_NARRATIVE`不直接归Polling；先识别NPC engagement、Moderation或poll/referendum语义，无明确语义时轨道未知。
- 无法解析时间的行保留原文，`occurred_at=NULL`、`points_decimal=NULL`、状态`invalid_time`，不进入积分对账；格式错误的CSV整体拒绝，避免半导入。
- 实得金额使用Decimal文本保存，支持负数与小数；拒绝NaN/Infinity。总分快照记录观测时间、来源、备注，同时间/来源/金额重复录入幂等。
- 输出总分、已解析金额、分轨金额、未解析数量、覆盖范围、参考余额差与相邻快照区间差额。仅一份总分快照时标`single_snapshot`，表示等待形成区间；`unexplained_decimal`为空，不能称为“有未解释项目”。参考余额减已解析流水单独存`absolute_balance_minus_parsed_decimal`，不作为已证漏分。形成区间后`unexplained_decimal`为最近区间差额；区间使用`(前次时间,后次时间]`，零差额仅标“已观测范围相符”，不证明流水完整。

## 3. 独立情景计算器

接口：`calculate_scenario(payload)`，由本地`POST /api/scenario`调用。返回`mode=scenario,is_official=false`；缺参数返回`status=unknown,points_decimal=null`，已算部分另列`partial_points_decimal`。情景从不写入官方积分流水。

| track / type | 必需输入 |
| --- | --- |
| red | `recorded_at`、`engagement`或`changes`；计入浏览时必须给`views_already_paid_today` |
| red / changes | `view=vaccination或other`、疫苗方向`towards/away/neutral`、`decisive_count`、`contributing_posts`；有贡献时须给`total_contributing_posts` |
| blue / post | `ruling`；判inauthentic时须给`report_rank`、`target_is_own`，合资格时给包含本次的`upheld_against_target_today` |
| blue / account | `reason=bot_spam`、`target_is_own`、`permanently_hidden_posts`、`actions`、`report_open_at_ruling` |
| polling / poll | `poll_number`、计票时`team_score`、`team_side=yes/no`、`yes_votes`、`no_votes` |
| polling / referendum | 有效票与本队阵营；`team_persuasion_weight`或`qualifying_changes＋contributions[{count,total_posts}]`；正权重须给`winning_side_total_weight`，零权重须给`team_score` |

Blue可给`distinct_teams`与`first_report_at`计算计划裁决时点。计数输入是假设已按规则去重的NPC事件，未提交类型假设无该类事件。公开计数对象明确拒绝。循环小数按Decimal默认28位有效数字显示，不假设官方舍入规则。

## 4. 描述统计与可复算结果

- 范围：按作者、明确本队归属/未知归属、帖子发布时间筛选。查询先在SQL限制范围；快照按post_id流式处理，内存只保留单帖历史，不加载整库快照列表。
- 时间序列：发布数、观测数、连续快照增量及有效样本对数；增量归入后一次观测时间桶，缺失保留null，不重复累加累计计数。`bucket_timezone`默认`Australia/Sydney`，可切`UTC`；先按所选时区小时/午夜分桶，`bucket_at`仍存UTC并另标`timezone`。DST切换日按真实23/25小时日处理，秋季重复小时保留不同UTC起点。
- 账号cohort：帖子数、互动覆盖率、各指标最新可见累计、同窗口样本数、均值和中位数；未知队伍保留未知。
- 相同窗口：默认发布后10分钟内基线、基线后60分钟端点、±12分钟容差。仅用真实快照，不插值；没有早期基线或端点则排除并记录原因。
- 相关性：文本长度/互动增长、浏览增长/互动增长的Pearson系数与样本数。使用在线Welford累计全部合资格样本，少于3个样本或变量恒定返回未知；不声称因果。
- 词频：Unicode词元频次与帖子覆盖数，去URL和英文停用词；当前版本文本分析，中文未作专业分词。
- 关系：回复与转发的帖子边、可确认的账号边；数据库中没有父帖时保留目标帖子ID与未知作者。
- 质量：缺计数、无作者/时间、未知队伍、过期快照、非法指标、计数下降、关系缺失、窗口排除原因；计数下降保留，不假设为零或NPC效果。
- `growth`、网络明细默认最多返回1000行，上限10000；返回`total/truncated`。汇总、cohort、时间序列和相关性统计仍覆盖全部所选记录。完整原始posts/counters可用CSV导出复算。
- 剩余内存随所选帖子、时间桶、词汇、账号关系及单帖最长历史增长；该结构消除了“全库历史快照全部驻留内存”的主要风险。无历史快照时增长分析会正确为空。

## 5. 验证记录

命令：`python -m unittest tests.test_imports tests.test_scoring tests.test_analytics -v`。

结果：41项通过。覆盖重复/重叠导入及合法重复行、SQL文本与多行原文、异常CSV原子性、非法时间保留、明确金额/计数隔离、legacy分类、重复金额歧义、Decimal、单快照等待状态/总分区间对账、悉尼午夜和DST、Red上限/负分/贡献池、Blue各档折扣/时延/账号阈值、Polling资格/close-call/公投权重、空数据/缺失/下降/未知关系/同窗口/相关性样本数，以及筛选只处理所选账号、明细截断不改变全样本统计、Sydney/UTC日桶与23/25小时DST日及重复小时分桶（含无IANA回退）。

测试使用隔离临时SQLite与明确构造的测试数据，未写入真实比赛库，未发送任何平台互动。真实计分样本尚未提供；保守解析不声称能解析全部未来日志。
