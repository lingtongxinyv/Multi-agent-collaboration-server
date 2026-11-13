# 安全模型

这份文档写清楚：**BellHub 信任谁、防谁、以及哪些地方是故意不防的。**

开源一个能跑 root 命令的 Agent 框架，最怕的不是漏洞，是使用者误以为它「默认安全」。

## 1. 威胁模型

假设的环境：**单机、单管理员、少量可信用户**。不是多租户 SaaS，不要按 SaaS 的假设用它。

| 角色 | 信任级别 | 能做什么 |
|---|---|---|
| 管理员（admin） | 全信任 | 一切：账号、配额、服务重启、人格、凭据 |
| 普通用户（user） | 半信任 | 自己的沙箱/网盘、提交任务、与站娘对话 |
| 外部伙伴（peer） | 低信任 | 只能对话（Bearer token），不能派活 |
| Worker / 设备 | 低信任 | 只能领自己 capability 匹配的任务 |
| 公网匿名 | 不信任 | 登录页、健康检查；其余全 401 |

## 2. 认证

**人（浏览器）**：HMAC-SHA256 签名的 httpOnly cookie，`SameSite=Lax`，7 天。
密钥在 `data/panel.secret`（首次启动生成，600）。

**Agent（MCP）**：`Authorization: Bearer <token>`，token 文件 `data/mcp_token`（600）。
它是**管理员级**能力，等同于 root 通道。

**Worker / 设备**：配对码（6 位、15 分钟、限尝试次数）→ 签发一次性 token → 库里只存哈希。
撤销即失效（`DELETE /api/link/agents/{id}`）。

**伙伴（peer）**：`ops/chat-bundle.py` 签发，只绑定对话接口，不能碰任务与文件。

所有密码/token 一律**只存哈希**（pbkdf2-HMAC-SHA256 / sha256），明文只在签发时返回一次。

## 3. 已知限制（故意的取舍）

### 3.1 值守 Agent 以 root 运行

`deploy/dsh-admin.service` 里 `User=root`。原因是运维工具（防火墙、系统更新、服务重启）需要
root，且它要在 `dsh` 的沙箱外操作宿主机。

**这意味着：能对 Agent 说上话 = 能让 root 干活。** 提示词注入、恶意网页内容、被投毒的
任务描述，都能变成 root 命令。

缓解措施（都已在代码里，但都不是银弹）：

- 值守面与应答面分离：**对话只说不做**（`chat.py` 的值班应答没有工具、没有 root）
- 真要动手必须走 `/task`，而 `/task` 只对 admin 身份开放
- 高危命令有闸门（`workers/` 的 handler 会拒绝整体危险的串，见 `test_termux_handler.py` 的注入矩阵）
- 审计表记录管理操作

要补的两句（这两点容易误解）：

- **配额与沙箱是应用层的，不是安全边界**。以 root 运行时，`~/dsh-users/<user>/` 的路径校验、
  磁盘配额、并发限制，都只对「守规矩的调用」有效；进程本身有全盘权限。
- **root 跑的 headless 分身必须单独隔离 `DSH_HOME`**。web 面已在 `deploy/dsh-admin.service`
  里做了（`DSH_HOME=/home/agent/.dsh-admin`），headless 面靠 worker 自己设
  （见 `workers/resident_agent.py` 的 `env["DSH_HOME"]`）—— 漏了就会和人的会话共用存储与凭据。

**推荐加固**：不要给 root。建一个专用用户 + 只放行几条 `sudo` 命令，比 root 好得多。
`ops/dsh-admin-integrity.py` 是这套思路的起点（基线哈希，防止「改一行代码等下次重启」提权）。

### 3.2 单租户，没有 CSRF token

依赖 `SameSite=Lax` + 同源部署。跨站表单 POST 会被拦，但这不是完整的 CSRF 防护。
如果要把面板挂到共享域名下，自己加 CSRF token。

### 3.3 cookie 的 `Secure` 是关的

`auth.py` 里 `secure=False`，因为默认部署是「边缘隧道终结 TLS，nginx 听 80」。
**如果 nginx 自己有证书，请把它改成 `True`。**

### 3.4 工作站会执行用户代码

这是功能不是漏洞：用户在沙箱里跑代码。所以所有文件操作都必须过 `sandbox.resolve_safe`，
配额限制磁盘，进程没有额外隔离（不是容器）。

**不要把工作站当成不可信代码的执行环境。** 要跑不可信代码，自己套容器/VM。

### 3.5 日志会把凭据写下来（部署时自己管）

默认的 nginx `combined` 日志会把**整个请求行**（含 `?token=…`）写进 `access.log`；
uvicorn 的 `[accepted]` 行同理。做过一次实测：设备令牌以明文、每 ~66s 一条落盘。

两条建议（都不在代码里，属部署面）：

- **凭据走头，不走 query**：`Authorization` / 自定义头比 `?token=` 安全一个量级；
- 确实只能用 query 时，nginx 用**不含 `$args`** 的 `log_format`，服务端用 logging 过滤器把 `token=…` 打成 `***`；
  已经写进去的旧日志要按「已泄漏」处置（轮换令牌，而不是删日志了事）。

### 3.6 SQLite 无并发写保护之外的东西

WAL + 单写者。没有任何行级权限；能读 `panel.db` 就等于拿到全部数据（除密码哈希）。
`chmod 600` + 备份文件同样要 600。

## 4. 供应链

| 依赖 | 用途 | 备注 |
|---|---|---|
| DeepSeek Harness | Agent 运行时 | 必需，独立 MIT 项目 |
| FastMCP / Starlette | MCP 服务 | Apache-2.0 |
| FastAPI / uvicorn / pydantic | 面板 | MIT |
| React / Vite / TypeScript | 前端 | MIT / Apache-2.0 |
| NapCat | QQ 协议端（可选） | 第三方，容器镜像引用，不随本仓分发 |
| Ollama + nomic-embed-text | 知识库向量（可选） | 本机模型 |

`uv.lock` 与 `panel/frontend/package-lock.json` 都提交了，装的是锁定的版本。

## 5. 报告问题

见 [`SECURITY.md`](../SECURITY.md)。请不要在公开 issue 里贴可利用细节或真实凭据。
