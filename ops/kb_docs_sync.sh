#!/usr/bin/env bash
# 把仓库 docs/ 同步进知识库 inbox（脱敏 + 触发 ingest）
# 用法：bash ops/kb_docs_sync.sh
set -euo pipefail
SRC=$HOME/dsh-suite/docs
DST=/opt/kb/data/inbox/docs
mkdir -p "$DST"
for f in "$SRC"/*.md; do
  sed 's/15319283146/153****3146/g' "$f" > "$DST/$(basename "$f")"
done
chown -R kb:kb "$DST"
T=$(/opt/kb/.venv/bin/python -c "import json;print(json.load(open('/opt/kb/kb_config.json'))['server']['token'])")
curl -s -X POST -H "X-KB-Token: $T" "http://127.0.0.1:8787/api/ingest?only=inbox"
echo
