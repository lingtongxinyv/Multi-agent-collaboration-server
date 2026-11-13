#!/bin/bash
# ufw 冗余端口清理 · 一次性（2026-10-17 管理员令）· root 执行
# 删除无本地监听的放行：20,21,22,888,8888,36868,39000:40000；保留 80,443。
# 自毁：执行完即从 root crontab 删除本行，不残留。
set -u
LOG=$HOME/dsh-suite/data/backups/ufw-cleanup-$(date +%Y%m%d-%H%M%S).log
{
echo "=== ufw cleanup @ $(date '+%F %T') ==="
echo "--- before ---"
ufw status numbered

# 防御：端口仍有监听则跳过
DEL=""
for p in 20 21 22 888 8888 36868; do
  if ss -tln | awk '{print $4}' | grep -qE "[:.]$p$"; then
    echo "SKIP $p (有监听)"; else DEL="$DEL $p"; fi
done
# 39000:40000 区间整体跳过判定：区间内任一监听则不动
if ss -tln | awk '{print $4}' | grep -qE '[:.](39[0-9]{3}|40000)$'; then
  echo "SKIP 39000:40000 (区间内有监听)"
else DEL="$DEL 39000:40000"; fi

for r in $DEL; do
  ufw delete allow "$r" 2>/dev/null || ufw delete allow "$r/tcp" 2>/dev/null || echo "del-fail $r"
done

echo "--- after ---"
ufw status numbered
} | tee "$LOG" >/dev/null 2>&1

# 结果落黑板
python3 - "$LOG" <<'PYEOF'
import sqlite3, sys, time
log = open(sys.argv[1]).read()
conn = sqlite3.connect("$HOME/dsh-suite/data/panel.db", timeout=10)
conn.execute(
  "INSERT INTO blackboard (key, value, updated_at, updated_by) VALUES ('ops/ufw-cleanup-20261017', ?, ?, '伙伴@root-once') "
  "ON CONFLICT(key) DO UPDATE SET value = value || excluded.value, updated_at = excluded.updated_at",
  ("\n=== " + time.strftime('%F %T') + " ===\n" + log[:3000] + "\n", time.time()))
conn.commit(); conn.close()
PYEOF

# 从 root crontab 自毁
crontab -l | grep -v ufw-cleanup | crontab -
