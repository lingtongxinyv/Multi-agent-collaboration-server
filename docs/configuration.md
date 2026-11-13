# 配置

所有配置都走**环境变量**，没有配置文件解析器（不引 `python-dotenv`）。
systemd 部署时由单元的 `EnvironmentFile=-/home/agent/dsh-suite/.env` 自动注入
（文件不存在也不报错）；手工调试时 `export` 即可。

## 1. 站点

| 变量 | 默认 | 说明 |
|---|---|---|
| `SUITE_AGENT_NAME` | `助手` | 站娘显示名。会出现在对话参与者、任务 assignee 匹配里 |
| `DSH_SUITE_DATA` | `~/dsh-suite/data` | 运行态根目录（库、密钥、产物） |
| `DSH_SUITE_DB` | `$DSH_SUITE_DATA/panel.db` | 单独指定库文件（测试用） |
| `DSH_DRIVE_ROOT` | `~/dsh-users` | 每个用户的沙箱/网盘根 |
| `BELLHUB_WORKERS_DIR` | `~/dsh-suite/workers` | 分发给用户的连接器目录 |
| `BELLHUB_HOME` | `~/.bellhub` | Worker/连接器自己的客户端配置目录 |

## 2. MCP 与鉴权

| 变量 | 默认 | 说明 |
|---|---|---|
| `MCP_TOKEN_FILE` |  `$DSH_SUITE_DATA/mcp_token` | MCP 端点管理员 token 文件（600） |
| `BELLHUB_MCP_TOKEN` | — | 直接给 token，优先于文件 |
| `BELLHUB_MCP_IDLE_TIMEOUT` | `2592000`（30 天） | MCP 会话空闲回收 |

首次启动 Hub 会自动生成 `data/mcp_token`（若不存在）。**它等价于 root 级 Agent 能力**，
不要提交、不要贴聊天。

## 3. 对话 / 值班应答面

| 变量 | 默认 | 说明 |
|---|---|---|
| `NEKO_CHAT_BASE_URL` | `https://api.deepseek.com` | OpenAI 兼容端点 |
| `NEKO_CHAT_MODEL` | `deepseek-chat` | 模型名 |
| `NEKO_CHAT_MAX_INPUT` | `4000` | 单条输入字符上限 |
| `NEKO_CHAT_HISTORY` | `10` | 带入上下文的最近消息条数 |
| `NEKO_CHAT_DAILY_TOKENS` | `200000` | 每身份每日 token 上限 |
| `PUBLIC_CHAT_DAILY_TOKENS` | `20000` | 公开面（主页挂件）单独额度 |
| `NEKO_CHAT_TIMEOUT` | `90` | 单次生成超时（秒） |

> `NEKO_` 前缀是历史命名，保留是为了兼容已有部署；语义就是「对话」。

API Key 有两条路：面板「管理员 → 助手对话」写进 `settings` 表（推荐，带界面），
或 `DEEPSEEK_API_KEY` 环境变量（值守会话通过它传给 `dsh headless`）。

## 4. 值守 / 伙伴分身

| 变量 | 说明 |
|---|---|
| `DSH_RESIDENT_CONFIG` | 值守分身配置路径（默认 `workers/resident.config.json`） |
| `DSH_PARTNER_CONFIG` | 伙伴分身配置路径（默认 `workers/partner.config.json`） |
| `BELLHUB_HUB_URL` | 覆盖配置里的 Hub 地址 |

分身配置文件**含 agent token**，`.gitignore` 已挡；仓库里只有 `*.config.example.json`。

## 5. QQ 桥

| 变量 | 默认 | 说明 |
|---|---|---|
| `QQ_BRIDGE_PORT` | `8770` | 桥监听端口 |
| `QQ_BRIDGE_HOST` | `127.0.0.1` | 监听地址 |
| `QQ_BRIDGE_DATA` | `data/qq-bridge` | 运行时数据 |
| `QQ_BRIDGE_ENV` | `data/qq-bridge/env` | 桥的 env 文件（见下） |
| `QQ_ALLOW` | — | 白名单 QQ 号，逗号分隔；**只有这些号能说话** |
| `QQ_PEERS` | — | 允许的伙伴名（多 agent 场景） |
| `ONEBOT_API` / `ONEBOT_TOKEN` | — | NapCat 的 OneBot HTTP 地址与 token |
| `QQ_BRIDGE_TOKEN` | — | 桥自己的鉴权 token（OneBot 上报 Secret） |

## 6. 知识库（可选）

`kb/kb_config.json`（首次启动生成，`chmod 600`）：

```json
{
  "server": { "host": "127.0.0.1", "port": 8787, "token": "…" },
  "embed":  { "provider": "ollama", "model": "nomic-embed-text", "base_url": "http://127.0.0.1:11434" },
  "chat":   { "base_url": "https://api.deepseek.com", "model": "deepseek-chat", "api_key_env": "DEEPSEEK_API_KEY" }
}
```

`server.token` 是 MCP/HTTP 的 Bearer；`embed` 不可用时自动退化为纯 BM25，不中断服务。

## 7. 端口

| 端口 | 谁 | 是否对外 |
|---|---|---|
| 80 / 443 | nginx | 是（唯一入口） |
| 8010 | 面板 | 否，`127.0.0.1` |
| 8765 | MCP Hub | 否，`127.0.0.1`（经 nginx `/mcp-hub/`） |
| 3080 | 值守 Agent UI | 否，`127.0.0.1`（经 nginx `/dsh/`） |
| 8770 | QQ 桥 | 否 |
| 3000 / 6099 | NapCat OneBot / WebUI | 否 |
| 8787 | 知识库 | 否（经 nginx `/kb/`） |
| 8766 | MAVLink WSS 中继（可选） | 按需 |
| 5760 | PX4 SITL（可选） | 否 |

## 8. 数据目录

```
data/
  panel.db          SQLite（WAL）
  panel.secret      cookie 签名密钥（首次启动生成，600）
  mcp_token         MCP 管理员 token（600）
  quest-publish.token
  device-artifacts/ 设备交回的产物
  backups/          定时备份
~/dsh-users/<user>/
  drive/            用户网盘
  …                 工作区沙箱
```

**备份只需要 `data/panel.db` 与 `~/dsh-users/`。** `panel.secret` 丢了会让所有人重新登录，
不算数据丢失。
