# E 交接文档：易用性、导航和页面 UI

- 原会话：E `local_899470fa`。按 D44 交给协调会话的子 agent。
- 写于 2026-10-22。
- 依据：
  - 侧边栏和首页：`docs/PIPELINE_UI_SPEC.md`（取代 `docs/UX_IA_PLAN.md` 第 3 节的旧侧边栏）；
  - 页面规范：`docs/UX_IA_PLAN.md` 第 11–16 节；
  - 接口字段：`docs/UX_IA_PLAN.md` 第 18、21 节，以及本文第 4 节。

## 1. 已经上线
| 内容 | 主要文件 | 窗口 |
|---|---|---|
| S1 导航外壳：侧边栏、面包屑、404、`routes.tsx`；统一配色 `ui/theme.ts`；删除 AIWorkflow | `nav/*` `routes.tsx` `ui/theme.ts` `ui/tokens.css` `main.tsx` | 第三窗口 |
| AI 连接删除前要确认（`ui/ConfirmButton.tsx`） | `pages/AISettings.tsx` | 第三窗口 |
| 新闻页 3 个站内链接改成路由链接 | `pages/Records.tsx` | 第三窗口 |
| S1a 驾驶舱速度预警 | `pages/PaceAlert.tsx`、`Overview.tsx` | 第三窗口 |
| S1b Blue 只读候选页 `/blue` | `pages/BlueCandidates.tsx` | 第三窗口 |
| `/blue` 刷新 404 修复：`SPA_PATHS` 加上路由同步测试 | `ctn_data/server.py`、`tests/test_spa_routes.py` | 第四窗口 |
| 草稿卡"排队发布 / 取消排队" | `pages/Agents.tsx`（DraftCard）、`pages/AgentManualQueue.test.tsx` | 第四窗口 |
| S2 共享组件（目前**没有页面引用**） | `ui/status.tsx`（StatusTag / StanceTag / IdentityTags / RelativeTime）、`ui/layout.tsx`（PageHeader / MoreMenu / Advanced / NextStep / errorHint）、`ui/ui.css`、`ui/components.test.tsx` | 第四窗口 |

## 2. 已完成、未部署（工作区里，等下一个重载窗口）
**S-A：数据流侧边栏**（按 PIPELINE_UI_SPEC 第 2 节，以及 D33、D36）
- 改动的文件：`frontend/src/nav/navConfig.ts`、`nav/Sidebar.tsx`、`nav/nav.css`、`nav/NotFound.tsx`、`nav/navConfig.test.ts`、`nav/AppShell.test.tsx`、`routes.tsx`、`routes.test.tsx`，新增 `pages/StagePlaceholder.tsx`、`pages/StagePages.test.tsx`；`ctn_data/server.py` 的 `SPA_PATHS` 加了 8 个路径。
- 内容：
  - 全链路总览 → ① 采集 → ② 处理 → ③ 分析 → ④ 策略 → ⑤ Agent 执行 → ⑥ 效果 → ⚙ 系统。系统组默认折叠，当前页在系统组里时自动展开。
  - 8 个占位页：/processing、/processing/bots、/strategy、/agents/pipeline、/results/attribution、/results/exposure、/settings、/system/maintenance。
  - 按 D36 不做 /agents/manual；维护页没有任何按钮。
- `nav.css` 里两处手机宽度修复（在线上注入同样的 CSS 验证过）：
  - 页头只留标志，隐藏"数据工作台"文字；
  - 警告的操作链接放到正文下面（antd 6 的类名是 `.ant-alert-actions`）。
- 测试：tsc 通过；`src/nav`、`routes.test.tsx`、`StagePages.test.tsx` 共 74 个测试通过；`tests.test_spa_routes` 通过。前端全量串行的结果见第 6 节。
- 注意：`ctn_data/server.py` 和 `pages/contentResearch.css` 的工作区改动里还有别人的部分（C 的 pipeline 路由；A 正在修 375px 下的溢出），不是 E 的。

## 3. 草稿位置（`docs/handoff/E-drafts/`）
| 草稿 | 文件 | 状态 |
|---|---|---|
| S-C 全链路首页 | `pipeline-home/PipelineHome.tsx`、`pipelineModel.ts`、`pipeline.css`、`PipelineHome.test.tsx` | 按 C 的 `/api/pipeline/status` 实际字段写的，可以直接接入（见第 5 节第 2 步） |
| D41 账号簇页 | `clusters/AccountClusters.tsx`、`AccountClusters.test.tsx` | **按 E 的假设字段写的，要按第 4 节 C 的实际字段改**，改动点列在下面 |
| S2：QueryState 错误提示带下一步 | `queryState-errorHint.patch` | 第四窗口冻结前撤回了，原因是来不及跑全量测试。重新应用后要跑全量，因为网络错误时会渲染 `<Link>`，在 Router 外渲染会报错 |

