# 部署

面向单机、单用户的部署说明。目标形态：一台 Linux 服务器，nginx 占 80/443，其余全部本机。

## 1. 目录与用户

```
/home/agent/
├── dsh-suite/          本仓库
├── deepseek-harness/   上游 Agent 运行时
├── dsh-users/          每个用户的沙箱与网盘
└── .dsh-admin/         值守 Agent 的 DSH_HOME
```

用普通用户跑服务（`agent`），**不要用 root**。唯一以 root 跑的是 nginx 和 sudoers 白名单里的
几条 `systemctl restart`。

## 2. systemd 单元

| 文件 | 装到 | 说明 |
|---|---|---|
| `deploy/dsh-panel.service` | `/etc/systemd/system/` | 面板后端 |
| `deploy/dsh-hub.service` | 同上 | MCP Hub |
| `deploy/dsh-admin.service` | 同上 | 值守 Agent（`dsh web`，:3080） |
| `deploy/dsh-resident.service` | 同上 | 值守分身（轮询 Hub，可选） |
| `deploy/systemd/dsh-mavlink-*.service` | 同上 | MAVLink / SITL（可选） |

单元里的路径写成占位 `/home/agent/…`，`deploy/setup.sh` 安装时会替换成真实家目录；
单元同时会读 `~/dsh-suite/.env`（`EnvironmentFile=-`，缺失不报错）。
手工安装时自己 `sed` 一下。

```bash
sudo bash deploy/setup.sh
systemctl status dsh-panel dsh-hub dsh-admin --no-pager
journalctl -u dsh-panel -f
```

## 3. nginx

`deploy/nginx-dsh.conf` 是完整路由：

| 路径 | 上游 | 注意 |
|---|---|---|
| `/` | `127.0.0.1:8010` | 面板；SSE 必须关缓冲 |
| `/dsh/` | `127.0.0.1:3080` | 剥前缀 + 改写 cookie path |
| `/mcp-hub/` | `127.0.0.1:8765` | MCP streamable-http，长连接 |
| `/kb/` | `127.0.0.1:8787` | 可选；路由在 `kb/deploy/nginx-kb.conf`，不并入主配置 |

```bash
sudo cp deploy/nginx-dsh.conf /etc/nginx/sites-available/dsh
sudo ln -sf /etc/nginx/sites-available/dsh /etc/nginx/sites-enabled/dsh
sudo nginx -t && sudo systemctl reload nginx
```

**三处坑**（都踩过）：

1. **SSE 必须 `proxy_buffering off`**，否则工作站对话在工具执行期间被 nginx 判超时切断。
2. **`client_max_body_size` 要够大**（网盘分片上传），并且 `proxy_request_buffering off`。
3. **`/dsh/` 的 cookie path 要改写**，否则登录态在两个应用间串。

## 4. TLS

两种常见做法：

- **本机证书**（`certbot --nginx`）：直接给 nginx 上 443，最干净。
- **边缘隧道**（Cloudflare Tunnel / cpolar / frp / 类似）：TLS 在边缘终结，nginx 只听 80。
  本仓**不带**隧道单元（原部署用 cpolar，`dsh-panel` 的服务管理里保留了它的名字）——
  自己的隧道自己装，指向本机 80。
  此时面板 cookie 的 `Secure` 保持关闭（代码里就是 `secure=False`），
  并确认 `X-Forwarded-Proto` 有透传。

## 5. 对外暴露最小面

只放行 80/443。检查：

```bash
ss -ltnp | grep -v 127.0.0.1
sudo ufw status
```

Hub / 面板 / Agent UI 都只监听 `127.0.0.1`，靠 nginx 反代；
如果有人把 8765 直接开到公网，等于把 MCP 的 token 认证暴露给全世界爆破。

## 6. 开机自启与健康

```bash
systemctl enable dsh-panel dsh-hub dsh-admin nginx
systemctl is-active dsh-panel dsh-hub dsh-admin
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8010/api/auth/me   # 401 = 正常
```

面板「管理员 → 服务管理」可以一键重启白名单服务（`dsh-panel`、`dsh-hub`、`nginx`），
走的是 `/etc/sudoers.d/dsh-suite` 里那几条精确到 `.service` 的规则。

## 7. 备份

```bash
# 库 + 用户文件，一天一次足够
sqlite3 data/panel.db ".backup 'data/backups/panel-$(date +%F).db'"
rsync -a ~/dsh-users/ /backup/dsh-users/
```

`ops/backup-panel-db.sh` 是现成的（带 14 天轮转），配个 timer 即可。
**不要**在服务运行时直接 `cp panel.db`（WAL 模式下会拿到不一致快照），用 `.backup`。

## 8. 升级

```bash
cd ~/dsh-suite
git pull
uv sync
( cd panel/frontend && npm ci && npm run build )
sudo systemctl restart dsh-panel dsh-hub
bash tests/run_all.sh     # 升级后先自检，再放人用
```

数据库迁移是幂等的（`db.py` 里 `CREATE TABLE IF NOT EXISTS` + 列补齐），
但**升级前仍然备份**：迁移只保证向前，不保证回滚。
