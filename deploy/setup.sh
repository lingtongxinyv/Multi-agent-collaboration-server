#!/bin/bash
# DSH Suite 一键部署 — 需要 sudo 运行
# 用法: sudo bash $HOME/dsh-suite/deploy/setup.sh
set -e

REAL_USER="${SUDO_USER:-$USER}"
HOME_DIR="/home/${REAL_USER}"
SUITE="${HOME_DIR}/dsh-suite"
REPO="${HOME_DIR}/deepseek-harness"

echo "========================================="
echo " DSH Suite 部署（单实例 DSH + 面板 + Hub）"
echo "========================================="

# ─── 1. 依赖准备 ─────────────────────────────────────────────
echo "[1/7] 依赖准备..."
if ! command -v uv >/dev/null; then
  echo "  uv 缺失，跳过（假定已手动安装）"
else
  sudo -u "${REAL_USER}" bash -lc "cd ${SUITE} && ~/.local/bin/uv sync" >/dev/null 2>&1 || \
  sudo -u "${REAL_USER}" "${HOME}/.local/bin/uv" sync --project "${SUITE}" >/dev/null 2>&1 || true
fi
echo "  ✓ Python 依赖（uv sync）"

# ─── 2. 停用旧架构服务 ───────────────────────────────────────
echo "[2/7] 停用旧架构服务..."
systemctl stop dsh-user.service 2>/dev/null || true
systemctl disable dsh-user.service 2>/dev/null || true
rm -f /etc/systemd/system/dsh-user.service
systemctl daemon-reload
echo "  ✓ dsh-user（:3081 双实例旧架构）已停用移除"

# ─── 3. 安装 systemd 单元 ────────────────────────────────────
echo "[3/7] 安装 systemd 单元..."
# 单元里写的是占位路径 /home/agent，安装时换成真实家目录
for unit in dsh-admin dsh-panel dsh-hub; do
  sed "s|/home/agent|${HOME_DIR}|g" "${SUITE}/deploy/${unit}.service" \
    > "/etc/systemd/system/${unit}.service"
done
# 值守分身是可选件：有配置才装，没配置不装（避免起来就空转）
if [ -f "${SUITE}/workers/resident.config.json" ]; then
  sed "s|/home/agent|${HOME_DIR}|g" "${SUITE}/deploy/dsh-resident.service" \
    > /etc/systemd/system/dsh-resident.service
  echo "  ✓ dsh-resident（检测到 workers/resident.config.json）"
else
  echo "  · 跳过 dsh-resident（未生成 workers/resident.config.json；首次运行 worker 会自动生成）"
fi
systemctl daemon-reload
echo "  ✓ dsh-admin (:3080) / dsh-panel (:8010) / dsh-hub (:8765)"

# ─── 4. sudoers（面板服务管理白名单） ────────────────────────
echo "[4/7] sudoers 白名单..."
cat > /etc/sudoers.d/dsh-suite <<EOF
${REAL_USER} ALL=(root) NOPASSWD: /usr/bin/systemctl restart dsh-panel.service, /usr/bin/systemctl restart dsh-hub.service, /usr/bin/systemctl restart nginx.service
# 如果你自建了隧道单元（cpolar / cloudflared / frp），把它的 restart 也加进上面这一行
EOF
chmod 440 /etc/sudoers.d/dsh-suite
echo "  ✓ agent 可免密重启白名单服务"

# ─── 5. 沙箱与数据目录 ───────────────────────────────────────
echo "[5/7] 沙箱与数据目录..."
mkdir -p "${HOME_DIR}/dsh-users"
chown "${REAL_USER}:${REAL_USER}" "${HOME_DIR}/dsh-users"
chmod 750 "${HOME_DIR}/dsh-users"
mkdir -p "${SUITE}/data"
chown -R "${REAL_USER}:${REAL_USER}" "${SUITE}/data"
echo "  ✓ ~/dsh-users（用户沙箱根）与 ${SUITE}/data"

# ─── 6. nginx ────────────────────────────────────────────────
echo "[6/7] nginx 路由..."
cp "${SUITE}/deploy/nginx-dsh.conf" /etc/nginx/sites-available/dsh
ln -sf /etc/nginx/sites-available/dsh /etc/nginx/sites-enabled/dsh
nginx -t
echo "  ✓ / → 8010 面板，/dsh/ → 3080 DSH，/mcp-hub/ → 8765 Hub"

# ─── 7. 启动 ─────────────────────────────────────────────────
echo "[7/7] 启动服务..."
systemctl enable dsh-hub.service dsh-panel.service dsh-admin.service 2>/dev/null || true
# 隧道不在本仓：装了才启，没装就跳过（别让最后一步报红）
systemctl enable cpolar-dsh.service 2>/dev/null || true
systemctl restart nginx
systemctl restart dsh-hub.service
systemctl restart dsh-panel.service
systemctl restart dsh-admin.service
systemctl restart cpolar-dsh.service 2>/dev/null || true
sleep 4
for svc in dsh-hub dsh-panel dsh-admin nginx; do
  if systemctl is-active --quiet "${svc}.service"; then
    echo "  ✓ ${svc}"
  else
    echo "  ✗ ${svc} → journalctl -u ${svc} -n 30"
  fi
done

echo ""
echo "========================================="
echo " 部署完成"
echo "========================================="
echo ""
echo "  管理员入口:"
echo "    面板(本机)   http://127.0.0.1:8010   （默认 admin/admin123，请立即改密）"
echo "    面板(公网)   https://agent.example.com/"
echo "    机娘 DSH     https://agent.example.com/dsh/?token=<TOKEN>"
echo "    获取 token:  journalctl -u dsh-admin -n 20 | grep -o 'token=[A-Za-z0-9_-]*'"
echo ""
echo "  普通用户: 面板登录后进入 用户视图（任务提交 + 我的文件）"
echo "  Worker 接入: https://agent.example.com/mcp-hub （见 workers/worker.py）"
echo "  人格导入导出: 面板 → 管理员 → 人格（保存后自动改写 DSH 预设并需重启 dsh-admin）"
