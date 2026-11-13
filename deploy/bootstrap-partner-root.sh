#!/usr/bin/env bash
# 伙伴值守分身 —— root 完整版一键引导（需以 root 执行一次）
#
#   sudo bash $HOME/dsh-suite/deploy/bootstrap-partner-root.sh
#
# 做的事：
#   1) 建 /root/.dsh-partner（伙伴人格 profile，与 agent 家目录隔离，规避 §K 铁律）
#   2) 生成 root 版配置 /opt/dsh-partner/config.json（同一 hub 身份「伙伴」，沿用既有 token）
#   3) 安装 systemd 单元 dsh-partner.service（User=root，Restart=always）
#   4) 停用 agent 版（清 @reboot cron + 停进程），避免双实例（单例锁兜底）
#   5) enable --now 启动 root 版
set -euo pipefail

[ "$(id -u)" = "0" ] || { echo "请以 root 运行：sudo bash $0"; exit 1; }

LUSER=agent
LHOME=/home/$LUSER
SUITE="$LHOME/dsh-suite"
WORKER="$SUITE/workers/partner_agent.py"
LCFG="$SUITE/workers/partner.config.json"
DSH_HOME=/root/.dsh-partner
RUN_DIR=/opt/dsh-partner
UNIT=/etc/systemd/system/dsh-partner.service
NODE="$LHOME/.local/nodejs/bin/node"
HARNESS="$LHOME/deepseek-harness"

[ -f "$WORKER" ] || { echo "找不到 $WORKER"; exit 1; }
[ -f "$LCFG" ]   || { echo "找不到 $LCFG（请先在 agent 侧生成）"; exit 1; }

echo "==> 1/5 建 /root/.dsh-partner（复制伙伴 profile）"
rm -rf "$DSH_HOME/profiles/headless"
mkdir -p "$DSH_HOME/profiles"
cp -r "$LHOME/.dsh-partner/profiles/headless" "$DSH_HOME/profiles/headless"
[ -f "$LHOME/.dsh-headless/.anonymous-user-id" ] && \
  cp -f "$LHOME/.dsh-headless/.anonymous-user-id" "$DSH_HOME/.anonymous-user-id" || true

echo "==> 2/5 生成 root 版配置 $RUN_DIR/config.json"
mkdir -p "$RUN_DIR"
python3 - "$LCFG" "$RUN_DIR/config.json" "$DSH_HOME" "$RUN_DIR" "$NODE" "$HARNESS" "$LHOME" <<'PY'
import json, sys
src, dst, dsh_home, run_dir, node, harness, lhome = sys.argv[1:8]
cfg = json.load(open(src, encoding="utf-8"))
cfg["dsh_home"] = dsh_home
cfg["workdir"] = "/root"
cfg["dsh_dir"] = harness
cfg["panel_db"] = f"{lhome}/dsh-suite/data/panel.db"
cfg["node_bin"] = node
json.dump(cfg, open(dst, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
print("   root 版配置已写入", dst)
PY

echo "==> 3/5 安装 systemd 单元 $UNIT"
cat > "$UNIT" <<UNIT
[Unit]
Description=DSH Suite - 伙伴值守分身（root 常驻，Hub Agent）
After=network.target dsh-hub.service
Wants=dsh-hub.service

[Service]
Type=simple
User=root
Environment=DSH_XIAOSHU_CONFIG=$RUN_DIR/config.json
ExecStart=/usr/bin/python3 $WORKER
Restart=always
RestartSec=5
WorkingDirectory=/root

[Install]
WantedBy=multi-user.target
UNIT

echo "==> 4/5 停用 agent 版（清 @reboot cron + 停进程）"
crontab -u "$LUSER" -l 2>/dev/null | grep -v 'partner_agent\.py' | crontab -u "$LUSER" - 2>/dev/null || true
if [ -f /tmp/dsh-partner-agent.pid ]; then
  kill "$(cat /tmp/dsh-partner-agent.pid)" 2>/dev/null || true
  sleep 2
fi

echo "==> 5/5 启动 root 版"
systemctl daemon-reload
systemctl enable --now dsh-partner.service
sleep 4
systemctl --no-pager status dsh-partner.service | head -12 || true
echo "--- 最近日志 ---"
journalctl -u dsh-partner.service -n 8 --no-pager || true
echo
echo "完成。核对："
echo "  · 单例锁 $(cat /tmp/dsh-partner-agent.pid 2>/dev/null || echo '(空)')"
echo "  · systemctl is-active dsh-partner.service"
echo "  · root DSH_HOME=/root/.dsh-partner"
