# 从零跑起来

目标：一台干净的 Debian/Ubuntu，从 clone 到「在面板里给另一台机器派活」。

全程假设：

- Linux 用户 `agent`，家目录 `/home/agent`
- 仓库放在 `~/dsh-suite`
- 只在本机访问（公网/域名放到 `deployment.md`）

## 0. 前置

| 依赖 | 版本 | 检查 |
|---|---|---|
| Python | ≥ 3.12 | `python3 -V` |
| uv | 最新 | `uv --version`（`curl -LsSf https://astral.sh/uv/install.sh \| sh`） |
| Node.js | ≥ 20 | `node -v` |
| nginx | 任意稳定版 | `nginx -v` |
| git | 任意 | `git --version` |

还需要一份 DeepSeek Harness 源码（Agent 运行时）：

```bash
git clone https://github.com/deepseek-ai/deepseek-harness ~/deepseek-harness
cd ~/deepseek-harness
pnpm install          # 或按上游 README
```

## 1. 取代码

```bash
git clone <this-repo> ~/dsh-suite
cd ~/dsh-suite
```

## 2. Python 依赖

```bash
uv sync
```

会建 `.venv` 并装 `fastapi / uvicorn / fastmcp / httpx / psutil / pydantic`。

## 3. 前端

```bash
cd panel/frontend
npm ci
npm run build        # 产出 dist/，后端直接挂载
cd -
```

## 4. 配置

```bash
cp .env.example .env
```

最小改动：

```ini
# 你的域名（本机试用可留空，只用 127.0.0.1）
PUBLIC_BASE=http://127.0.0.1

# 站娘显示名（可以改成你喜欢的）
SUITE_AGENT_NAME=助手

# 对话接口用的模型
NEKO_CHAT_BASE_URL=https://api.deepseek.com
NEKO_CHAT_MODEL=deepseek-chat
DEEPSEEK_API_KEY=sk-...
```

> 面板侧另有一份模型配置在「管理员 → 助手对话」里（存 `panel.db` 的 settings 表），
> 与 `.env` 二选一，以面板设置为准。

## 5. 启动（不装 systemd 的最小验证）

```bash
# 终端 1：Hub
uv run python mcp-hub/hub.py 127.0.0.1 8765

# 终端 2：面板
uv run uvicorn app.main:app --app-dir panel/backend --port 8010
```

打开 `http://127.0.0.1:8010`：

1. 登录 `admin / admin123`
2. 立刻改密码（个人资料 → 修改密码）
3. 第一次进管理端会提示导入人格 —— 可以先用 `persona/example-persona.json` 试

## 6. 正式部署

```bash
sudo bash deploy/setup.sh
```

它会：装 systemd 单元 → 配 sudoers 白名单 → 建 `~/dsh-users` 与 `data/` → 装 nginx 路由 → 起服务。

```bash
systemctl status dsh-panel dsh-hub dsh-admin --no-pager
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8010/api/auth/me   # 401 = 服务活着
```

## 7. 接第一台设备

**方式 A：通用 Python Worker（零依赖）**

```bash
# 在另一台机器上
scp -r agent@server:~/dsh-suite/workers ~/workers && cd ~/workers
python3 worker.py --hub http://server:8765 --name my-laptop --capabilities code
```

首次运行会打印一个**配对码**（6 位）。在面板 →「智能体链接」输入它、审批，
Worker 拿到 token 后自动开始轮询。

**方式 B：设备连接器（单文件二进制，适合 Windows/Android）**

```bash
bash workers/build_connector.sh          # 产出 workers/dist-connector/bellhub-connector
./bellhub-connector pair --hub http://server:8765 --name my-pc
./bellhub-connector run
```

审批后这台设备出现在「设备」页，可以：

- 给它派活（「在 my-pc 上构建 xxx，把产物带回来」）
- 直接下发指令（由设备端 handler 解释）
- 看历史执行结果

## 8. 验证

```bash
bash tests/run_all.sh
```

18 套 / 561 项，全绿说明这套代码在这台机器上是自洽的。

## 常见问题

**`ModuleNotFoundError: app`**
在仓库根跑 `uv run uvicorn app.main:app --app-dir panel/backend`，别 `cd` 进 backend 再跑。

**面板 502 / 白屏**
前端没 build：`cd panel/frontend && npm run build`。

**Hub 起来了但 Worker 连不上**
先 `curl -s -o /dev/null -w '%{http_code}' http://<server>:8765/mcp`（401 = Hub 活着，只是要鉴权）；再确认 Worker 的 `--hub` 写的是**能被访问到**的地址，
不是 `127.0.0.1`（除非同机）。

**登录后一直跳回登录页**
Cookie 的 `Secure` 关闭、`SameSite=Lax`。用 `http://IP` 访问没问题；
用 HTTPS 反代时确保 `X-Forwarded-Proto` 透传。
