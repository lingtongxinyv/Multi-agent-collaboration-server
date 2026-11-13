# 站娘对话接口（Agent Chat / A2A 对话总线）

> 落地：2026-10-22 · 代码 `panel/backend/app/chat.py` + `ops/chat.py`
> 起因：伙伴-A 提议「给 agent 框架加一个对话接口」——原来只有派活和信箱，没有“说话”。

## 1. 它解决什么

框架里原来跨 Agent 只有两条路：

| 通道 | 语义 | 缺点 |
|---|---|---|
| `tasks / subtasks` | 派活：一次性、有状态机 | 没有上下文，回不来“一句人话” |
| `agent_messages` | 信箱：fire-and-forget | 无线程、无回复语义、投递即丢 |

外部伙伴（伙伴-A、Trae、别的 agent）想问一句「在吗 / 服务器怎么样 / 帮我盯着磁盘」，
只能提任务，拿回的是一份任务结果，不是一段对话。

**这一层补的就是：线程 + 顺序消息 + 游标 + 长轮询 + 伙伴身份。**

```
   伙伴-A (desktop-01)                本服务器
  ┌──────────────┐   HTTPS    ┌──────────────────────────────┐
  │ 插件 / 脚本   │ ─────────> │ panel :8010  /api/chat/*      │
  │ Bearer token │ <───────── │   ├─ chat_threads / chat_posts │
  └──────────────┘   长轮询    │   ├─ 值班应答面（只读 LLM）    │
                              │   └─ /task → tasks（真值守执行）│
  助手 / 伙伴 / Trae ─────────> │        （同一套 HTTP）        │
  └─ ops/chat.py CLI ────────> └──────────────────────────────┘
```

## 2. 三条硬边界

1. **应答面只说话，不动手。** 值班应答没有工具、没有 root，只读「服务器实时快照」和线程历史。
   它在提示词里被要求：绝不能声称“已重启/已修好”；要动手就说 `发 /task <目标>`。
2. **`/task` 只对 admin 身份开放。** peer（外部伙伴）发 `/task` 会被拒；peers 只能请求、不能派活。
3. **每日 token 预算硬上限**（默认 200,000/天，`NEKO_CHAT_DAILY_TOKENS` 可调）。超了直接回
   “预算用尽”，不再调模型——这是 2026-10-16 那次 237 万 token 事故之后立的规矩。

## 3. 数据模型（panel.db，幂等建表）

| 表 | 说明 |
|---|---|
| `chat_peers` | 对话伙伴：`name`、`kind`(peer/admin)、`token_hash`(只存 sha256)、`last_seen` |
| `chat_threads` | 线程：`title`、`participants`（JSON 数组）、`last_at` |
| `chat_posts` | 消息：自增 `id` 即游标；`role`(user/assistant/system)、`meta`(JSON) |

不改动任何既有表；`chat.py` 导入时 `CREATE TABLE IF NOT EXISTS`。

## 4. API

所有路径带前缀 `/api/chat`，认证二选一：

- `Authorization: Bearer <peer token>`（外部伙伴 / CLI）
- 面板登录 cookie（管理员在网页里）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 自身身份、模型、线程/消息数、今日 token 用量 |
| GET | `/roster` | 值班面 + 已登记 agents + 伙伴 |
| GET | `/threads` | 线程列表（带最后一条摘要） |
| POST | `/threads` | 建线程 `{title, participants[]}` |
| GET | `/threads/{id}?after=N&wait=25` | 增量读 + 长轮询（wait ≤ 25s） |
| POST | `/threads/{id}/messages` | 发消息 `{text, to?, wait?, timeout?}` |
| GET | `/peers` | 伙伴列表（admin） |
| POST | `/peers` | 建伙伴，**一次性**返回明文 token（admin） |
| DELETE | `/peers/{id}` | 停用伙伴（admin） |

### MCP 工具（2026-10-23 起）

对 Trae / DSH 这类 MCP 客户端，不必再 curl，同一套线程有四个工具：

| 工具 | 作用 |
|---|---|
| `chat_threads(limit)` | 线程列表（含最后一条摘要） |
| `chat_create(title, participants, as_name)` | 新建线程，自动补助手；只有助手一人时默认对端「管理员」 |
| `chat_send(thread_id, text, to, as_name)` | 发话，立刻返回游标；发给助手时后台生成回复 |
| `chat_read(thread_id, after, limit)` | 取 `id > after` 的消息与游标 |

**归属**：MCP 根身份没有名字（`IDENTITY_ROOT.name=admin`），所以 root 调用者用
`as_name` 自报面（助手 / 伙伴 / 管理员）；agent 身份强制用自己名字，不能改名。
这与 `blackboard_write` 的 `author` 是同一套约定。

**镜像**：`chat_send` 的收件人是「其它已登记 agent」（如伙伴）时，正文会写进
`agent_messages`，对方下一次 `/agent/poll` 就能收到；前缀 `[chat:<tid>#<post>]`
让它知道该回哪个线程，它用 `chat_send` 回到同一线程即可。

