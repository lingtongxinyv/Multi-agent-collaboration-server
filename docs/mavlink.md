# MAVLink WSS 中继与 Windows 本地桥（P3，2026-10-20）

状态：**服务器侧已落地并验证**（中继、nginx、token、桥修复、systemd/探针）；Windows 桥只出设计，本轮不落码。
范围：只动服务器与仓库，不动管理员本机；cpolar 备用 TCP 通道本轮不配置（现有 HTTP 隧道即主链路）。

## 1. 拓扑

```
QGC (Windows)
  │  TCP 127.0.0.1:15760           ← 固定端点，换网/换 IP 都不变
  ▼
Windows 本地桥（P4 落码）
  │  wss://agent.example.com/mavlink?token=<一次性>
  ▼
隧道边缘（TLS 终结）→ 本机 :80
  ▼
nginx  location = /mavlink          ← Upgrade + 无 token 直接 403 + access_log off
  │  http://127.0.0.1:8766
  ▼
中继 relay.py（systemd: dsh-mavlink-relay）
  │  TCP 127.0.0.1:5760（目标写死，握手后 getpeername 复核）
  ▼
容器 px4-t4 内 /bridge.py（修复版）→ UDP 127.0.0.1:18570 ↔ PX4 SITL
```

公网暴露面：仍然只有 nginx 的 443/80；没有新增公网端口。中继只监听回环 8766。

## 2. 文件与职责

| 文件 | 职责 |
| --- | --- |
| `ops/mavlink/tokens.py` | 一次性 token 签发/校验/吊销；只存 SHA-256 摘要，flock + 原子替换 |
| `ops/mavlink/relay.py` | WS 服务端（stdlib asyncio）：握手校验 token → 连 5760 → 双向字节搬运 |
| `ops/px4/mavtcp.py` | 修复后的 `/bridge.py` 源；宿主 `/root/PX4/.mavtcp.py` ← 部署目标 |
| `ops/sitl-probe.sh` | 30s 探针：容器 / 5760 心跳 / 中继单元，双探 + 冷却 + 黑板留痕 |
| `deploy/nginx-dsh.conf` | 站点配置仓库副本（与线上 `/etc/nginx/sites-available/dsh` 同步） |
| `deploy/systemd/dsh-{mavlink-relay,sitl,sitl-probe}.service`、`dsh-sitl-probe.timer` | 托管单元 |

## 3. 端口与边界（硬编码，不接受配置/模型输入改写）

| 端点 | 方向 | 约束 |
| --- | --- | --- |
| `127.0.0.1:8766` | 中继监听 | 只回环；并发上限 4；非 `/mavlink` 路径 404；公网 token 走 `?token=`（本机调试也可用 `X-Mavlink-Token` 头） |
| `127.0.0.1:5760` | 中继后端 | 常量；连接后 `getpeername()` 复核；不匹配直接 502 |
| `127.0.0.1:15760` | Windows 桥监听 | 只回环；QGC 唯一入口（P4） |
| `0.0.0.0:5760` | 容器内桥监听 | 容器 `--network host` 时即宿主 5760；**不映射公网** |
| 443/80 | 公网 | 仅经 nginx；`/mavlink` 无 token → 403 |

## 4. Token 生命周期

1. 签发：`python3 ops/mavlink/tokens.py mint --label qgc-win --ttl 1800`，明文只打印一次。
2. 存储：`data/mavlink-tokens.json`（0600），键为 `sha256(token)`，值为 `{created, expires, label, used_at}`。
3. 校验顺序（关键）：`check()` 只读校验 → 建后端 TCP → `consume()` 原子核销 → 才回 `101`。
   - 后端不可用时回 502，**token 不烧**，客户端可用同一 token 重试；
   - 并发抢同一 token：先到者核销，后到者 401 `already_used`。
