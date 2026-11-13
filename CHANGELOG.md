# 变更记录

格式：`类型(范围): 一句话`。日期为落地日期。

## 2026-11-13 · 首个开源版本

从一台长期运行的内部服务器整理而来，首次公开。

- **面板**：账号/配额/审计、网盘（分片续传/分享）、监控告警、智能体链接、工作站对话、委托板
- **MCP Hub**：26 个工具（任务、子任务、改派、黑板、对话、设备、沙箱、名册）+ Worker/Agent/Device REST 面
- **Worker/设备**：值守分身、伙伴分身、跨平台连接器（Windows/Linux/Termux）
- **对话总线**：线程 + 游标 + 长轮询 + 伙伴身份；值班应答面只读
- **知识库**：BM25 + 向量混合检索、MCP 五工具
- **运维插件**：防火墙、日志、系统更新、资源监控、告警、配额、委托、语音
- **QQ 桥**：NapCat / OneBot v11

开源整理时做的调整：

- 移除全部部署相关的个人数据、域名、账号与凭据，改为占位符 + `.env.example`
- **不带任何旧 git 历史**：单基线提交（旧历史里有真实 QQ 号与口令 MD5，且已清理的内容仍会随历史泄露）
- 人格显示名改为可配置（`SUITE_AGENT_NAME`），附示例人格
- 加固 `sandbox.resolve_safe` 的路径前缀比较（`/x/alice2` 不再被误判为 `/x/alice` 内）
- `tests/run_all.sh` 缺前端产物时会先尝试构建
- 补齐 README、架构/部署/配置/安全文档与第三方来源标注
- `ops/bb-append.py` 支持 `--author` / `DSH_BB_AUTHOR`（原署名硬编码）
- 清掉 qq-bridge 文档与代码里残留的真实 QQ 号与人名，管道号默认改为空
- `docs/security.md` 增加「日志会把凭据写下来」的部署建议；新增 `ops/README.md`
- **成本治理**：`docs/cost.md`；面板「今日成本」看板 + `cost_today` 告警；
  日成本闸 `SUITE_COST_DAILY_CAP_CNY`（面板对话与站娘任务在超限时直接拒绝）
- **黑板可分段读**：新增 `blackboard_sections` / `blackboard_tail` / `blackboard_search`，
  大键不必整读（本机那个 70KB 的索引键已拆成「索引 + 原样归档」）
- **移除美术资源**（立绘、两张背景图）：说不清来源与授权的图不放；UI 改用纯 CSS 渐变