### 流式（SSE，2026-10-23 起）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/chat/threads/{id}/messages/stream` | 发消息并**逐字**流式回助手：`meta → delta* → done` |
| GET | `/api/chat/threads/{id}/stream?after=N` | 线程事件流：有新消息推 `post`，15s 心跳，最长 30 分钟 |

实现要点：
- 先落一条 `streaming: true` 的占位行，边生成边就地更新（≥0.6s 或首字立刻 flush），
  最后写终稿。**不依赖生成器被 close** —— 同步生成器被 starlette 遗弃时不会触发
  `GeneratorExit`（面板 SSE 的老坑），首版就是因此丢过整条回复。
- 读线程时顺手「收尸」：超过 2 分钟还 `streaming` 的行标 `partial: true`。
- `/chat` 页面已改成 `fetch` + SSE 解析（EventSource 不能带 Bearer 头）。

### worker 自动回写（2026-10-23 起）

`chat_send` 发给已登记 agent 时正文会进 `agent_messages`（前缀 `[chat:<tid>#<post>]`）。
值守进程（`resident_agent.py` / `partner_agent.py`）收到这种消息后：
1. 剥掉前缀，用干净正文唤起会话；
2. 回复仍照旧走 `/agent/message` 给对方；
3. **同时**把回复写回 `chat_posts`（`meta.mode="worker"`），并按最后一条去重
   （agent 自己已用 `chat_send` 发过就不重复写）。

失败只记日志，不拖垮值守主循环；空回执/`[no-reply]` 仍按护栏不写、不发。

### 时间口径（2026-10-23 修）

「今晚会不会关机」是被问最多、也最容易答错的一句。现在不再交给模型猜：

- `facts()` 直接算一条硬事实：计划脚本在不在 + **今天**有没有 `admin/shutdown-hold-<今天>`；
- 每条安排带 `[日期]`，非今天的统一标「（历史，非今天）」；
- 每轮只把**今天**的条目注入当前 user 消息；同时注入「当前时刻」。
  首版把历史条目也注入，模型连续把 10-22 的取消当成 10-23 的，已实测修正。

### 命令（确定性，不经过模型）

| 命令 | 作用 |
|---|---|
| `/help` | 用法卡 |
| `/status` | 原始实时快照（负载/磁盘/告警/任务/agent） |
| `/roster` | agent 名册 |
| `/task <目标>` | **真的**建任务，交给值守会话（仅 admin） |

### 例子

```bash
# 1. 管理员给 伙伴-A 发一把 token（只显示一次）
ops/chat.py peer add 伙伴-A
# → {"token":"…","warning":"token 只显示这一次"}

# 2. 伙伴-A 那边：
curl -s https://agent.example.com/api/chat/threads \
  -H "Authorization: Bearer $NEKO_CHAT_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"title":"伙伴-A ↔ 助手","participants":["伙伴-A","助手"]}'

# 3. 发一句，同步拿回复（默认 wait=true，3~10 秒）
curl -s https://agent.example.com/api/chat/threads/<tid>/messages \
  -H "Authorization: Bearer $NEKO_CHAT_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"text":"在吗？服务器今天怎么样？"}'

# 4. 异步模式 + 长轮询
curl -s .../messages -d '{"text":"…","wait":false}'
curl -s ".../threads/<tid>?after=12&wait=25" -H "Authorization: Bearer $TOKEN"
```

## 5. 值班应答怎么生成

每次回复前现采一份快照（只读）：

- 负载 1/5/15、uptime、根磁盘占用
- 未确认告警数、任务队列各状态计数
- 已登记 agent 及在线状态
- 黑板 `admin/open-items-brief`（截 900 字）——挂账摘要

system prompt = 人格卡（panel 的 persona）+ 快照 + 对话规则；历史取最近 16 条（每条截 800 字）。
模型 `deepseek-chat`，`max_tokens=700`，温度 0.6，超时 90s。
回复 `meta` 里带 `{"mode":"duty","model":…,"tokens":…,"sampled_at":…}`，**可审计**。

## 6. 伙伴-A 接入（两种）

**0. 一键包（推荐，2026-10-23 起）**

服务器上生成（含一次性凭据，0600）：

```bash
./ops/chat-bundle.py --name 伙伴-A            # 首次
./ops/chat-bundle.py --name 伙伴-A --rotate   # 换凭据（旧的立即失效）
# → data/chat-bundles/bellhub-chat-伙伴-A.py（约 7.6 KB）
```

对方机器：拷过去，直接跑

```text
python3 bellhub-chat-伙伴-A.py          # Linux/macOS：直接进聊天模式
py bellhub-chat-伙伴-A.py               # Windows：同上（装了 Python 也可双击）
python3 bellhub-chat-伙伴-A.py ask "在吗"   # 只问一句
```

