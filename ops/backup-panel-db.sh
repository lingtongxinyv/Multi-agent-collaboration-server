#!/bin/sh
# dsh-suite panel.db 每日备份（65-C 数据面）
# 由 dsh-panel-backup.timer 以 agent 身份触发；保留最近 14 份。
set -eu
SUITE=${DSH_SUITE:-$HOME/dsh-suite}
SRC="$SUITE/data/panel.db"
DEST_ROOT="$SUITE/data/backups"
STAMP=$(date +%Y%m%d-%H%M)
DEST="$DEST_ROOT/$STAMP"
LOG="$DEST_ROOT/backup.log"
mkdir -p "$DEST"

python3 - "$SRC" "$DEST/panel.db" <<'PY'
import sqlite3, sys
src, dst = sys.argv[1], sys.argv[2]
con = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
out = sqlite3.connect(dst)
con.backup(out)
out.close(); con.close()
PY

sha256sum "$DEST/panel.db" > "$DEST/panel.db.sha256"
SIZE=$(stat -c %s "$DEST/panel.db")
echo "$(date -Is) ok stamp=$STAMP size=$SIZE sha256=$(cut -d' ' -f1 "$DEST/panel.db.sha256")" >> "$LOG"

# 保留最近 14 份
ls -1d "$DEST_ROOT"/[0-9]*-[0-9]* 2>/dev/null | sort | head -n -14 | while read -r old; do
  rm -rf -- "$old"
done
