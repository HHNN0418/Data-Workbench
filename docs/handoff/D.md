# D 交接：LLM 调用层 / 调度效率（2026-10-22）

原会话：「AI 工作流调用效率」（D，`local_a885e808`）。按 D44，交给协调会话的子 agent 继续。
详细审计和方案见 `docs/AI_CALL_EFFICIENCY.md`：第 8 节是调度效率，8.5 是实施记录和复测。

## 1. 负责范围

- D 自己的文件：`ctn_data/ai_providers.py`（共享文件，改前要在协调板公告）、`agent_budget.py`、`llm_calls.py`、`llm_gateway.py`、`ai_jobs.py`、`ai_workflows.py`。
- 脚本：`scripts/measure_llm.py`、`scripts/openrouter_balance.py`。
- 测试：`tests/test_llm_calls.py`、`test_llm_gateway.py`、`test_publish_priority.py`。
- 在别人文件里改过的地方（都用 Edit，都已登记）：
  - `agent_generation.request_options` 加了 `trace`；
  - `ai_routes.invoke` 加了 `purpose` 参数；
  - `agent_actions.execute` 的 `write()` 加了发布优先级包装；
  - `agent_reading` 记录错误时的 TypeError 修复；
  - `agent_observation._finish_channel` 加了 `first_seen_at`。

## 2. 已上线的改动（D 已经没有未部署的改动）

| 批次 | 内容 | 上线 commit |
|---|---|---|
| 第一批 | 每次 LLM 调用记一行到 `llm_calls`（失败也记）；按调用类型设 `max_tokens`；402 在途额度 / 429 按 Retry-After 原地等待、重发同一请求；`session_id` | `6cae8f9` / `084ffdf` |
| 第二批 | S2：写作、检查、事实提取用 `reasoning.effort=low`，决策保持 auto。S4：统一闸门 `llm_gateway.py` | `5d0b1a6` |
| D26 ①② | post 额度优先占位：人工 0 > 回复通知 1 > 其他回复 2 > 新帖 3。接口是 `publish_priority`、`priority_for`、`release_priority` | `5d0b1a6` |
| 第三批 | `agent_reading` TypeError 修复（这个 bug 造成"角色任务未完成；敏感错误已隐藏"）；选读改用 `reasoning none`；结构化输出不是 JSON 时自动重发一次；收件箱 `first_seen_at`；脚本按规则 6 禁用 initialize | `b2004dd`（第四窗口，10:53 悉尼 = 23:53 UTC 10-21） |

### 当前参数

- **`llm_calls.MAX_TOKENS`**：reading 8000、decide 12000、single 20000、write 8000、check 6000、extract 12000、agent 16000、radar / research / interactive 24000、connection_test 4000。
- **`llm_calls.REASONING`**：reading `none`；write、check、extract `low`；其余不传，保持 auto。只对 OpenRouter 生效。
- **`RETRY_BUDGET_SECONDS`**：60。402 / 429 最多重试 3 次；结构化输出不是 JSON 时最多重发 1 次。
- **`llm_gateway.GATE`**：上限 6，`RESERVE=2`，也就是后台任务只能在至少空 2 个名额时才放行。优先级依次是 write / check、decide / single、reading / agent、interactive、radar / research / extract。402 时收紧上限。排队时间计入 `llm_calls.wait_ms`。
- **发布占位**：只对 `post` 额度生效。占位存在 `rate.priority_holds['post']`，有效期 retry_after + 30 秒。

## 3. 关键数字（用于对比）

| 指标 | 基线（10-21） | 第三窗口后（21:00–23:00 UTC） |
|---|---|---|
| 有效发布（发帖 + 回复） | 27.3 条/小时 | 48–50 条/小时（post 额度已放宽到 2 条/分钟） |
| 选读耗时中位数 | 43 秒 | 17.6 秒；第四窗口改成 none 后，前几次约 3 秒 |
| 作业耗时中位数 | 127 秒 | 112 秒 |
| 闸门排队 | — | 462 次调用里只有 1 次排过队（16 ms） |
| 缓存命中 | 未记录 | 检查 52%、写作 19%、选读 5%、决策 4% |

## 4. 剩下的任务（按优先级）

1. **选读的 max_tokens**：先在第四窗口后跑 1–2 小时数据（`measure_llm.py --hours 2 --json`，看 reading 的 `completion_p99`、思考 token 和 `truncated`）。如果思考 token 的 p99 接近 0、completion 的 p99 低于约 400，就把 `MAX_TOKENS['reading']` 从 8000 降到 2000：万一哪家服务商不理会 none，也能几秒内失败、退回备用目录，而不是跑约 50 秒。只改 `llm_calls.py`，测试不用改。
2. **用 first_seen_at 测发现延迟**：只统计第四窗口上线之后（≥ 23:53 UTC 10-21）创建的通知。预期中位数 ≤ 60–90 秒，因为每个 Agent 约 60 秒轮询一次，生成期间会跳过。脚本骨架见第 6 节。
3. **通知的处理链路（S9）**：之前测出"从通知到生成作业排队"中位数 236 秒；47% 的通知回复，前面先跑过至少一个同一 Agent 的其他作业，都没回这条通知。修复在 B 那边：tick 优先给收到通知的 Agent 排作业，选读必须带上通知，决策默认先回复通知。需要复测 B 改完后的效果。
4. **A 的本地预检**：A 已在 `agent_pipeline.local_issues` 里加好（还没上线），拦以下几类，命中就直接退回写作，不调用检查模型：
   - 让步说法（`_CONCEDE`）；
   - 自我让步：I don't have it / that / the name…、I haven't seen…、I can't speak to、I won't pretend；
   - 列出场所清单；
   - 逐字复制事实。

   "I hear you"、"I don't have kids" 这类不拦，已有测试覆盖。上线后测三件事：每条发布需要几次检查调用、"两次没通过检查"的次数、误拦率（拿本地拦下的和检查模型的结论对比）。