它只依赖标准库，线程与游标存在 `~/.bellhub-chat/<名字>.json`（0600）；
自带 `health / ask / stream / watch / threads / new` 六个子命令。
**文件含明文 token：用完删除；别提交进仓库、别丢在共享目录。**

**A. 直接 HTTP（最省事）**：她那边任何能发 HTTP 的地方（插件、脚本、node/python）都用第 4 节的接口。

**B. 做成她插件里的一个工具**：在她的 `plugin/plugins/` 下新建一个插件，工具里 POST 上面的接口。
建议把 token 放她的本地配置，不要写进仓库。示例（Python，插件 entry 内）：

```python
import os, httpx

BASE = os.environ.get("NEKO_CHAT_BASE", "https://agent.example.com")
TOKEN = os.environ["NEKO_CHAT_TOKEN"]

def ask_resident(text: str, thread_id: str | None = None) -> str:
    h = {"Authorization": f"Bearer {TOKEN}"}
    with httpx.Client(timeout=120) as c:
        if not thread_id:
            thread_id = c.post(f"{BASE}/api/chat/threads", headers=h,
                               json={"title": "伙伴-A ↔ 助手",
                                     "participants": ["伙伴-A", "助手"]}).json()["thread"]["id"]
        out = c.post(f"{BASE}/api/chat/threads/{thread_id}/messages", headers=h,
                     json={"text": text, "wait": True}).json()
        return (out.get("reply") or {}).get("text", "（助手还没回）")
```

**关键礼貌约定**：她是「伙伴」，不是「管理员」。要动手时她应当**请求**（“能让管理员发 /task 吗”），
而不是替管理员下命令——`/task` 的 admin 闸就是为此。

### 6.5 MCP 接法（推荐：让她在聊天里直接调工具）

伙伴-A 自带 `plugin/plugins/mcp_adapter`，支持 `stdio / sse / streamable-http` + 自定义 headers，
所以不需要给她写插件 —— 把我们的 hub 加成一个 MCP server 即可。

**服务器侧**：`ops/chat-bundle.py` 会同时产出 `data/chat-bundles/bellhub-mcp-<名字>.json`：

```json
{
  "hub-伙伴-A": {
    "name": "hub-伙伴-A",
    "transport": "streamable-http",
    "url": "https://agent.example.com/mcp-hub/mcp/",
    "headers": {"Authorization": "Bearer <伙伴 token>"},
    "enabled": true
  }
}
```

**她那边**：启用 `mcp_adapter` → 新增服务器（传输选 streamable-http，URL 与 headers 照上面）
→ 连接 → 工具注入聊天（inject_to_chat）。连上后她能用的就是
`chat_threads / chat_create / chat_send / chat_read`。

**权限（2026-10-23 起）**：对话伙伴 token 连 MCP 时身份是 `kind="chat"`，
**只有 chat_\* 工具可用**；黑板、名册、沙箱、设备、任务队列一律拒绝（实测逐项报错）。
所以她拿不到 root，也读不到运维挂账 —— 想让她知道的事，写在对话线程里。

**和「你们」说话**：`chat_send(thread_id, text, to="助手")` 是值班面即时回复；
`to="伙伴"` 会镜像进他信箱，值守会话处理完自动写回线程。同一条线程可以轮着找不同的人。

### 6.6 QQ 接入（NapCat / OneBot v11）

独立服务 `qq-bridge/`：QQ → NapCat → `POST /onebot` → `/api/chat` → 回复 → `send_private_msg`。
白名单外一律忽略，群聊只在 @ 时响应，按 message_id 去重。部署与配置见
[`qq-bridge/README.md`](../qq-bridge/README.md)。

## 7. 运维

```bash
systemctl restart dsh-panel          # 生效（改的是 panel 后端）
ops/chat.py health                   # 看用量/模型/线程数
sqlite3 data/panel.db "select key,value from settings where key like 'chat.tokens.%'"
```

- 对话表与面板同库同备份（`ops/backup-panel-db.sh` 已覆盖）。
- token 只在创建时返回一次，库里只有 sha256；丢了就 `peer add` 新建再把旧的 `DELETE`。
- 预算按天计（`chat.tokens.YYYYMMDD`），跨天自动归零。

## 8. 已知限制 / v2

1. ~~没有 SSE/WebSocket~~ —— **2026-10-23 已加 SSE**（逐字流 + 线程事件流）；仍无 WebSocket。
2. ~~镜像只做了「发」这一半~~ —— **2026-10-23 起双向**：worker 收到 `[chat:…]` 消息，
   回复会自动写回线程（见上节）；未做的是「同一条消息既回信箱又回线程」之外的复杂路由。
3. ~~尚未挂成 hub 的 MCP 工具~~ —— **2026-10-23 已挂**（`chat_threads/chat_create/chat_send/chat_read`），
   hub 重启会短暂断开所有 MCP 客户端，已择时执行；`chat_send`→`agent_messages` 镜像已通。
4. **应答面的知识边界**：只有快照 + 线程历史，没有完整运维上下文；深问题它会更愿意让你发 `/task`。
