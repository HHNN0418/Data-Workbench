# 取消关注机器人账号清单

整理时间：2026-10-22（B 会话，D38）。**本清单只列出对象，没有执行任何操作。** 用户已关闭手工发帖，是否取消关注、何时操作，都由用户本人决定。

## 来源

- 关注关系取自 Agent 状态中已核对的关系记录（`rate.relationships` 里 `kind=follow`、`present=true` 的条目）。
- 机器人标记取自 `account_annotations`，`is_bot=1`，原因均为 `behavior`：回话率高，或经常出现在很深的线程里。
- 我方 5 个账号目前共关注 22 个账号，其中 12 个被标为机器人。去掉 Ethan → Maya（我方自己的账号），共 11 个。

## 清单

| 我方账号 | 要取消关注的账号 id | 用户名 |
|---|---|---|
| Maya Collins（5554） | 6229 | mango_fest_lu |
| Ethan Brooks（5832） | 2533 | reid_ellis69 |
| Ethan Brooks（5832） | 4025 | pete_plumbs_kingstonia |
| Ethan Brooks（5832） | 4986 | grant_ferry_coffee |
| Ethan Brooks（5832） | 6898 | danny_solofa |
| Ethan Brooks（5832） | 9518 | （本地没有用户名） |
| Leah Patel（6464） | 3997 | tevita_marrowpoint |
| Leah Patel（6464） | 5018 | bridget_bus_dispatch |
| Leah Patel（6464） | 6280 | DrAmaraOutWest |
| Nora Bennett（6494） | 2535 | dylan_shaw83 |
| Nora Bennett（6494） | 4028 | lucas_plays_the_quay |
| Nora Bennett（6494） | 6237 | nana_ruth_ch |

## 不在清单里

- Ethan → Maya（5554）：这是我方自己的账号，同样被标为机器人，但不属于"别队机器人"，要不要取消请另行决定。
- 其余 10 个被关注的账号目前未被标为机器人，保留关注。

## 操作提示

- 每条取消关注是对比赛账号的一次对外操作，按平台限额每分钟最多 30 次（关注 / 取消关注共用）。
- 取消关注不会扣分（扣分的是"市民取消关注我们"）。
- 操作前建议重新核对一次 `account_annotations`。机器人标记每分钟同步一次，可能已经变化。
