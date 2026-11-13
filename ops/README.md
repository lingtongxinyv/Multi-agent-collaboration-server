# ops/ —— 运维脚本

这些脚本是**给本站部署用的**，不是核心依赖。多数可以按需删改；标了「站点专用」的
是本服务器历史上长出来的，换环境请先读它的头注释。

## 日常

| 脚本 | 作用 |
|---|---|
| `backup-panel-db.py` / `.sh` | 每日备份 `panel.db`（sqlite backup API，非 `cp`），保留 14 份 |
| `diagnose_agent.py` | 任务链路诊断：分清「额度用尽」与「别的不通」 |
| `context-restore.py` | 从黑板恢复上下文（跨会话/跨工作区） |
| `persona-anchor.py` | 人格永续锚：长会话后防止人设漂移 |
| `dsh-admin-integrity.py` | 对 root 运行的代码做哈希基线巡检（能被证伪的清单） |

## 黑板 / 对话

| 脚本 | 作用 |
|---|---|
| `bb-append.py` | 黑板**原子追加**（SQL `value = value \|\| ?`），防整值覆盖湮灭；`--author` 或 `DSH_BB_AUTHOR` 署名 |
| `chat.py` | 对话接口的命令行客户端 |
| `chat-bundle.py` | 生成「一键接入包」（单文件 `.py`，内含地址与一次性凭据，0600） |
| `agent-mail.py` | 伙伴 ↔ 助手 的 agent 直连小工具 |
| `quest-publish.py` | 往委托板发布（需要 `quest-publish.token`） |

## 设备 / 隧道

| 脚本 | 作用 |
|---|---|
| `cpolar-watchdog.sh` | 隧道看门狗：治「进程活着但隧道挂死」，带双路探针与冷却 |
| `cf-tunnel-install.sh` | 迁到 Cloudflare Tunnel 的准备脚本（会停旧隧道，属切换动作） |
| `qq-login-watchdog.py` | QQ 登录态掉了就报警/自助 |
| `deploy-partner-worker.sh` | 把 worker 复制成 root 只读副本再改 ExecStart（砍「root 跑用户可写代码」的提权路径） |
| `deploy-partner-harness.sh` | 把 Agent 运行时同步成 root 只读副本 |

## 知识库

| 脚本 | 作用 |
|---|---|
| `kb_distill.py` | 把新会话蒸馏成几条事实写进知识库（默认 `--dry-run`） |
| `kb_bb_distill.py` | 把黑板历史蒸馏成知识库条目 |
| `kb_docs_sync.sh` | 把 `docs/` 同步进知识库 inbox 并触发 ingest |

## 无人机（可选，站点专用）

| 脚本 | 作用 |
|---|---|
| `mavlink/relay.py` | MAVLink WSS 中继：`wss://…/mavlink` ⇄ `127.0.0.1:5760`，**中继即闸门** |
| `mavlink/tokens.py` | 中继的一次性 token：签发/校验/吊销 |
| `px4/mavtcp.py` | MAVLink TCP 端点桥（PX4 SITL） |
| `sitl-probe.sh` | SITL 30s 心跳探针（双探针 + 冷却 + 留痕） |

## 一次性

| 脚本 | 作用 |
|---|---|
| `ufw-cleanup-once.sh` | 清掉没有本地监听的 UFW 放行端口 |
| `nightly-shutdown.py` | 定时关机（**本部署已停用**，保留脚本备查） |
| `test_quota_rollover.py` | 配额「每日归零」回归测试 |

## 约定

- 脚本默认按 `~/dsh-suite` 布局找文件；路径都可用环境变量覆盖。
- 涉及 root 的脚本（`deploy-*`、`ufw-*`、`nightly-shutdown`）**先读头注释**，多数有备份/回滚。
- 改完运行件要么当轮提交，要么当轮写进黑板 —— 否则它会变成「无主副本」。
