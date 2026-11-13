#!/usr/bin/env bash
# Cloudflare Tunnel 一键迁移脚本
# 替代 cpolar 免费版（HK 节点不稳定、周期性 404）
#
# 前置条件：
#   1. 管理员需要在 Cloudflare 创建免费账号（dash.cloudflare.com）
#   2. 管理员需要把 example.com 的 NS 改指 Cloudflare（具体见下方提示）
#   3. 执行本脚本需 sudo（写 systemd unit、/usr/local/bin）
#
# 用法：sudo bash cf-tunnel-install.sh
# 过程中 `cloudflared tunnel login` 会开浏览器 → 管理员在 Cloudflare 选 example.com 域名授权
set -euo pipefail

CF_DIR="/etc/cloudflared"
CF_BIN="/usr/local/bin/cloudflared"
TUNNEL_NAME="dsh-tunnel"
DOMAIN="example.com"
SUBDOMAIN="agent"
NGINX_LOCAL="127.0.0.1:80"

echo "═══════════════════════════════════════════════"
echo " Cloudflare Tunnel 迁移安装"
echo " 替代：cpolar 免费版 HK 节点不稳定"
echo "═══════════════════════════════════════════════"

# —— Step 1: 下载 cloudflared ——
echo ""
echo "▶ Step 1: 下载 cloudflared..."
if [ -x "$CF_BIN" ]; then
  echo "  cloudflared 已安装：$($CF_BIN --version 2>&1)"
else
  VER=$(curl -sI https://github.com/cloudflare/cloudflared/releases/latest | grep -i '^location:' | sed 's/.*tag\///' | tr -d '\r\n')
  echo "  最新版本：$VER"
  curl -fSL "https://github.com/cloudflare/cloudflared/releases/download/${VER}/cloudflared-linux-amd64" -o "$CF_BIN"
  chmod +x "$CF_BIN"
  echo "  ✓ 下载完成：$($CF_BIN --version 2>&1)"
fi

# —— Step 2: 登录 Cloudflare ——
echo ""
echo "▶ Step 2: 登录 Cloudflare（会打开浏览器）..."
echo "  → 请选择域名：$DOMAIN"
echo "  → 如果还没有在 Cloudflare 添加 $DOMAIN，跳过本脚本先去 Cloudflare 添加"
echo ""
$CF_BIN tunnel login
echo "  ✓ 登录完成（证书保存在 ~/.cloudflared/）"

# —— Step 3: 创建隧道 ——
echo ""
echo "▶ Step 3: 创建隧道 '$TUNNEL_NAME'..."
TUNNEL_ID=$($CF_BIN tunnel create "$TUNNEL_NAME" 2>&1 | grep -oP '[0-9a-f]{32}' | head -1)
echo "  隧道 ID: $TUNNEL_ID"
mkdir -p "$CF_DIR"
TUNNEL_JSON="$HOME/.cloudflared/${TUNNEL_ID}.json"
cp "$TUNNEL_JSON" "$CF_DIR/${TUNNEL_ID}.json"
chmod 600 "$CF_DIR/${TUNNEL_ID}.json"
echo "  ✓ 凭据已复制到 $CF_DIR/"

# —— Step 4: 写 config.yml ——
echo ""
echo "▶ Step 4: 写 config.yml..."
cat > "$CF_DIR/config.yml" <<EOF
# Cloudflare Tunnel 配置 —— DSH Suite
# 由 cf-tunnel-install.sh 自动生成，2026-10-15
tunnel: ${TUNNEL_NAME}
credentials-file: ${CF_DIR}/${TUNNEL_ID}.json

# HTTPS 入口（Cloudflare 自动颁发/续期证书）
ingress:
  - hostname: ${SUBDOMAIN}.${DOMAIN}
    service: http://${NGINX_LOCAL}
  # HTTP 自动跳 HTTPS
  - hostname: ${SUBDOMAIN}.${DOMAIN}
    service: http://${NGINX_LOCAL}
  - service: http_status:404
EOF
echo "  ✓ config.yml 已写"

# —— Step 5: 写 DNS 记录 ——
echo ""
echo "▶ Step 5: 写 Cloudflare DNS 记录（CNAME）..."
echo "  （这会在 Cloudflare 为 agent.$DOMAIN 添加 CNAME → ${TUNNEL_ID}.cfargotunnel.com）"
echo ""
$CF_BIN tunnel route dns "$TUNNEL_NAME" "${SUBDOMAIN}.${DOMAIN}"
echo "  ✓ DNS 记录已写"

# —— Step 6: 写 systemd unit ——
echo ""
echo "▶ Step 6: 安装 systemd 服务..."
cat > /etc/systemd/system/cf-tunnel.service <<EOF
[Unit]
Description=Cloudflare Tunnel for DSH Suite (agent.${DOMAIN})
After=network.target nginx.service

[Service]
Type=simple
ExecStart=${CF_BIN} tunnel --config ${CF_DIR}/config.yml run ${TUNNEL_NAME}
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable cf-tunnel.service
echo "  ✓ cf-tunnel.service 已 enable"

# —— Step 7: 停 cpolar，起 cloudflared ——
echo ""
echo "▶ Step 7: 停止 cpolar，启动 Cloudflare Tunnel..."
echo "  （cpolar 会先 disable 避免开机自动恢复）"
systemctl disable cpolar-dsh.service 2>/dev/null || true
systemctl stop cpolar-dsh.service 2>/dev/null || true
pkill -9 cpolar 2>/dev/null || true

systemctl start cf-tunnel.service
sleep 5

# —— Step 8: 验证 ——
echo ""
echo "═══════════════════════════════════════════════"
echo " 验证阶段（10 次 HTTP+HTTPS 探针）"
echo "═══════════════════════════════════════════════"
H_OK=0; T_OK=0
for i in 1 2 3 4 5 6 7 8 9 10; do
  H=$(curl -sS -o /dev/null -w "%{http_code}" --max-time 10 "https://${SUBDOMAIN}.${DOMAIN}/" 2>/dev/null)
  T=$(curl -sS -o /dev/null -w "%{http_code}" --max-time 10 "http://${SUBDOMAIN}.${DOMAIN}/" 2>/dev/null)
  [ "${H:0:1}" = "2" ] && H_OK=$((H_OK+1))
  [ "${T:0:1}" = "2" ] && T_OK=$((T_OK+1))
  SYMB=$([ "${H:0:1}" = "2" ] && [ "${T:0:1}" = "2" ] && echo "✅" || echo "❌")
  echo "  [$i] $SYMB HTTPS=$H HTTP=$T"
  sleep 3
done

echo ""
echo "═══════════════════════════════════════════════"
echo " 完成！"
echo "═══════════════════════════════════════════════"
echo ""
echo " Cloudflare Tunnel 状态："
echo "   HTTPS: ${H_OK}/10 通过"
echo "   HTTP:  ${T_OK}/10 通过"
echo ""
echo " systemd 状态："
systemctl status cf-tunnel.service 2>&1 | head -6
echo ""
echo " cpolar-dsh 已 disable（不再开机自启）"
echo ""
echo " 管理命令："
echo "   sudo systemctl status cf-tunnel     # 查看状态"
echo "   sudo journalctl -u cf-tunnel -f     # 看实时日志"
echo "   sudo systemctl restart cf-tunnel    # 手动重启"
echo ""
echo " 如果 Cloudflare DNS 还没生效（TTL 0-24h），先等 DNS 同步再测"
echo "═══════════════════════════════════════════════"
