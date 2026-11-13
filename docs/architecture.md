# 架构

## 1. 分层

```
┌──────────────────────────────────────────────────────────────┐
│  接入层     nginx :80                                        │
│            / 面板 · /dsh/ Agent UI · /mcp-hub/ MCP · /kb/     │
├──────────────────────────────────────────────────────────────┤
│  控制面     panel :8010（账号、配额、审计、链接、网盘、告警）   │
│            mcp-hub :8765（任务、黑板、对话、设备、沙箱）       │
├──────────────────────────────────────────────────────────────┤
│  执行面     DeepSeek Harness（`dsh web` / `dsh headless`）      │
│            值守分身 · 伙伴分身 · 设备连接器                    │
├──────────────────────────────────────────────────────────────┤
│  数据面     SQLite（panel.db）+ 文件（沙箱、网盘、产物）        │
└──────────────────────────────────────────────────────────────┘
```

一条贯穿的设计原则：**控制面不推理，执行面不掌管凭据。**

- 面板只管「谁能做什么、用掉多少、发生什么」；
- Hub 只管「谁在干什么、结论放哪」；
- 真正调模型、跑工具的只有 Harness 进程，它拿的是任务内容，不是账号密码。

## 2. 两个服务为什么分开

`panel` 和 `mcp-hub` 共享同一个 `panel.db`（WAL 模式多进程），但**进程分开**：

| | panel :8010 | mcp-hub :8765 |
|---|---|---|
| 面向 | 人（浏览器） | Agent（MCP / HTTP） |
| 认证 | HMAC 签名的 httpOnly cookie | `Authorization: Bearer`（MCP）· 配对 token（Worker） |
| 重启影响 | 网页短暂 502 | 已连的 MCP 会话断开，任务仍留在库里 |
| 故障域 | 面板崩了，Agent 之间照常派活 | Hub 崩了，人还能登录、看历史 |

合并成一个进程当然更简单，但「人点坏了网页」不该让整队 Agent 停摆，反过来也一样。

## 3. 数据流

### 3.1 派活（人 → 设备）

```
浏览器 POST /api/link/dispatch
  → panel 写 tasks（user_name, title, prompt, capability）
  → mcp-hub 的 worker_poll 长轮询取到
  → worker claim（原子 UPDATE ... WHERE status='pending'）
  → worker 调本地 handler / dsh headless 执行
  → complete / fail 回写结果
  → 面板轮询任务状态，用户看到结果与产物
```

认领是**原子抢占**：`UPDATE tasks SET status='running', worker=? WHERE id=? AND status='pending'`，
返回 `rowcount` 判断谁抢到。多 Worker 并发认领同一任务不会双跑。

### 3.2 对话（Agent ↔ Agent）

对话总线是独立于任务的一层（`panel/backend/app/chat.py`）：

- **线程**（thread）+ **顺序消息**（posts, 自增 id）+ **游标**（after）+ **长轮询**
- 参与者可以是人（admin）、站娘（值守面）、外部伙伴（peer，凭 Bearer token）
- 「值班应答面」只读：拿实时快照和线程历史回答，**没有工具、没有 root**；
  真要动手，用户在消息里发 `/task`，由值守会话认领执行
- 每日 token 预算硬上限，超了直接拒答而不是继续烧

### 3.3 设备产物回收

设备上构建出来的东西（exe、日志、固件）不必借第三方文件站：

```
worker POST /device/artifact（分块） → data/device-artifacts/<task>/<name>
用户 GET /api/link/connector-artifact?task=… → 下载
```

单文件上限 512 MB（`MAX_ARTIFACT_BYTES`），路径经 `sandbox` 校验，不允许 `..`。

## 4. 数据模型（SQLite，幂等建表）

| 表 | 作用 |
|---|---|
| `accounts` | 账号、角色、配额、当日用量、强制改密 |
| `sessions` | 面板登录会话（cookie 校验用） |
| `tasks` / `subtasks` | 任务与子任务状态机、认领者、结果 |
| `workers` / `agents` | 注册的 Worker / Agent（token 只存哈希） |
| `chat_threads` / `chat_posts` / `chat_peers` | 对话线程、消息、伙伴身份 |
| `blackboard` | 键值黑板（署名 + 时间） |
| `alerts` / `alert_rules` | 阈值告警与规则 |
| `audit` | 管理操作审计 |
| `devices` | 用户设备（心跳、指令、结果） |
| `quests` | 委托板 |
| `settings` / `persona` | 站点设置与人格卡 |

凭据一律**只存哈希**（pbkdf2/sha256）；明文 token 只在签发那一刻返回一次。

## 5. 关键取舍

**为什么用 SQLite 而不是 Postgres。**
单机、单管理员、日活个位数到几十。SQLite 的 WAL + 单写者模型足够，且备份就是 `cp` 一个文件。
真要横向扩展，`db.py` 的 `DB` 类已经是唯一入口，换驱动是局部改动。

**为什么任务和对话分两套。**
任务是一次性的、有状态机的、要落结果的；对话是有上下文、要等回复、要能撤回语气的。
把对话塞进任务会得到「每条消息一个任务」的垃圾队列；把任务塞进对话会丢状态机。

**为什么 Agent 用长轮询而不是 WebSocket。**
跨设备、跨 NAT、跨公司网络，长轮询几乎不会被中间设备掐；断线重连的语义也简单（重发 poll）。
MAVLink 那类需要低延迟的双向流才单独用 WebSocket（见 `docs/mavlink.md`）。

**为什么所有路径都过一次 `sandbox` 校验。**
工作站允许用户在沙箱里跑代码，这是**故意的**。所以任何文件读写都必须锚定在
`~/dsh-users/<user>/` 内，`..`、绝对路径、符号链接逃逸都在 `app/sandbox.py` 里统一挡掉，
而不是让每个接口自己记得检查。

## 6. 代码地图

```
panel/backend/app/
  main.py       路由装配（账号、文件、网盘、管理端、服务管理）
  db.py         数据层（唯一入口，SQLite + 迁移）
  auth.py       会话 cookie（HMAC 签名）
  chat.py       对话总线（线程/长轮询/值班面）
  assistant.py  值守会话的流式桥（SSE）
  drive.py      个人网盘（分片上传、分享、配额）
  sandbox.py    路径校验（所有文件操作的最后一道闸）
  monitor.py    采样与告警
  memory.py     记忆/检索
  codecenter.py 代码中心（在线编辑）
  chat_page.py  独立聊天页（移动端/嵌入式）

mcp-hub/hub.py  MCP 工具 + Worker/Agent/Device REST 面 + 鉴权

workers/
  resident_agent.py  值守分身（本机，轮询 Hub → dsh headless）
  partner_agent.py   伙伴分身（别的机器）
  worker.py          通用 Worker 客户端（Python，零依赖）
  bellhub_connector.py 设备连接器（单文件打包，Win/Linux/Termux）
  device_handler.py / shell_handler.py / termux_handler.py  执行面
```

## 7. 部署形态

一台机器、一个 Linux 用户、六个 systemd 单元：

| 单元 | 端口 | 说明 |
|---|---|---|
| `dsh-panel` | 8010 | 面板后端（uvicorn） |
| `dsh-hub` | 8765 | MCP Hub |
| `dsh-admin` | 3080 | 值守 Agent 的 `dsh web` |
| `dsh-resident` | — | 值守分身（可选，轮询 Hub） |
| `kb` | 8787 | 知识库（可选） |
| `nginx` | 80 | 路由与 TLS 终结 |

公网暴露只需要 nginx 的 80/443；其余全部 `127.0.0.1`。
