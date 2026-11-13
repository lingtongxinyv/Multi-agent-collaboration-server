# QQ 桥（NapCat / OneBot v11）

把助手接到 QQ：你在 QQ 里发一句，桥转给 `/api/chat`，助手回完再发回 QQ。

```
QQ(小号) ──NapCat──▶ POST 127.0.0.1:8770/onebot ──▶ panel /api/chat/*
   ▲                                                      │
   └──────── send_private_msg ◀── 桥 ◀── 回复 ─────────────┘
```

## 一、一次性准备（需要管理员动手）

1. **给助手开一个 QQ 小号**，在这台服务器上扫码登录（NapCat）。
2. 把**管理员自己**的 QQ 号填进 `data/qq-bridge/env` 的 `QQ_ALLOW`（白名单是「谁可以跟助手说话」，
   不是机器人账号）。

## 二、起 NapCat（协议端）

```bash
cd ~/dsh-suite/qq-bridge
docker compose up -d            # 首次会拉镜像（约 1~2G）
docker logs -f napcat           # 出现二维码就用小号手机 QQ 扫
```

扫码后进 WebUI（`http://127.0.0.1:6099`，token 在日志里）配置 OneBot：

- HTTP 服务：监听 `0.0.0.0:3000`，token 填 `ONEBOT_TOKEN`（没有就留空）；
- **HTTP 上报（Post）**：URL `http://127.0.0.1:8770/onebot`，Secret/token 填 `QQ_BRIDGE_TOKEN`。

> 容器用的是 host 网络，所以它能直接访问宿主 127.0.0.1。

## 三、起桥

```bash
sudo cp ~/dsh-suite/qq-bridge/dsh-qq-bridge.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now dsh-qq-bridge
systemctl status dsh-qq-bridge --no-pager
curl -s http://127.0.0.1:8770/health
```

## 四、配置（`data/qq-bridge/env`，0600）

| 变量 | 说明 |
|---|---|
| `QQ_BRIDGE_TOKEN` | 上报鉴权；NapCat 的「上报 Secret」要跟它一致 |
| `QQ_ALLOW` | 逗号分隔的 QQ 号白名单；**为空 = 全部拒绝** |
| `QQ_PEERS` | JSON：`{"QQ号":{"name":"显示名","token":"对话伙伴 token"}}`，让不同人用不同身份说话 |
| `ONEBOT_API` | NapCat OneBot HTTP，默认 `http://127.0.0.1:3000` |
| `ONEBOT_TOKEN` | NapCat 的 access token（调发送 API 用） |
| `CHAT_BASE` | 面板地址，默认 `http://127.0.0.1:8010` |
| `CHAT_TOKEN` | 对话伙伴 token（已在 `chat_token`，桥用它说话） |
| `DRY_RUN` | `1` = 不真发 QQ，只打日志 |
| `QQ_BRIDGE_PORT` | 默认 8770 |

改完 `sudo systemctl restart dsh-qq-bridge`。

## 四点五、多身份（谁在说话）

不同 QQ 号可以映射到不同的对话身份，助手会叫对名字：

```json
QQ_PEERS={"10001":{"name":"管理员","token":"<token-A>"},
          "10002":{"name":"成员B","token":"<token-B>"}}
```

每个身份对应一个 `chat_peers` 记录（token 在面板 `/api/chat/peers` 生成，或直接看
`data/qq-bridge/chat_token` / `xinyu_token`）。没配的号退回 `CHAT_TOKEN`。

> 鉴权兜底：NapCat 的 httpClient 实测**不发** `Authorization`（token 字段没生效），
> 桥只对 docker 网段 `172.19.*` 放行无 token 上报，其它来源仍必须带 token。

## 五、能用的命令

| 发什么 | 效果 |
|---|---|
| 直接说话 | 助手（值班应答，几秒回） |
| `/枢 内容` | 转给伙伴，异步；他回完会补发到 QQ |
| `/看` | 取这个对话里最新一条回复 |
| `/new` | 换新线程 |
| `/help` | 帮助 |

## 六、安全边界（都是硬编码）

- 白名单外的 QQ 一律忽略，不回「你没权限」——不暴露机器人存在；
- 上报必须带 `QQ_BRIDGE_TOKEN`，否则 401；
- 按 `message_id` 去重（OneBot 超时会重投），7 天 TTL；
- 群聊只在 **@助手** 时才处理，不做群广播；
- 输入截断 800 字、输出 3500 字；
- 助手的回复仍然受对话接口的每日 20 万 token 上限约束。

## 七、排障

```bash
journalctl -u dsh-qq-bridge -f          # 桥
docker logs -f napcat                   # 协议端
curl -s 127.0.0.1:8770/health           # 白名单数 / 线程数 / dry_run
```

- 收不到消息：看 NapCat 的上报 URL 是否 `127.0.0.1:8770/onebot`、Secret 是否一致；
- 有事件但没回复：看桥日志里 chat API 的报错（多半是 `CHAT_TOKEN` 失效）；
- 回复重复：检查 OneBot 是否重投，桥的去重表 `seen` 是否被清过。

## 八、已知限制

- **一个 QQ 号只能有一套 OneBot 消费者**：如果 伙伴-A 那边也要收同一个号的消息，
  得让 NapCat 走反向 WS 或由本桥做转发；更简单的做法是**各用各的号**。
- NapCat 是第三方协议端，有**风控/封号**风险；建议只上小号，不碰主号。

## 九、WS 订阅（外部伙伴实时收消息）

给 伙伴-A 这类伙伴用，不用轮询：

```text
wss://agent.example.com/qq-ws/ws?token=<对话伙伴 token>
# 或 Header: Authorization: Bearer <token>（推荐，避免 token 进 URL/日志）
```

连上后先收 `{"type":"hello"}`，之后每条：

```json
{"type":"qq_in","qq":"10002","name":"成员B","text":"…","ts":1792727305.1}
{"type":"qq_out","qq":10002,"text":"…","ts":1792727306.2}
```

- token 用任意有效 `chat_peers` 凭据（桥会拿它调一次 `/api/chat/health` 验证）；
- 无效直接 `4401` 关闭；
- 端点由 nginx `/qq-ws/` 反代到桥 `127.0.0.1:8770/ws`（Upgrade 头已配）；
- 订阅是**只读**：要说话走 `/api/chat` 或 QQ 本身。

实测（2026-10-23）：本地 `ws://` 与公网 `wss://` 同时订阅，均收到
`hello → qq_in → qq_out`。

## 十、NapCat Forward WS（OneBot v11，给外部客户端）

已开（2026-10-23）：`wss://agent.example.com/onebot-ws/?access_token=<ONEBOT_WS_TOKEN>`

- NapCat `websocketServers`：0.0.0.0:3001，`enableForcePushEvent=true`，heartInterval 30s；
- nginx `/onebot-ws/` → **本机转发器** `127.0.0.1:3001`（`dsh-napcat-ws-forward.service`）。
  转发器每次新连接用 `docker inspect` 解析容器 IP（缓存 10s），容器重建换 IP 也不会 502；
  `access_log off`，避免 access_token 进日志；
- token 在 `data/qq-bridge/env` 的 `ONEBOT_WS_TOKEN`；
- 与 HTTP 上报（桥）并存，但一个号不要再加第三个消费者。

反向 WS（NapCat 主动连外部服务器）未配；需要时加 `websocketClients` 并重启 NapCat。
