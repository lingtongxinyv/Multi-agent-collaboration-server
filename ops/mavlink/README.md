# ops/mavlink — WSS 中继运行手册

设计背景见 `docs/mavlink.md`。这里只写“怎么用/怎么排障”。

## 常用命令

```bash
# 签发一次性 token（TTL 30 分钟，明文只出现一次）
python3 $HOME/dsh-suite/ops/mavlink/tokens.py mint --label qgc-win
python3 $HOME/dsh-suite/ops/mavlink/tokens.py mint --label qgc-win --ttl 1800 --json

# 看条目（used/expired/live）
python3 $HOME/dsh-suite/ops/mavlink/tokens.py list

# 事故处置：全部吊销
python3 $HOME/dsh-suite/ops/mavlink/tokens.py revoke --all

# 服务状态
systemctl status dsh-mavlink-relay --no-pager
journalctl -u dsh-mavlink-relay -n 100 --no-pager
bash $HOME/dsh-suite/ops/sitl-probe.sh --status   # 容器/心跳/中继三路
bash $HOME/dsh-suite/ops/sitl-probe.sh --once     # 只探不动作

# 端到端自测（服务器上执行；token 现场签发）
TOKEN=$(python3 $HOME/dsh-suite/ops/mavlink/tokens.py mint --label selftest)
python3 - "$TOKEN" <<'PY'
import socket, ssl, sys, base64, os
tok = sys.argv[1]
ctx = ssl.create_default_context()
raw = socket.create_connection(("agent.example.com", 443), timeout=10)
s = ctx.wrap_socket(raw, server_hostname="agent.example.com")
key = base64.b64encode(os.urandom(16)).decode()
# 公网必须走 query：cpolar 在 WS 升级请求上会丢掉自定义头（2026-10-20 实测）
s.sendall((f"GET /mavlink?token={tok} HTTP/1.1\r\nHost: agent.example.com\r\n"
           "Upgrade: websocket\r\nConnection: Upgrade\r\n"
           f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
print(s.recv(200).split(b"\r\n")[0])
print("frame bytes:", len(s.recv(2048)))
PY
```

## 排障对照

| 现象 | 查什么 |
| --- | --- |
| 握手 403 | nginx 没带 `?token=`（公网只认 query，自定义头会被 cpolar 丢） |
| 握手 401 `unknown`/`malformed` | token 打错或已被吊销 |
| 握手 401 `already_used` | 一次性凭据被复用，重新签发 |
| 握手 401 `expired` | 超过 30 分钟，重新签发 |
| 握手 502 | 中继连不上 5760：`docker ps` / `ss -ltnp | grep 5760` / `sitl-probe.sh --status` |
| 101 后没有数据 | SITL 没起流：`docker logs px4-t4 --tail 50`，或探针看 heartbeat |
| 连接频繁断 | 看中继日志 `reason=`；`keepalive timeout` 表示客户端 60s 无响应 |

## 部署位置

| 源 | 目标 |
| --- | --- |
| `ops/mavlink/relay.py` | `$HOME/dsh-suite/ops/mavlink/relay.py` |
| `ops/mavlink/tokens.py` | `$HOME/dsh-suite/ops/mavlink/tokens.py` |
| `ops/px4/mavtcp.py` | `/root/PX4/.mavtcp.py`（容器 `/bridge.py` 的 ro bind 源），改完 `systemctl restart dsh-sitl` |
| `ops/sitl-probe.sh` | `$HOME/dsh-suite/ops/sitl-probe.sh` |

注意：中继同时支持 `X-Mavlink-Token` 头，但**只在本机直连 8766 时有效**；公网经 cpolar 的 WS 升级
会被剥掉自定义头，必须用 `?token=`。

注意：`/root/PX4/.mavtcp.py` 是**单文件 bind mount**，用 `cat >` 原地写（保留 inode）再重启容器；
`mv` 换 inode 会让运行中的容器仍挂旧文件。
