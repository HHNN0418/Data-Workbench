# 采集与历史入库实现报告

实现依据：`docs/CONTRACT.md`、`docs/PLAN.md`；生产代码仅用 Python 标准库。接口均为公开、匿名 GET，没有登录、发帖、点赞、view、举报、反馈或远程修改能力。

## 模块与接口

- `ctn_data.transport.ReadOnlyTransport(conn, **limits).get_json(service, path, entity_id=None)`；`get` 为别名。service 仅 `social`、`news`，主机固定；路径与查询参数逐项白名单。禁止任意 URL、auth 路由、home feed 与写操作。规范 SDK JSON 路径不需要重定向，因此所有重定向一律拒绝，避免 urllib 产生隐式、未计入预算的请求。
- `ctn_data.ingest.save_posts/save_counters/save_accounts/save_news`：遵守合同签名，使用参数化 SQL、savepoint；可嵌入调用方事务。保留 raw provenance、首次/末次观察、正文版本和计数历史。字段缺失保留原值，计数缺失为 NULL，明确的 0 才写 0。公开资料不生成队伍或 NPC 归属。
- `ctn_data.collector.collect_once(db_path, config=None)`：单次有限运行。返回合同字段及 requests/waited_seconds/feeds/scope，errors 为 stage/entity_id/status/message 列表。`completed` 表示本次预算内工作完成，不代表已采集全平台完整历史；每个 feed 的 `cycle_complete` 单独说明回填周期是否结束。

## 有限请求、限流与运行恢复

默认 page_limit=50、max_pages=5（每种 feed）、counter_limit=30、counter_new_share=0.5、account_limit=50、news_limit=10、site_limit=10；默认请求预算160次（重试也计数）、timeout=15秒、重试最多3次、deadline_seconds=120、累计等待预算30秒。配置有类型和上限校验，不接受远程地址或凭据。允许并忽略 server 传入的 team_label/timezone/interval_seconds/read_only_remote 本地配置。

429 按 Retry-After，5xx 与网络错误按有限退避重试。服务端等待超出本次预算时结束，不截短 Retry-After 后提前重试。冷却持久保存到 checkpoints，下次手动 collect 继续遵守。读取 X-RateLimit-* 并按接口组（含不同 post_id 的同一路由）控制等待。每次实际尝试记录 GET 路径、状态、耗时、重试序号、限流及简短错误；日志完全丢弃 query、请求头、请求体和响应错误正文，不存密钥。

官方 SDK 0.5.1 的 `RateLimit` 说明额度连续恢复：相对秒数形式的 `X-RateLimit-Reset` 是恢复整个额度所需的时间，额度为零时下一次请求约需等待 `Reset / Limit`。例如 `Limit=300, Reset=60` 对应约 0.2 秒，不能把每次额度耗尽都解释为暂停 60 秒。直连与代理结果使用同一计算；时间戳形式的 Reset 仍等待到指定时刻，429 的 Retry-After 和已经保存的更长冷却继续生效，原有共享请求间隔也继续执行。

排他运行使用进程内锁与 OS 文件锁，异常退出由 OS 自动释放；锁文件持久存在但不表示仍被锁住。获得排他锁后会将旧 running 记录标为 interrupted。正常、部分失败与中断均记录结束状态。

## 分页、刷新与完整性

Latest 与 Trending 分别保存已成功入库页的 cursor。原始响应、帖子和下一页断点在同一事务提交；坏页不推进断点。Latest 只在周期结束时提升 UTC 创建时间/ID 水位，下一周期从头追至旧水位；周期内新增帖不会被错误水位跳过。

恢复回填时，max_pages 大于1会先抓一页最新头部，再使用剩余页预算继续旧 cursor；这页头部不会提升旧回填周期水位，防止新头部与回填之间出现永久缺口。max_pages=1时优先续传，头部更新可能延迟到下一周期。Trending 的排名 cursor 若已失效，则记错并清空失效 cursor，下一次从头开始。