## 4. 需要的接口字段（都由 C 提供）
- **`GET /api/pipeline/status`**（已做，未部署）：
  - 返回 `{generated_at, overall, stages[6], thresholds}`。
  - stage 的字段是 `{key, label, status(ok|warn|bad|unknown), headline, metrics{}, updated_at}`，unknown 时另带 `error`。
  - 各段 metrics 的字段见 `docs/UX_IA_PLAN.md` 第 21 节，以及首页草稿 `pipelineModel.ts` 里的 `nodeView`。
- **`GET /api/pipeline/history?hours=24`**：E 请 C 做的，用于每段的小走势图，C 还没答复。没有这个接口就不显示走势图。
- **`GET /api/clusters`**（已做，未部署；以 C 的说明为准）：
  - 没有 hours 参数，窗口固定 48 小时，缓存 10 分钟，带 `stale`，冷启动约 20 秒。
  - 返回 `{generated_at, window{hours,from,to}, note, own, clusters[≤30，按 citizen_points_per_hour 降序], clusters_total, clustered_accounts, citizen_accounts, method{…}, stale}`。
  - 簇的字段：`cluster_id` `is_own` `accounts` `members_total` `members[{account_id, username, is_bot, bot_reasons[], writes}]` `writes` `hourly_writes[48]` `kinds{post,reply,repost}` `post_share` `reply_share` `replies_to_citizens_share` `stance{yes,no,undecided}`（比例） `stance_posts` `trending_share` `citizen_replies` `citizen_reposts` `citizen_points_per_hour` `bot_flagged` `bot_share` `bot_reasons{原因: 人数}` `blue_candidates`（可能为 null） `blue_cluster_ids[]` `examples[≤2，{post_id, why, excerpt}]`。
  - **草稿要改的地方**：
    - 请求不带 hours；
    - 把 `own` 拼到表格第一行；
    - `engagement_per_hour` 换成 `citizen_points_per_hour`（列名"市民互动分 / 时"）；
    - `size` 换成 `accounts`；
    - `posts_per_hour` 用 `writes/48` 算；
    - 成员用 `account_id` 和 `writes`；
    - `reasons[]` 换成对象 `bot_reasons{}`；
    - `top_posts` 换成 `examples`（显示 `why`）；
    - 立场是比例，不是计数；
    - 页面顶部那句话用接口的 `note`。
  - **和 Blue 互相跳转**：簇 id 和 Blue 的 cluster_id 对不上。从簇跳到 Blue 用 `/blue?clusters=<blue_cluster_ids 用逗号连起来>`；Blue 页要新增 `?clusters=` 筛选，以及每张卡片上"所属账号簇"的链接 `/analysis/clusters?blue_cluster=<id>`，簇页根据 `blue_cluster_ids` 找到对应的簇并展开。
- 其他已上线的接口：
  - `/api/overview.threshold`（PaceAlert 用）；
  - `/api/blue/candidates`（字段见 `UX_IA_PLAN` 第 18 节 N9）；
  - `/api/nav/status`（N8，可以用在侧边栏徽标）；
  - `/api/insights`（N4，舆论与洞察页）；
  - `/api/accounts` 的 N5 新字段；
  - `/api/posts` 的 N3 字段（stance / is_bot / bot_reasons）。

## 5. 剩下的任务和顺序
1. **S-A 上线**（下一个重载窗口）。上线后用内置浏览器打开 http://127.0.0.1:8765（用户已经登录），在 375px 和 768px 下复查。检查方法：比较 `document.documentElement.scrollWidth` 和 `innerWidth`，并列出 `getBoundingClientRect().right > innerWidth` 的元素。上次 13/14 个页面没有溢出。/ai/analysis 的溢出 A 已经修好（未部署）：`contentResearch.css` 在 ≤900px 和 ≤600px 下改成 `minmax(0,1fr)`，表单项加了 `min-width:0`。**部署后请在 375px 下打开 /ai/analysis →「自动内容分析」复查一次**，A 只在开发服务器上验证过。
2. **S-C 全链路首页**：
   - 把 `E-drafts/pipeline-home/*` 复制到 `frontend/src/pages/`；
   - `routes.tsx` 里 `/` 改成 `PipelineHome`（原来的 Overview 不再用作首页；其中的数据规模和覆盖信息按计划移到采集运行页）；
   - `nav/AppShell.tsx` 用 `useApi('/pipeline/status',{},60000)` 读数据，传给 Sidebar，在每段标题旁显示状态点：stage 和侧边栏组的对应在 `pipelineModel.stageGroups`（outcome 对应 results），全链路总览用 `overall`；
   - 补测试。
