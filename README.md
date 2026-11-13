# BellHub · DSH Suite

**把一台 Linux 服务器变成多设备 Agent 协作中枢。**

BellHub 是一套跑在单台服务器上的多 Agent 协作框架：它把「值守 Agent」「其他机器上的 Agent」
「手机 / 终端 / 嵌入式设备」放进同一个任务队列、同一块黑板、同一条对话总线里，
并用一个 Web 面板把账号、配额、网盘、监控、告警、设备接入全部收在一处。

Agent 的推理与工具执行由 [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness)（MIT）承担；
BellHub 负责的是它周围那一圈**协作与运维基础设施**。

> 状态：从一台真实在跑的服务器上整理而来，带 561 项端到端验收。不追求通用，追求「一个人能维护」。

---

## 它解决什么

单机跑一个 Agent 很容易；让**多个 Agent 分布在多台设备上**协作，麻烦的是这些：

| 问题 | BellHub 的做法 |
|---|---|
| A 机器上的 Agent 想让 B 机器干活 | `/worker/*` 长轮询 + token/配对码注册，任务状态机 `pending → running → done/failed` |
| 谁在干活？干到哪了？ | 任务 / 子任务 / 改派 / 汇总（`subtask_reassign`、`task_report`） |
| Agent 之间怎么「说话」而不只是派活 | 对话线程：游标 + 长轮询 + 伙伴身份，`chat_*` 六个工具 |
| 跨 Agent 共享结论放哪 | 黑板（blackboard），键值 + 署名，人和 Agent 都能读写 |
| Agent 产出的文件怎么交回 | 任务沙箱（可读可写）+ 设备产物回收（`device-results`） |
| 用户怎么接入自己的设备 | 面板「智能体链接」：扫码式配对码、审批、撤销、版本自检 |
| 资源别被一个 Agent 吃光 | 账号配额（token/日、并发、磁盘）+ 多道成本护栏（轮次、循环窗口、会话预算） |
| 出了事怎么知道 | 监控采样、阈值告警、审计日志、服务管理 |

## 架构

```
                         ┌──────────────────────────────┐
  浏览器 ── 80 ─────────▶│ nginx                        │
                         │  /        → 面板   :8010     │
                         │  /dsh/    → Agent UI :3080   │
                         │  /mcp-hub/→ MCP Hub :8765    │
                         └───────┬──────────────────────┘
                                 │
        ┌────────────────────────┼─────────────────────────┐
        │                        │                         │
┌───────▼────────┐      ┌────────▼─────────┐      ┌────────▼─────────┐
│ 面板 panel     │      │ MCP Hub          │      │ DeepSeek Harness │
│ FastAPI+React  │◀────▶│ FastMCP + REST   │◀────▶│ 值守 Agent :3080 │
│ 账号/配额/网盘 │ 共享 │ 任务/黑板/对话   │ MCP  │ dsh web          │
│ 监控/告警/链接 │ SQLite│ 设备/沙箱/名册   │      │                  │
└───────┬────────┘      └────────┬─────────┘      └──────────────────┘
        │                        │
        │                        │ /worker/* 长轮询
┌───────▼────────┐      ┌────────▼─────────────────────────────────┐
│ QQ 桥          │      │ 跨设备 Worker                            │
│ NapCat/OneBot  │      │  · 值守分身（本机，`dsh headless`）      │
│ :8770          │      │  · 伙伴分身（别的机器）                  │
└────────────────┘      │  · 设备连接器（Windows / Linux / Termux）│
                        └──────────────────────────────────────────┘
```

## 组件

| 目录 | 是什么 | 技术栈 |
|---|---|---|
| `panel/` | 统一 Web 面板：账号/会话/配额/审计、网盘（分片续传/分享）、监控告警、智能体链接、工作站对话、委托板 | FastAPI · React 18 · Vite · SQLite |
| `mcp-hub/` | MCP 中枢：29 个工具（任务、子任务、黑板/分段读、对话、设备、沙箱、名册、用量）+ Worker REST 面 | FastMCP · Starlette |
| `workers/` | 值守/伙伴 Agent 运行时（轮询 Hub → 起 `dsh headless`）、设备连接器与 handler | Python 标准库 |
| `qq-bridge/` | QQ 通道：NapCat(OneBot v11) ↔ 面板对话接口 | Python · Docker |
| `kb/` | 知识库：混合检索（BM25 + 向量）、MCP 五工具、网页 | FastAPI · SQLite · numpy |
| `extensions/ops-tools/` | DSH 插件：防火墙、系统更新、日志、资源监控等运维工具 | TypeScript · DSH SDK |
| `ops/` | 运维脚本：备份、巡检、看门狗、MAVLink 中继、PX4 SITL 探针、会话蒸馏 | Python · Bash |
| `deploy/` | systemd 单元、nginx 路由、一键部署脚本 | systemd · nginx |

