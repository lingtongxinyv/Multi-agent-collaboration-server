# tests/ —— 铃枢「智能体链接」验收套件

一条命令跑完全部：

```bash
bash tests/run_all.sh            # 全部套件（源码版）
bash tests/run_all.sh --bin      # 额外用打包好的单文件二进制再跑一遍端到端
bash tests/run_all.sh --only test_link_api    # 只跑一套（排障用）
```

面板管理端「服务管理 → 智能体链接自检」页也可以一键运行（同一脚本，管理员可见）。

## 铁律（写新套件时必须遵守）

1. **隔离临时库**：`os.environ["DSH_SUITE_DATA"] = tempfile.mkdtemp(...)`，
   并立刻 `assert str(D.DB_PATH).startswith(str(ROOT))`。
   以 root 跑脚本时 `Path.home()` 会指向 `/root`，不锁就会连到影子库并污染真实数据（铁律 #2）。
2. **相对定位**：用文件头部的 `REPO / BACKEND / WORKERS / MCPHUB` 常量，
   不要写 `$HOME/...` 这种绝对路径——换机器/换用户就废。
3. **起真实服务用子进程**：`uvicorn app.main:app`、`mcp-hub/hub.py` 都真起，
   走真 HTTP，不用 mock（本项目多次证明"看起来对"和"真的通"差很远）。
4. **断言前先读实现的分支**：产品常常比想象更严或更宽。
   已踩三次：能力标签传了没注册的、用会话 cookie 访问只认 agent 凭据的端点、
   以为 `battery; touch X` 会执行 battery（实际被整体拒绝）。
5. **清理自身残留**：测试只准写临时目录；跑完不得在仓库留下文件或改属主。

## 现有套件

| 套件 | 项数 | 覆盖 |
|---|---|---|
| `test_link_unit.py` | 37 | 配对码生命周期、防重放、token 轮换、撞名、过期、撤销、跨用户隔离 |
| `test_link_api.py` | 64 | 真实 HTTP：限流、401、审批幂等、名册、派活、越权 404、版本自检、取件、预编译分发 |
| `test_connector_e2e.py` | 37 | pair 子进程真配对、`run --once` 真派活、自定义适配器、高危闸门、`doctor`、自升级、撤销自知 |
| `test_hub_scoping.py` | 40 | MCP JSON-RPC：匿名 401、全局=管理员、用户凭据只碰自己、站娘工具开关、一站派活/收活/改派 |
| `test_termux_handler.py` | 20 | 安卓白名单 handler：动作映射、独立 argv、**注入矩阵**、缺依赖提示 |
| `test_device_autostart.py` | 25 | 自启（Termux/ systemd）、`doctor` 四类结论、不污染源码目录 |
| `test_drive_unit.py` | 79 | 网盘单元：路径校验、配额、流式上传/Range/分享、一次性直传 |
| `test_drive_api.py` | 36 | 网盘接口（隔离版）：上传下载、Range、穿越防护、中文/emoji 文件名、分享、匿名拒绝、管理员视图 |
| `test_chunked_upload.py` | 36 | 分片上传（隔离版）：init/chunk/complete、断线续传、幂等、缺片拒拼、越界与超大分片拦截、管理端清理 |
| `test_new_user_journey.py` | 23 | **新用户全程**：下载→pair→确认→run，每一步都真跑一遍 |
| `test_lifecycle.py` | 18 | **长期运行家务**：过期配对码清理（带宽限期）、僵尸子任务收尸（领了活没回报）、回收后可重新派活、housekeeping 幂等 |
| `test_pairing_races.py` | 13 | **配对竞态**：两人抢同一码只能一人成功、并发取件只发一次凭据、同用户两机互不覆盖、两用户同名不串号、重复配对轮换 |
| `test_connector_offline.py` | 13 | **离线韧性**：hub 不在时不崩、`--once` 如实非零退出（9）、`doctor` 给排查顺序、**同一进程内**先退避后自愈 |
| `test_docs_drift.py` | 56 | **文档漂移检查**：UI 承诺的命令/动词/内置指令/文件名/配置字段/能力，必须与实现一致；无旧文案；源码改完必须已重新构建 |

合计 **497 项**（14 套，源码版）+ `--bin` 再跑 37 项 = **534 项**；
另有手动重测 `heavy_resume_large.py` 18 项（含它则为 552）。

> 漂移检查器可用 `BELLHUB_DRIFT_ROOT=<临时副本>` 指向一份注入漂移的副本；
> 已验证三种注入都会**非零退出**：① UI 提到不存在的子命令；② 旧文案"三步接入"；
> ③ 把 `termux_handler.py` 从下载白名单里移除（用户会下到 404）。会红的检查才有意义。
>
> 同理，`test_pairing_races` 也做过**敏感性验证**：把 `approve_pair` 改成非原子并 `sleep(0.3)`
> 放大竞态窗口后，该套件立刻红（两人同时 approve 都拿到 200）—— 证明它不是"写了但测不到"。

> 更正记录：2026-10-14 早先本文件写过的 "415/452" 是**加法错误**（223+79+36 应为 338）。
> 现在这条是逐套相加核对过的；**验收数字也要复核**，写了总数就回头加一遍。

### 不列入 run_all 的两类

| 文件 | 为什么 |
|---|---|
| `heavy_resume_large.py` | 跑大文件吞吐（默认 100MB，`BELLHUB_HEAVY_MB` 可调）。**手动跑**，不该拖慢一键自检。18 项。 |
| （历史）`test_router_handler.py` | 它测的 `router_handler.py` 不在本仓库（是 desktop-01 侧的路由脚本），故不纳入本仓库验收 |

> 网盘那两套原来只能人工跑（连真实库 + 线上 :8010 + 真 admin 的签名 cookie）。
> 2026-10-14 改造：`test_drive_api.py` 自己起隔离服务（临时 `DSH_SUITE_DATA` +
> 临时 `DSH_DRIVE_ROOT`，端口 8098），`drive.py` 的存储根因此新增了 `DSH_DRIVE_ROOT` 覆盖项。

## 元测试（不在 run_all 里，防递归）

`meta_selftest_check.py` 验证面板「服务管理 → 智能体链接 · 验收自检」端点是真能跑完整套：

```bash
~/.local/bin/uv run --project ~/dsh-suite python tests/meta_selftest_check.py
```

它刻意用 `meta_` 前缀而不是 `test_`，并且**不能**加进 `run_all.sh` 的 SUITES ——
面板自检会调 `run_all.sh`，一旦把本文件列进去就成了无限递归。它用 8097 端口避开各套件的 8093–8103。

### 浏览器验收（root 手动运行）

`meta_ui_check.py` + `meta_ui_agents.mjs`：用无头 Chromium 真点一遍「我的智能体」页（24 项）——
登录、四步向导文案（含"没有残留三步旧文案"的漂移检查）、下载与复制按钮、
**填真配对码点确认接入 → 刷新看名册**、用页面上的「派活」表单派一条活、
**依次点「测试连接」「复制」「撤销」并核对反馈与空状态**、**填无效配对码要有明确提示**、
手机宽度(390px)无横向溢出、登录后无失败请求。

```bash
$HOME/dsh-suite/.venv/bin/python tests/meta_ui_check.py
```

不列入 `run_all.sh` 的原因：依赖 Playwright（装在 `/root/1012/visual/node_modules`，root-only），
而且要真起浏览器，比其余套件重。它用 8092 端口。
