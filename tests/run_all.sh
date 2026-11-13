#!/usr/bin/env bash
# 铃枢验收自动跑（智能体链接 + 网盘） —— 一条命令跑完全部套件，失败即非零退出。
#
# 用法：
#   bash tests/run_all.sh              跑全部（源码版）
#   bash tests/run_all.sh --bin        额外用打包好的单文件二进制再跑一遍端到端
#   bash tests/run_all.sh --only test_link_api   只跑一套（排障时用）
#
# 说明：每套都用**隔离临时库**（DSH_SUITE_DATA 指向 mkdtemp）并断言 DB_PATH，
#       绝不会碰真实 panel.db（铁律 #2）。
set -uo pipefail
cd "$(dirname "$0")"
REPO="$(cd .. && pwd)"
PY="${BELLHUB_PYTHON:-$REPO/.venv/bin/python}"
[ -x "$PY" ] || PY="$(command -v python3)"
BIN="$REPO/workers/dist-connector/bellhub-connector"
FRONTEND_DIST="$REPO/panel/frontend/dist"

# 有两条套件要核对「已构建的前端产物」；缺产物时先尝试就地构建，
# 免得新克隆的仓库第一次跑测试就红。
if [ ! -d "$FRONTEND_DIST" ]; then
  if command -v npm >/dev/null 2>&1; then
    printf '▸ 前端产物缺失，先构建 panel/frontend …\n'
    ( cd "$REPO/panel/frontend" \
      && { [ -d node_modules ] || npm ci --no-audit --no-fund; } \
      && npm run build ) >/tmp/bellhub-frontend-build.log 2>&1 \
      || printf '⚠ 前端构建失败（日志 /tmp/bellhub-frontend-build.log），依赖产物的套件会失败\n'
  else
    printf '⚠ 未找到 npm，且 panel/frontend/dist 缺失：test_new_user_journey / test_docs_drift 会失败\n'
  fi
fi

SUITES=(test_link_unit test_link_api test_connector_e2e test_hub_scoping
         test_termux_handler test_device_autostart
         test_drive_unit test_drive_api test_chunked_upload
         test_new_user_journey test_docs_drift test_connector_offline
         test_pairing_races test_lifecycle test_windows_autostart
         test_agent_loop_budget test_agent_turn_budget test_mavlink_relay)
WITH_BIN=0
ONLY=""
while [ $# -gt 0 ]; do
  case "$1" in
    --bin) WITH_BIN=1 ;;
    --only) ONLY="${2:-}"; shift ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "未知参数: $1" >&2; exit 2 ;;
  esac
  shift
done

echo "铃枢 · 验收自检"
echo "  仓库  : $REPO"
echo "  Python: $PY"
echo "  时间  : $(date '+%Y-%m-%d %H:%M:%S')"
echo

pass=0
fail=0
FAILED_NOTES=""

run_one() {                       # run_one <标签> <命令...>
  local label="$1"; shift
  local out rc line mark
  out=$(timeout 300 "$@" 2>&1); rc=$?
  line=$(printf '%s\n' "$out" | grep -E '^结果：' | tail -1)
  if [ "$rc" -eq 0 ]; then
    pass=$((pass + 1)); mark="✅"
  else
    fail=$((fail + 1)); mark="❌"
    FAILED_NOTES+="$(printf '\n[%s] 失败明细：\n%s\n' "$label" \
                    "$(printf '%s\n' "$out" | grep -E '❌|失败项|Traceback' | head -6)")"
  fi
  printf '%s %-34s %s\n' "$mark" "$label" "${line:-（没有结果行；退出码 $rc）}"
}

for s in "${SUITES[@]}"; do
  [ -n "$ONLY" ] && [ "$s" != "$ONLY" ] && continue
  [ -f "$s.py" ] || { printf '⚠ %-34s 文件不存在，跳过\n' "$s"; continue; }
  run_one "$s" "$PY" -u "$s.py"
done

if [ "$WITH_BIN" = 1 ]; then
  if [ -x "$BIN" ]; then
    VER="$(cat "${BIN}.version" 2>/dev/null || echo '?')"
    run_one "connector_e2e(二进制 $VER)" env "CONNECTOR_BIN=$BIN" "$PY" -u test_connector_e2e.py
  else
    printf '⚠ %-34s 未找到打包产物，跳过（先跑 workers/build_connector.sh）\n' "connector_e2e(二进制)"
  fi
fi

echo
if [ "$fail" -eq 0 ]; then
  echo "全部通过 ✅（$pass 套）"
  exit 0
fi
echo "有失败 ❌（通过 $pass 套 / 失败 $fail 套）"
printf '%s\n' "$FAILED_NOTES"
exit 1