计数分成“尚无快照的新基线”和“已有快照的历史复查”两个队列，默认各占一半 counter_limit，某队列不足则另一补足；counter_limit≥2且share在0和1之间时，两队列各保留至少一个名额。组内按最近尝试时间轮转，未尝试取first_seen，避免连续新增让旧待采帖子也无限等待。历史复查先行，再与新基线交错执行，预算提前耗尽也优先留下历史重复快照。counter_limit=1且两队列都有候选时持久跨轮交替（中间share值采用交替；share=0/1可显式选择单队列，仍允许空队列补位）。运行指标 counter_sampling 分别记录 selected/attempted/saved。

失败请求也记录尝试时间，避免404对象占满所有刷新名额。先执行计数队列，再用剩余预算 GET 本次未在 feed 中观察到的帖子正文，以捕获后续编辑；不存在/隐藏对象不会被错误记为互动清零或从历史删除。账号也按最近尝试时间轮转。

新闻严格使用 SDK 0.5.0 路径：`/index.json`、`/{sub_url}.json`、`/{sub_url}/articles/{slug}.json`。先保留 front-page 元数据，再按 news_limit 获取完整正文，摘要更新不擦除已保存正文。`news` 运行指标表示元数据 upsert 条数，不能称为全文数量；全文覆盖用 `body IS NOT NULL` 查询，未取全文为 NULL。新闻 published_at 保留剧情时间，不以真实时钟修正。

轮询无法保证捕获两次观察之间出现后被删的帖子、短暂编辑或完整互动明细。新/旧分队列保障复查预算，但不保证每个帖子按固定时间间隔复查；实际间隔取决于候选池、请求成功率、预算和运行频率。若 feed 已用完所有请求/时间预算，本次仍可能没有计数机会。增长分析必须使用实际拥有相同观察窗口的重复快照，未复查不能当作增长0；这不是全平台随机或完整样本。公开 counters 无 NPC 分项、单帖积分；公开账号无队伍/role。热门分数不作为比赛积分。新闻 front page 也不是有承诺的完整历史档案。

## 验证

测试文件：`tests/test_ingest.py`、`tests/test_collection.py`。28项离线 unittest 覆盖事务原子性、版本/快照/NULL、跨来源去重、断点续传、回填头部与水位缺口、失败队列轮转、JSON新闻、429/5xx/网络重试、日志脱敏、预算、跨运行冷却、重定向禁止、OS跨进程排他及进程终止后恢复。新增回归确认：每轮新增6帖而counter_limit=2仍产生历史重复快照；单名额跨轮交替；预算只余一次计数请求时历史快照先保存；新队列为空时历史复查借用全部名额。

PowerShell：

```powershell
& 'C:/Users/R2616/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe' -m unittest discover -s tests -p 'test_ingest.py' -v
& 'C:/Users/R2616/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe' -m unittest discover -s tests -p 'test_collection.py' -v
```

本机 `python` 别名指向不能启动的 WindowsApps 文件，故使用上面的实际运行时。测试临时数据库位于 workspace/tests 的自动清理目录，避免 sandbox 默认 TEMP 的 SQLite ACL 问题。

主 agent 已完成一次真实只读验收（本 agent 未重复 live）：`config.validation.json` 为 page_limit20/max_pages1/counter_limit10/account_limit10/news_limit2/site_limit2、两种 feed 和新闻开启、request_budget35/deadline45秒/等待10秒/重试1次/timeout10秒。报告结果为 completed，27次GET、40个唯一 feed 帖子、10个计数快照、10个账号、61条新闻元数据 upsert、errors=[]。news_limit=2仅允许此次取得最多2篇完整正文；61不能解读为61篇全文。

## 官方依据

- https://pypi.org/project/capturethenarrative/
- https://github.com/capturethenarrative/pypi-capturethenarrative/blob/main/src/capturethenarrative/legitsocial.py
- https://github.com/capturethenarrative/pypi-capturethenarrative/blob/main/src/capturethenarrative/newssites.py
- https://capturethenarrative.com/info/competitions/2026/scoring/

已只读实测公开帖子、counters、账号和新闻 JSON 样例；本模块未调用任何反馈或远程写工具，未启动持续采集。