3. **D41 账号簇页**：
   - 按第 4 节修改草稿；
   - 在 `navConfig.ts` 的 ③ 分析 里、Blue 候选前面加 `{key:'clusters', label:'账号簇 / 竞品', to:'/analysis/clusters'}`，并加进 `simplePaths`；
   - `routes.tsx` 加路由；`server.py` 的 `SPA_PATHS` 加 `'analysis/clusters'`；
   - Blue 页加 `?clusters=` 筛选和反向链接。
4. **S2 剩余**：
   - 应用 `queryState-errorHint.patch`；
   - 把状态颜色换成 `StatusTag`：Agents 7 处、Operations 5、AgentLogs 2、AgentResearchReference / AgentMemoryHistory / AISettings / AIReview / ReadingSelection / CollectionStatus 各 1 处。`CollectionStatus.test.tsx` 现在检查 `.ant-tag-green`，要改成检查 `[data-tone="ok"]`；
   - 去掉 Overview、Score、Operations 的英文装饰小标题，改用外壳标题：在 `navConfig` 里去掉这三项的 `shellTitle:false`，并改 `navConfig.test` 和 `AppShell.test` 里的相关断言；
   - A 的 5 个页面要等 A 交出后再动。
5. **S-D 段落页**：用真实数据替换占位页。来源：/processing 用 C 的 annotations；/strategy 用 B 的设置和 pipeline metrics；/agents/pipeline 用 pipeline/status 的 execute 段；/results/attribution 用 A 的 post_attribution；/results/exposure 用 measure_kpis 的 exposure 段。
6. **页面重设计**（`UX_IA_PLAN` 第 15 节，S3–S9）：
   - 帖子、账号、新闻：立场、身份标记、按影响力排序，帖子详情只保留两个标签页；
   - 分析页改成 4 个标签加"高级"，旧 `analysis_tab` 值的对照见第 15.5 节；
   - 积分、运行、规则页；
   - Agent 页先纯拆分，再改布局，不动 B 的动作选项；
   - AI 连接页、资料筛选页。
   - 注意 D36：手工发帖已关闭，"人工代发"和 F 的面板改进先不做。
- 未决：用户还没决定"生成文章"的去留，先保持现状（入口在 ③ 分析 › AI 工作台）。

## 6. 测试命令
- 这台机器的 PATH 上没有 node，要用捆绑的运行时：`NODE="/c/Users/R2616/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node.exe"`，然后在 `frontend/` 目录下运行：
  - 类型检查：`"$NODE" node_modules/typescript/bin/tsc --noEmit`
  - 相关测试：`"$NODE" node_modules/vitest/vitest.mjs run --no-file-parallelism src/nav src/routes.test.tsx src/pages/StagePages.test.tsx`
  - 前端全量：`"$NODE" node_modules/vitest/vitest.mjs run --no-file-parallelism`（**必须串行**，约 20 分钟，建议放后台跑；日志里的 "Could not parse CSS stylesheet" 是 jsdom 的警告，不是失败）
- 后端（在仓库根目录）：`.venv/Scripts/python.exe -m unittest tests.test_spa_routes tests.test_server`（venv 里没有 pytest）
- 规则：
  - `routes.tsx` 新增路由时，同一次改动必须把路径加进 `server.py` 的 `SPA_PATHS`，否则 `test_spa_routes` 会失败；
  - 改共享文件（`server.py`、`main.tsx`）之前，先在协调板的「变更公告」写一行；
  - 不要自己构建 dist，也不要重载。
- **S-A 的前端全量结果**（38 个文件、408 个测试，串行跑）：
  - 第一次：404 个通过，4 个失败，都是 5 秒超时，在 `ReadingSelection.test.tsx` 等文件里。这些文件 S-A 都没碰过；单独跑时其中一个用 30 秒超时就能通过。
  - 第二次：407 个通过，1 个失败：`FactBank.test.tsx`「shows recent extraction runs on the extraction tab」找不到文字，这是 A 新加的测试。这个文件单独跑 7 个全部通过。
  - 结论：失败都发生在机器负载高的时候（多个会话同时跑测试），和 S-A 无关。FactBank 那条已经告诉 A，可能需要给 findByText 加长等待时间。重载窗口里请以协调会话的全量结果为准。