4. 过期：`expires` 到点即 401 `expired`；核销后重放 401 `already_used`。
5. 吊销：`tokens.py revoke --all`（事故处置，不需要重启中继）。
6. 携带方式（实测约束）：**公网只能走 `?token=`**。2026-10-20 用临时调试日志实测：cpolar 边缘在
   WS 升级请求上会丢弃自定义头（`X-Mavlink-Token` 与 `Authorization` 到了 nginx 都是空），只有 URI 原样保留；
   非升级请求反而能带上头。故公网入口按 `$arg_token` 判空，日志侧用两条封死凭据：
   `access_log off`（不记 URI）+ `error_log ... crit`（上游报错也不带 URI）。中继仍同时接受
   `X-Mavlink-Token` 头，供本机直连调试。

## 5. WSS 协议细节

- 客户端必须发二进制帧；文本帧回 close 1003，未掩码/超 1 MiB/保留位 1002/1009。
- 中继按字节流转发，不解析 MAVLink；QGC 侧看到的是一条普通 TCP 链路。
- 中继每 15s 发 WS ping；60s 无任何客户端流量则断链（防止半开连接占满 4 个槽）。
- 后端 EOF / 客户端 EOF / 协议错误都会同时关双向并记录 `dur/bytes/reason`。

## 6. nginx 要点

```nginx
location = /mavlink {
    if ($arg_token = "") { return 403; }   # 公网只认 query 携带的 token
    access_log off;                        # token 绝不落访问日志
    error_log /var/log/nginx/mavlink-error.log crit;  # 上游报错也不带 URI
    proxy_pass http://127.0.0.1:8766;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_read_timeout 3600s;
    proxy_buffering off;
}
```

一次性/过期判定只在中继一处，nginx 不复制状态（避免两处漂移）。

## 7. Windows 侧 127.0.0.1:15760 桥（设计，本轮不落码）

### 7.1 落点

- 由 Windows 上的 local-dsh 以**子进程**方式拉起（与 px4-panel 同宿主、同生命周期），第一版不做 Windows 服务/开机自启；local-dsh 崩溃时桥随之退出，由 QGC 侧显示"链路断开"而不是静默半死。
- 端口被占用时**显式失败并上报**，不自动换端口（换端口会让 QGC 的链路配置漂移成"看起来连上了其实连错"）。
- 只 `bind 127.0.0.1:15760`，禁止 `0.0.0.0`（否则同网段任何人都能接管仿真机）。

### 7.2 数据面

- 纯字节管道：QGC ↔ TCP ↔ 桥 ↔ WS 二进制帧。**不解析 MAVLink、不做命令过滤**——闸门在中继/插件层，桥越"哑"越好排障。
- 每条 WS binary message ≤1 KiB（MAVLink 帧 8~280 字节；分片由中继按流处理）。
- token 放 URL query：Windows 桥连接 `wss://agent.example.com/mavlink?token=…`。这不是偏好而是
  边缘约束——cpolar 在 WS 升级时会丢自定义头（见 4.6）；凭据落日志的风险已由 nginx 两条配置封住，
  且 token 一次性 + 30 分钟 TTL，泄露窗口仅限"握手前"。
- 上行写入失败/下行 3s 无字节 → 关闭 QGC 侧连接并进入重连（QGC 会自己重握手）。

### 7.3 控制面

- token：启动时向面板申请（TTL 30min，一次性）；重连**必须重新申请**（旧 token 已核销）。收到 401/403 不无限重试，转为 `auth-required` 状态并在 local-dsh UI 提示"需要重新授权"。
- 重连退避：1s→2s→4s→8s→16s→30s 封顶 + 0~500ms 抖动；连续失败 10 次进入 5 分钟冷却。
- 状态上报给 local-dsh：`{state: connecting|up|auth-required|backoff, since, last_rx_at, ws_bytes_in/out, tcp_bytes_in/out, token_expires_at}`。
- 桥自身每 30s 发 WS ping；中继 60s 无流量断链，两端行为对称。