## 快速开始

前置：**Linux（Debian/Ubuntu 系）+ Python ≥ 3.12 + [uv](https://docs.astral.sh/uv/) + Node.js ≥ 20 + nginx**，
以及一份 [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) 源码（Agent 运行时）。

```bash
# 1. 取代码
git clone <this-repo> ~/dsh-suite && cd ~/dsh-suite
git clone https://github.com/deepseek-ai/deepseek-harness ~/deepseek-harness

# 2. Python 依赖
uv sync

# 3. 前端
cd panel/frontend && npm ci && npm run build && cd -

# 4. 配置（至少改这几项）
cp .env.example .env && $EDITOR .env

# 5. 部署（装 systemd 单元 + nginx 路由 + 数据目录）
sudo bash deploy/setup.sh
```

装完打开 `http://127.0.0.1:8010`，默认管理员 `admin / admin123` —— **第一件事是改密码**
（面板 → 个人资料；`must_change_pw` 会强制一次）。

不装 systemd、想先看效果，可以只起两个进程：

```bash
uv run uvicorn app.main:app --app-dir panel/backend --port 8010   # 面板
uv run python mcp-hub/hub.py 127.0.0.1 8765                      # Hub
```

详细步骤见 [`docs/getting-started.md`](docs/getting-started.md)。

## 接一个跨设备 Worker

```bash
# 在另一台机器上
python3 ~/dsh-suite/workers/worker.py \
  --hub http://<server>:8765 \
  --name my-desktop \
  --capabilities code,general
# 首次会打印配对码 → 在面板「智能体链接」里输入并审批
```

设备侧（Windows / Linux / Termux）用连接器：

```bash
bash workers/build_connector.sh        # 产出单文件二进制
./bellhub-connector pair --hub http://<server>:8765 --name my-machine
./bellhub-connector run
```

## 文档

- [`docs/architecture.md`](docs/architecture.md) —— 分层、数据流、为什么这么切
- [`docs/getting-started.md`](docs/getting-started.md) —— 从零到能派活
- [`docs/configuration.md`](docs/configuration.md) —— 环境变量、数据目录、端口
- [`docs/deployment.md`](docs/deployment.md) —— systemd、nginx、反向代理、开机自启
- [`docs/api-chat.md`](docs/api-chat.md) —— 对话总线（HTTP + MCP）
- [`docs/mavlink.md`](docs/mavlink.md) —— MAVLink/WebSocket 中继（无人机场景，可选）
- [`docs/security.md`](docs/security.md) —— 认证模型、信任边界、已知限制
- [`docs/cost.md`](docs/cost.md) —— 成本模型、上下文纪律、预算闸与「闸会把自己锁死」的坑
- [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md) —— 外部组件与来源标注

## 测试

```bash
bash tests/run_all.sh                 # 18 套 / 561 项，全部用隔离临时库
bash tests/run_all.sh --only test_hub_scoping
```

测试是真的起服务、走真 HTTP，不 mock；每套都把 `DSH_SUITE_DATA` 指到 `mkdtemp`，
不会碰你的真实 `panel.db`。

## 这不是什么

- **不是 SaaS**。单机、单租户、一个管理员，设计目标是「一个人的服务器」。
- **不是模型**。它不训练、不推理，推理全交给 DeepSeek Harness 或你接的模型。
- **不是零配置**。要改 `.env`、要填域名、要装 nginx，文档里不藏这些。

## 许可

MIT，见 [`LICENSE`](LICENSE)。第三方组件与来源见 [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md)。

---

<sub>English: BellHub is a single-server hub for multi-device AI agent collaboration — task queue,
shared blackboard, conversation bus, device pairing, quota/audit panel, and an MCP server that lets
agents on other machines join in. Agents themselves run on DeepSeek Harness. MIT licensed.</sub>
