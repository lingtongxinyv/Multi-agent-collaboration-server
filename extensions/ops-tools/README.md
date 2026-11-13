# ops-tools（DSH 插件）

给 [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) 加一组**服务器运维工具**，
让值守 Agent 能直接看告警、查日志、管防火墙、发布委托，而不必每次都写 shell。

## 工具

| 工具 | 作用 |
|---|---|
| `firewall_manage` | UFW 规则：状态 / 放行 / 拒绝 / 删除 / 启停（需 root） |
| `log_inspect` | journald / auth.log / syslog：最近、搜索、失败登录、sudo 记录 |
| `system_update` | 列出可更新包、装更新、只装安全更新、清理旧包 |
| `resource_monitor` | CPU / 内存 / 磁盘 / 网络 / 进程快照 |
| `metrics_history` | 面板采集的历史指标（趋势分析用） |
| `alert_list` / `alert_ack` | 读/确认面板的未处理告警 |
| `quota_overview` | 各账号的配额使用（token/日、并发、磁盘） |
| `quest_board` / `quest_publish` | 读委托板 / 往委托板挂一条需要人处理的事 |
| `voice_asr` / `voice_tts` | 接外部语音网关的 ASR/TTS（可选） |

## 安装

1. 把 `src/` 放到你的 DSH 源码树里（例如 `~/deepseek-harness/ops-plugins/src/`）。
2. 在 DSH profile 的 patch 里挂上：

```yaml
# ~/.dsh/profiles/web/cordis.patch.yml
- insert:
    - id: ops-tools
      name: '<绝对路径>/ops-plugins/src/index.ts'
```

3. 重启 DSH，`/tools` 里应能看到上面这些工具。

`voice_asr` / `voice_tts` 需要额外的语音网关，环境变量见 `src/voice.ts` 顶部注释；
不配就不注册可用端点。

## 依赖的本地约定

这些工具读的是**本仓的本地布局**（可用环境变量覆盖）：

- `~/dsh-suite/data/panel.db` —— 面板库（告警、配额、指标）
- `~/dsh-suite/data/quest-publish.token` —— 委托板发布凭据（600）
- `~/dsh-suite/data/voice_service.token` —— 语音网关令牌（可选）

## 安全

- `firewall_manage` / `system_update` / `alert_ack` 需要 root；插件本身不做提权，
  它假设宿主进程有权限（见主仓 `docs/security.md` 对 root 值守 Agent 的说明）。
- `quest_publish` 只读 token 文件，不落库、不进 git。