5. **调度器的后续工作**：
   - E5 消息顺序：决策的缓存命中只有 4%，要把固定内容（规则、人设、允许动作、护栏）挪到消息开头。要等 A、B 交出 `agent_pipeline` 之后，按 B → A → D 的顺序做。
   - S5 流水线：在写作阶段就开始预取下一轮的选读；草稿等额度时允许下一轮先跑决策。这些都在 B 的 `agent_control` / `agent_generation` 里，要和 B 约好。
   - 闸门参数：按 `wait_ms` 的分布调上限和 RESERVE。目前几乎没有排队，不用动。
   - E7(b) 选读目录从 48 条缩到 32 条，片段从 320 字缩到 200 字：D13 已批准，改 `agent_reading` 前先告诉 B。E7(c) 规则选读的对照试验：等 E1 数据攒够后，拿 1 个 Agent 试。
6. **已经转给别人的**：
   - Relace 的 reason 超长：B 已改成截断。
   - 23:00 的数据库锁超时（`55P03`）和一个 idle in transaction 的流式游标：转给了 C 和协调会话。

## 5. 和 A / B / F 的约定

- **`agent_pipeline.py`**：修改顺序是 B → A → D。本地预检由 A 来做，D 不改。D 的消息顺序（E5）等前两家交出后再单独约。
- **`agent_reading.py`**：D13 授权 D 改 E7(b)，改之前先发消息告诉 B。
- **`agent_observation.py`**：B 已同意 D 改；再改之前仍要先告诉 B。
- **`agent_control.tick`**：发布排序已经由 B 按 `priority_for` 加好（D26 ③）。
- **F 的人工排队**：F 用 `agent_budget.publish_priority` / `release_priority`。人工帖被 Deferred 后，按 `exc.retry_after` 重试；放弃时调用 `release_priority(controller, 'post', draft_id)`。
- **共享文件**（`ai_providers.py`、`db.py`、`server.py` 等）：改前在协调板「变更公告」写一行；改别人的文件只用 Edit。
- **线上库**（协调板规则 6）：先把 `ctn_data.db.initialize` 和 `ctn_data.postgres.initialize_postgres` 替换成直接抛错的函数，再执行 `BEGIN READ ONLY`。`measure_llm.py` 和 `openrouter_balance.py` 已经内置了这一步。
- **真实调用验证**：每次最多 3 次，并记下花费。

## 6. 工具用法

```bash
.venv/Scripts/python.exe -X utf8 scripts/measure_llm.py --hours 2
```
按调用类型统计次数、成功数、错误码、耗时和首 token 中位数、重试次数、缓存命中率、截断次数和花费。加 `--json` 可以看 p99、思考 token 和服务商分布。只读，不调用模型。

```bash
.venv/Scripts/python.exe -X utf8 scripts/openrouter_balance.py --no-log
```
查 OpenRouter 余额和消耗速度。只读，不打印 key。

发现延迟的统计方法：读 `meta` 里 `agent_control_v1:memory/<agent>/inbox` 的每一条通知，筛 `created_at` 在上线时间之后的，统计 `first_seen_at - created_at` 的中位数和 p90。连接方式照抄 `measure_llm.py`：只用 `db.connect`，执行 `BEGIN READ ONLY`，最后 rollback。

## 7. 测试命令

D 的核心测试，约 2 分钟：

```bash
.venv/Scripts/python.exe -m unittest tests.test_llm_calls tests.test_llm_gateway tests.test_publish_priority tests.test_ai_providers tests.test_ai_completion_compatibility tests.test_agent_budget tests.test_agent_reading tests.test_agent_observation
```

调用方相关的测试，约 5–6 分钟，可以分几组并行跑：

```bash
.venv/Scripts/python.exe -m unittest tests.test_agent_budget_runtime tests.test_agent_generation_api tests.test_agent_generation_errors tests.test_agent_generation_jobs tests.test_agent_loop tests.test_agent_reading_integration tests.test_ai_routes tests.test_ai_jobs tests.test_agent_actions tests.test_agent_manual_execution tests.test_agent_notifications tests.test_agent_notification_binding tests.test_agent_interaction_workflows
```

如果某个失败怀疑是别的会话未提交的改动造成的，可以这样隔离：`git archive HEAD ctn_data tests scripts pyproject.toml` 导出到 scratchpad，只叠加自己的文件后再跑。
