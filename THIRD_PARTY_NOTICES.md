# 第三方组件与来源标注

本仓库**只包含自研代码**。以下是运行时用到、但**不在本仓库内**的外部组件；
以及曾经参考过的公开资料。标注来源是为了避免版权争议，也方便使用者自己核对许可。

## 1. 运行时依赖（不随本仓分发）

| 组件 | 用途 | 许可 | 引入方式 |
|---|---|---|---|
| [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) | Agent 运行时（`dsh web` / `dsh headless`） | MIT | 独立仓库，clone 到同机 |
| [FastMCP](https://github.com/jlowin/fastmcp) / Starlette | MCP 服务与 ASGI | Apache-2.0 | `uv.lock` |
| FastAPI / uvicorn / pydantic / httpx / psutil | 面板后端 | MIT / BSD-3-Clause | `uv.lock` |
| [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) | 知识库的 MCP 端点 | MIT | `kb/requirements.txt` |
| numpy | 向量检索 | BSD-3-Clause | `kb/requirements.txt` |
| React / React DOM / React Router | 前端 | MIT | `panel/frontend/package-lock.json` |
| Vite / TypeScript | 前端构建 | MIT / Apache-2.0 | 同上 |
| [NapCat](https://github.com/NapNeko/NapCatQQ) | QQ 协议端（OneBot v11），可选 | 上游当前未声明 SPDX，以其仓库为准 | 仅以容器镜像名/版本引用；**本仓不含其代码或镜像资产** |
| [Ollama](https://ollama.com/) + nomic-embed-text | 知识库向量（可选） | MIT / Apache-2.0 | 本机模型 |
| PX4 / MAVLink | 无人机 SITL 与协议（可选） | BSD-3-Clause / LGPL | 外部安装 |

## 2. 明确**没有**打包进来的社区插件

部署时可能会用到这些 DSH 插件，但它们是别人的项目，本仓库不复制、不改写、不再分发：

| 插件 | 作者/来源 | 许可 | 说明 |
|---|---|---|---|
| `dsh-bili-scroll` | [c1overzz/dsh-bili-scroll](https://github.com/c1overzz/dsh-bili-scroll) | MIT | B 站日报插件 |
| `dsh-onebot-qq` | DSH 社区 | 见其包声明 | 另一个 QQ 通道实现；本仓的 `qq-bridge/` 是独立自研的 Python 桥，与其无关 |

如果要在自己的部署里用它们，请自己 `npm install` 并遵守其许可。

## 3. 参考过的公开资料

以下资料只用于理解协议/算法，**代码为自行实现**：

- OneBot v11 协议：<https://github.com/botuniverse/onebot-11>
- MAVLink 协议与 `pymavlink` 文档：<https://mavlink.io/>
- SQLite WAL 模式文档：<https://www.sqlite.org/wal.html>
- systemd 单元与 specifier：`man systemd.unit`、`man systemd.service`
- FastAPI / Starlette / FastMCP 官方文档

## 4. 本项目原创的部分

以下内容为本项目自行编写/制作，随本仓以 MIT 发布（美术资源见下）：

- 全部 Python / TypeScript / Shell 源码
- systemd 单元、nginx 路由、部署脚本
- `docs/` 下的文档
- `persona/example-persona.json` 示例人格

### 美术资源

**本仓库不包含任何第三方美术资源。**

- UI 背景：纯 CSS 渐变（`panel/frontend/src/styles.css`），无图片依赖；
- 图标：`panel/frontend/public/favicon.svg`，为本项目自绘的简单图形，随 MIT 发布；
- 站点立绘/形象挂件：**已移除**，不随开源仓分发。想接入自己的形象，
  把资源放到 `panel/frontend/public/` 并在 `index.html` 里挂上即可。

这样做的原因：开源仓里每一张图都应当能说清作者与许可，说不清的不放。

---

如果你认为本仓某处使用了你的代码而未标注，请开 issue，我们会补标注或移除。
