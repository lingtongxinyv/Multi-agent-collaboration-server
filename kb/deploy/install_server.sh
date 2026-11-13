#!/usr/bin/env bash
# 服务器侧安装脚本（幂等，可重复跑）
# 前置：kb-bundle.tar.gz 已传到服务器，例如 /tmp/kb-bundle.tar.gz
# 用法：sudo bash install_server.sh [/tmp/kb-bundle.tar.gz]
set -euo pipefail

BUNDLE="${1:-/tmp/kb-bundle.tar.gz}"
PREFIX="/opt/kb"
SVC_USER="kb"

if [[ $EUID -ne 0 ]]; then
  echo "需要 root：sudo bash $0 $BUNDLE" >&2
  exit 1
fi

echo "== 1/6 检查运行时 =="
python3 -V
PYV=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
echo "python3 = $PYV"

echo "== 2/6 解包到 $PREFIX =="
mkdir -p "$PREFIX"
tar -xzf "$BUNDLE" -C "$PREFIX"

if [[ -f "$PREFIX/deploy/kb_config.server.json" && ! -f "$PREFIX/kb_config.json" ]]; then
  cp "$PREFIX/deploy/kb_config.server.json" "$PREFIX/kb_config.json"
  echo "已生成 $PREFIX/kb_config.json（server.token 为空，稍后按需填）"
fi

echo "== 3/6 建服务用户与虚拟环境 =="
if ! id -u "$SVC_USER" >/dev/null 2>&1; then
  useradd --system --home "$PREFIX" --shell /usr/sbin/nologin "$SVC_USER"
fi
if [[ ! -x "$PREFIX/.venv/bin/python" ]]; then
  python3 -m venv "$PREFIX/.venv" || {
    echo "venv 创建失败，尝试安装 python3-venv"
    apt-get update -qq && apt-get install -y python3-venv
    python3 -m venv "$PREFIX/.venv"
  }
fi
"$PREFIX/.venv/bin/pip" install --upgrade pip >/dev/null
"$PREFIX/.venv/bin/pip" install -r "$PREFIX/requirements.txt"

echo "== 4/6 落环境变量文件 =="
if [[ ! -f "$PREFIX/kb.env" ]]; then
  install -m 600 -o "$SVC_USER" -g "$SVC_USER" "$PREFIX/deploy/kb.env.example" "$PREFIX/kb.env"
  echo "已生成 $PREFIX/kb.env（请填 DEEPSEEK_API_KEY，权限 0600）"
fi
chown -R "$SVC_USER:$SVC_USER" "$PREFIX"

echo "== 5/6 安装 systemd 服务 =="
install -m 644 "$PREFIX/deploy/kb.service" /etc/systemd/system/kb.service
systemctl daemon-reload
systemctl enable kb.service

echo "== 6/6 启动并自检 =="
systemctl restart kb.service
sleep 3
systemctl --no-pager --lines=20 status kb.service || true
echo
echo "---- 健康检查 ----"
curl -sS http://127.0.0.1:8787/api/stats || echo "（stats 未响应，看上面日志）"
echo
echo "完成。库文件放 $PREFIX/data/kb.sqlite3 后重启服务即可检索。"
