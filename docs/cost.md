# 成本与上下文

这套系统最贵的不是模型单价，是**上下文每步全量重发**。

一个 70KB 的黑板键，读一次 ≈ 2 万 token；之后每一轮都重发一次。一个跑了 40 步的会话，
把同一个键重发了 40 次。**所以省钱的第一杠杆不是少干活，是别把大块东西放进上下文。**

## 实测（本机，2026-11-13）

| 项 | 数值 |
|---|---|
| 平时日花费 | ¥0.07 – 1.92 |
| 异常日 | **¥7.15**（12 次记费 / 7,736 万 token） |
| 其中两次长会话 | 1,470 万 + 1,933 万 token |
| 黑板总量 | 328 键 / 95.6 万字符 |
| 最大单键 | `admin/open-items` **70,476 字符** |

当天的归因很干脆：整读那个 70KB 的键 + 在同一会话里连做五件事。

## 已经内置的闸（代码里）

| 闸 | 位置 | 行为 |
|---|---|---|
| 每日对话 token 上限 | `chat.py` `NEKO_CHAT_DAILY_TOKENS` | 超了直接拒答，不再调模型 |
| 公开面单独额度 | `chat.py` `PUBLIC_CHAT_DAILY_TOKENS` | 路人吃不到管理员预算 |
| 单轮预算 | `workers/*_agent.py` `turn_max_tokens` | 上一轮超限 → 静默一轮 |
| 滚动窗口 / 回合数 | `loop_window_sec` / `loop_max_replies` | 防连续爆量 |
| **日成本闸** | `assistant.py` + `chat.py` `SUITE_COST_DAILY_CAP_CNY` | 今日花费 ≥ 上限（默认 ¥12）→ 直接拒绝，不启动 headless |

> 日成本闸读的是 DSH 自己写的账本 `~/.dsh-admin/.dshw-usage.json`（`DSH_USAGE_FILE` 可覆盖）。
> **读不到就放行** —— 监控坏了不能把功能锁死。

## 读取纪律（给写 agent 的人）

1. **先读摘要**，再定向取细节。本仓的黑板工具就是为这个准备的：
   - `blackboard_read(key)` —— 只用于小键（< 10KB）
   - `blackboard_sections(key)` —— 只看标题结构
   - `blackboard_tail(key, n)` —— 只看末尾 n 行
   - `blackboard_search(q)` —— 全板搜关键词，返回「键:行号: 那一行」
2. **单键 > 10KB 禁止整读**。
3. **一件事一个会话**。长会话是隐性复利：第 40 步的价格里含前 39 步的全部内容。
4. 大文件先落盘，再看片段（`read` 带 offset/limit），不要整篇灌进上下文。

## 一个容易踩的坑：预算闸会把自己锁死

`workers/*_agent.py` 的「单轮预算」闸是**事后闸**：它看的是「上一轮花了多少」。
如果静默分支不写回状态，`last_tokens` 就永远停在超限值 —— **一轮爆量 = 永久静默**，
消息只被消费、不唤起会话，对人来说就是「这个 agent 突然不理我了」。

排查方法：

```bash
journalctl -u dsh-<agent> | grep 单轮预算已超
cat ~/.dsh-<agent>/partners.json | grep -A3 'agent:'
```

对策（二选一，改代码优先）：

- 给静默加**时间窗**：窗口外的第一条消息放行一轮试探（推荐）；
- 或运维侧手工把 `partners.json` 里对应 `last_tokens` 归零（治标，下一次爆量还会重复）。

## 看板

面板「管理员 → 监控仪表盘 → 今日成本」直接读 DSH 账本，显示：今日花费 / 今日 token / 余额 /
近 7 天 / 今日最贵的几次记费。告警规则 `cost_today > 5`（人民币）由首次启动自动补种。

> 不要用 `panel.db` 的 `accounts.used_tokens` 做成本告警：admin 的 web 会话不经过面板记账，
> 那一列长期是 0，用它等于永远不响。