### 7.4 失败模式

| 现象 | 判定 | 动作 |
| --- | --- | --- |
| 15760 占用 | 配置冲突 | 启动失败 + UI 报错，不换端口 |
| 401 `already_used`/`expired` | token 生命周期问题 | 停止重试，提示重新授权 |
| 502 | 服务器侧无后端（SITL 掉） | 30s 退避重试；UI 显示"仿真机离线" |
| WS 101 后 3s 无下行 | 链路半死 | 断开重连 |
| 系统休眠唤醒 | 连接已死 | 立即重连 + 重新申请 token |

### 7.5 与 local-dsh 的接口（建议）

- `bridge.state.json`（0600，每 2s 原子写）+ 可选本地 HTTP `GET /healthz`：供 px4-panel 读取；
- local-dsh 在对话卡片上显示链路状态，不把状态塞进模型提示（避免把"链路正常"误当授权）。

## 8. 限高常量（管理员裁定：3m）

SITL 演示的硬上限写进代码常量，**超界直接拒绝、不截断**（截断会让操作者以为指令生效）：

```python
# P1 `px4-panel` 插件常量（本轮由 P1 落码，P3 只做设计登记）
MAX_ALT_M = 3.0            # 相对起飞点高度上限（m），TAKEOFF 与后续 SET_POSITION 共用
ALT_HARD_REJECT = True      # 超界 → 拒绝并回报原因，绝不 clamp
COMMAND_WHITELIST = {ARM, DISARM, TAKEOFF, LAND, HOLD, MODE}
```

- 单位：米；参考系：相对起飞点（relative alt），不是 AMSL。
- 常量不来自配置、不来自模型、不来自黑板；改它必须改代码并重启（与"中继即闸门"同一原则）。
- 确认卡上必须显示 `MAX_ALT_M` 与本次请求高度，超界时"确认"按钮直接不可用。

## 9. 验证记录（2026-10-20）

- 桥崩溃复现：容器内跑原版 `/bridge.py`（15761 端口 + 模拟 UDP 流）压测，**精确复现**
  `ValueError: list.remove(x): x not in list`（第 61 行，与 08:50 生产事故同一行）；修复版同压力 20s 无崩溃、持续转发。
- 生产容器日志存证：`docker logs px4-t4` 第 15~18 行即该 ValueError（restarts=1 的来源）。
- 中继单测：`tests/test_mavlink_relay.py` 6 项通过（一次性/过期/未知/缺失 token、后端故障不烧 token、双向字节流、文本帧 1003、错误路径 404）。
- 端到端：`wss://agent.example.com/mavlink` → 读到 SITL HEARTBEAT；同 token 二次连接 401。
- 探针：`ops/sitl-probe.sh --status` 显示 `frames=683 heartbeat=12`（4s 采样）。

## 10. 回滚

| 改动 | 回滚 |
| --- | --- |
| nginx `/mavlink` | 还原 `/etc/nginx/sites-available/dsh.bak-<时间戳>` + `nginx -t && nginx -s reload` |
| 中继 | `systemctl disable --now dsh-mavlink-relay` |
| 桥 | 还原 `/root/PX4/.mavtcp.py.bak-<时间戳>` + `systemctl restart dsh-sitl` |
| token | `tokens.py revoke --all` |
| 探针 | `systemctl disable --now dsh-sitl-probe.timer` |

## 11. 待批/未做

- Windows 桥落码（P4，需管理员分配落点给 local-dsh 侧）。
- 中继侧命令闸（ARM/TAKEOFF 过滤）：等批 C 写指令落地后与插件闸门一起设计，现在加会误伤 QGC 正常链路。
- cpolar 固定 TCP 备用通道：本轮按裁定不配置。
- 面板签发 token 的 API：本轮用 CLI（助手可代签），P4 随桥一起加面板端点。
