#!/usr/bin/env bash
# cpolar 隧道看门狗 —— 治「开机竞态/边缘断连后进程活着但隧道挂死」
# 背景：2026-10-14 21:53 cpolar-dsh 开机启动后隧道静默挂死 22 分钟
#       （master 活着、零日志、公网 000），systemd 无法察觉，须外部探针。
# 2026-10-15 强化（D）：
#       ① 本地栈判别：先探 127.0.0.1:80，本地也挂 ⇒ 判为后端故障，不重启 cpolar；
#       ② 同轮二次确认（~25s）：边缘抖动常秒级自愈，未恢复才动作，检测延迟压到 ~2.5 分钟；
#       ③ 重启后验证循环（至多 ~120s），未恢复则写黑板「重启未恢复」并进入冷却（默认 5 分钟），
#          杜绝 15:08→15:12 式连环重启（既无效又可能触发 cpolar 限流）。
# 2026-10-15 升级（自救）：看门狗重启后仍未恢复 ⇒ 唤醒「助手」值守分身接管自救
#       （经本机 hub 127.0.0.1:8765 /agent/message 投递，不依赖 cpolar 隧道本身）。
#       双闸门：仅在「进入冷却」那一刻投递一次 + 独立节流（默认 ≥30 分钟一条），
#       避免持续故障期反复唤起 LLM 会话（既烧额度又触发助手自身速率熔断）。
# 机制：每 2 分钟由 cron 调用；连续 FAIL_THRESHOLD 次探测失败才 restart（防抖动）；
#       本机网络整体不通时只计数不重启（重启也无用）；重启事件写黑板留痕。
# 安装：crontab 行  */2 * * * * $HOME/dsh-suite/ops/cpolar-watchdog.sh
# 调试：WATCHDOG_DRYRUN=1 跳过 restart；CPOLAR_PROBE_URL 可覆盖探针地址。
set -u

# 2026-10-15 增强：双路探针（HTTP + HTTPS 都要 2xx 才算正常）
# 原因：cpolar 免费版偶发「HTTPS 通但 HTTP 被边缘返回 404 页面」
PROBE_URL_HTTPS="${CPOLAR_PROBE_URL_HTTPS:-https://agent.example.com/}"
PROBE_URL_HTTP="${CPOLAR_PROBE_URL_HTTP:-http://agent.example.com/}"
EXPECT_PREFIX="${CPOLAR_EXPECT_PREFIX:-2}"  # 2xx 都算过（兼容 200/201/204 等）
LOCAL_PROBE_URL="${CPOLAR_LOCAL_PROBE_URL:-http://127.0.0.1:80/}"
LOCAL_EXPECT_CODE="${CPOLAR_LOCAL_EXPECT_CODE:-200}"
NET_CHECK_URL="${CPOLAR_NET_CHECK_URL:-https://www.baidu.com}"
FAIL_THRESHOLD="${CPOLAR_FAIL_THRESHOLD:-2}"
RECHECK_SEC="${CPOLAR_RECHECK_SEC:-25}"
VERIFY_SEC="${CPOLAR_VERIFY_SEC:-120}"
COOLDOWN_SEC="${CPOLAR_COOLDOWN_SEC:-300}"
# 自救层：看门狗未恢复时唤醒助手的通道参数（均可被环境变量覆盖，便于演练）
RESCUE_SEC="${CPOLAR_RESCUE_SEC:-1800}"        # 自救投递最小间隔（秒）
RESCUE_FILE="${CPOLAR_RESCUE_FILE:-/tmp/cpolar-watchdog.rescue}"
RESCUE_UNIT="${CPOLAR_RESCUE_UNIT:-dsh-resident.service}"   # 助手值守分身单元
HUB_URL="${CPOLAR_HUB_URL:-http://127.0.0.1:8765}"
HUB_AUTH_FILE="${CPOLAR_HUB_AUTH_FILE:-$HOME/dsh-suite/workers/partner.config.json}"
RESCUE_AGENT="${CPOLAR_RESCUE_AGENT:-助手}"
BB_DB="${CPOLAR_BB_DB:-$HOME/dsh-suite/data/panel.db}"
STATE_FILE="${CPOLAR_STATE_FILE:-/tmp/cpolar-watchdog.fail}"
COOLDOWN_FILE="${CPOLAR_COOLDOWN_FILE:-/tmp/cpolar-watchdog.cooldown}"
LOCK_FILE="/tmp/cpolar-watchdog.lock"
LOG_FILE="${WATCHDOG_LOG:-$HOME/dsh-suite/data/cpolar-watchdog.log}"
UNIT="${CPOLAR_UNIT:-cpolar-dsh.service}"

log() { echo "[$(date '+%F %T')] $*" >>"$LOG_FILE"; }
probe() { curl -sk -o /dev/null -w '%{http_code}' --max-time "${2:-15}" "$1" 2>/dev/null; }
# 2xx 判定（兼容 cpolar 边缘偶发 201/204 等正常码）
ok_code() { local c="$1"; [ "$c" != "000" ] && [ "${c:0:1}" = "$EXPECT_PREFIX" ]; }
# 双路同时探：返回 "HTTPScode,HTTPcode"
probe_both() {
  local hc hcode
  hc="$(probe "$PROBE_URL_HTTPS" "${2:-15}")"
  hcode="$(probe "$PROBE_URL_HTTP" "${2:-15}")"
  echo "${hc:-000},${hcode:-000}"
}
# 双路都 OK
# ★2026-10-17 修：原写法 `local pair="$1" hc="${pair%%,*}"` 中，bash 先展开整条
#   local 的所有实参、再赋值 ⇒ ${pair} 取到的是**全局旧采样**（主流程第 1 跳的 pair），
#   而非传入的 $1。后果：①同轮复探（pair2）明明 2xx 仍判失败 ⇒ 白重启 cpolar；
#   ②重启后验证循环永远判失败 ⇒ 恒进冷却 + 反复唤醒自救。
#   日志活证：「最近=200,200」却记「未恢复」；全日志 `双路复探恢复` 命中 0。
#   修法：拆成两条 local，先绑定 $1 再派生。
both_ok() {
  local pair="$1"
  local hc="${pair%%,*}" hcode="${pair##*,}"
  ok_code "$hc" && ok_code "$hcode"
}

# 黑板留痕（追加式；失败静默。CPOLAR_BB_DB 可指向不可用路径以在演练中隔离）
bb_event() {
    python3 - "$1" <<'PY' >>/dev/null 2>&1
import os, sqlite3, sys, time
text = sys.argv[1]
try:
    db = sqlite3.connect(os.environ.get('CPOLAR_BB_DB') or "$HOME/dsh-suite/data/panel.db", timeout=10)
    key = f"admin/cpolar-watchdog-events-{time.strftime('%Y%m%d')}"
    db.execute(
        "INSERT INTO blackboard(key,value,updated_at,updated_by) VALUES(?,?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=value||excluded.value, "
        "updated_at=excluded.updated_at, updated_by=excluded.updated_by",
        (key, "\n" + text, time.time(), 'cpolar-watchdog'))
    db.commit(); db.close()
except Exception:
    pass
PY
}

# 自救投递：把事故与已知根因一并交给助手，硬性要求回信只写 [no-reply]（防 ack 环）。
# 由「进入冷却」分支调用；内部再做独立节流，确保一个故障周期内至多唤起一次 LLM。
rescue_handoff() {
    local fails="$1" before="$2" after="$3" waited="$4"
    local now_ts; now_ts=$(date +%s)
    if [ -f "$RESCUE_FILE" ]; then
        local last; last=$(cat "$RESCUE_FILE" 2>/dev/null || echo 0)
        if [ $(( now_ts - last )) -lt "$RESCUE_SEC" ]; then
            log "自救：距上次投递 $(( now_ts - last ))s < ${RESCUE_SEC}s，本次不重复投递"
            return
        fi
    fi
    # 前置自检：自救通道本身不可用时要留独立痕迹，别与「重启失败」混为一谈
    if ! SYSTEMCTL_FORCE_BUS=1 systemctl is-active --quiet "$RESCUE_UNIT"; then
        log "自救：$RESCUE_UNIT 非 active → 自救层不可用，本次不投递"
        bb_event "◎ 自救层不可用：$RESCUE_UNIT 非 active（cpolar ${waited}s 未恢复，${fails} 次失败）"
        return
    fi
    local msg_file; msg_file="$(mktemp)"
    cat >"$msg_file" <<EOF
【事故】cpolar 隧道公网不可达。看门狗已自动重启 $UNIT，并在 ${VERIFY_SEC}s 内复探仍为 ${after}
（连续 ${fails} 次探测失败，最近一次 ${before}）。本机栈正常：127.0.0.1:80=200。
【已知根因】DNS 链 agent.example.com → CNAME 734bc66d.cname.hk.cpolar.io → 35.220.203.140，
CNAME 仍指 hk 边缘；客户端 region 已切 jp，但入站仍走 hk（换区无效已实证）。cpolar master 进程
常活着却零 outbound 连接（静默挂死）；看门狗重启后恢复耗时波动大（最短 8s，最长 >90s）。
【请你自救】诊断 cpolar / nginx / DNS / cpolar 管理面板（http://127.0.0.1:4040 ）现状，可尝试
可逆的服务层操作：sudo -n systemctl restart $UNIT；必要时 systemctl restart nginx；
循环复探 https://agent.example.com/ 。若判断属 cpolar 平台侧问题，给出结论与建议即可。
【硬性约束】不得改动 DNS / CNAME / 证书 / cpolar.yml 配置，不得删除任何数据，
不得执行破坏性操作；仅限可逆的服务层自救。
【回执要求】详细结果请直写黑板 key admin/cpolar-rescue-$(date +%Y%m%d-%H%M)；
**回信内容只写 [no-reply]**（避免与值守分身形成 ack 环）。
【时效】看门狗已进入 ${COOLDOWN_SEC}s 冷却，期间不会与你抢着重启，你可安心操作。
EOF
    local out
    out="$(HUB_URL="$HUB_URL" HUB_AUTH_FILE="$HUB_AUTH_FILE" TO="$RESCUE_AGENT" MSG_FILE="$msg_file" python3 - <<'PY' 2>&1
import json, os, sys, urllib.request
from urllib.parse import quote
try:
    cfg = json.loads(open(os.environ["HUB_AUTH_FILE"], encoding="utf-8").read())
    name, token = cfg["name"], cfg["token"]
except Exception as exc:
    print("AUTH_FAIL", exc); sys.exit(3)
text = open(os.environ["MSG_FILE"], encoding="utf-8").read()
req = urllib.request.Request(os.environ["HUB_URL"].rstrip("/") + "/agent/message",
    data=json.dumps({"to_agent": os.environ["TO"], "text": text}).encode(), method="POST",
    headers={"Content-Type": "application/json"})
req.add_header("X-Agent-Auth", quote(name, safe="") + ":" + token)
try:
    out = json.loads(urllib.request.urlopen(req, timeout=15).read().decode())
except Exception as exc:
    print("SEND_FAIL", exc); sys.exit(4)
print("OK", out.get("message_id") if out.get("ok") else "NOT_OK:" + str(out))
PY
)"
    rm -f "$msg_file"
    if [ "${out%% *}" = "OK" ]; then
        echo "$now_ts" >"$RESCUE_FILE"
        log "自救：已投递求助 → ${RESCUE_AGENT}（${out}）"
        bb_event "★ 看门狗 ${waited}s 未恢复（${fails} 次失败）→ 已唤醒 ${RESCUE_AGENT} 接管自救，看门狗进入冷却"
    else
        log "自救：投递失败（${out:-空}）→ 自救层不可用"
        bb_event "◎ 自救层不可用：投递失败 ${out:-空}（cpolar ${waited}s 未恢复，${fails} 次失败）"
    fi
}

# 演练入口：仅验证「自救投递」路径，不跑主流程。
# 安全闸：若未显式覆盖 CPOLAR_HUB_URL（仍是真实 hub），直接拒绝——避免误唤醒真实助手。
if [ "${1:-}" = "--rescue-selftest" ]; then
    if [ "$HUB_URL" = "http://127.0.0.1:8765" ]; then
        log "SELFTEST：未覆盖 CPOLAR_HUB_URL，拒绝演练（会真实唤醒助手）"
        echo "SELFTEST_REFUSED"; exit 2
    fi
    rescue_handoff "${2:-1}" "${3:-000}" "${4:-000}" "${5:-10}"
    exit 0
fi

# 单实例锁：上一次还没跑完就直接退出
exec 9>"$LOCK_FILE"
flock -n 9 || exit 0

now=$(date +%s)

# 冷却期：上轮重启未能恢复，等 cpolar 边缘/助手自救收敛，期间不连环重启
if [ -f "$COOLDOWN_FILE" ]; then
    until_ts=$(cat "$COOLDOWN_FILE" 2>/dev/null || echo 0)
    if [ "$now" -lt "${until_ts:-0}" ]; then
        pair="$(probe_both)"
        if both_ok "$pair"; then
            rm -f "$COOLDOWN_FILE"; echo 0 >"$STATE_FILE"
            log "冷却期内双路已达 2xx（${pair}），解除冷却"
        else
            log "冷却期内（剩余 $(( until_ts - now ))s），跳过本轮"
        fi
        exit 0
    fi
    rm -f "$COOLDOWN_FILE"
fi

# 开机宽限：systemd 激活期内 is-active 尚未为真，「直接重启」是误判
# （2026-10-15 09:06:01 实测一次：重启后 3 秒 systemd 自己 Started 了两单元）
if [ "$(cut -d. -f1 /proc/uptime)" -lt "${CPOLAR_BOOT_GRACE_SEC:-120}" ]; then
    log "开机宽限期（uptime $(( $(cut -d. -f1 /proc/uptime) ))s < ${CPOLAR_BOOT_GRACE_SEC:-120}s），让位 systemd，跳过本轮"
    exit 0
fi

# 0) 单元都不 active：直接重启（不计阈值——这已经算确凿故障）
if ! SYSTEMCTL_FORCE_BUS=1 systemctl is-active --quiet "$UNIT"; then
    log "单元非 active，直接重启"
    [ "${WATCHDOG_DRYRUN:-0}" = 1 ] || sudo -n /usr/bin/systemctl restart "$UNIT"
    echo 0 >"$STATE_FILE"
    exit 0
fi

# 1) 探针：公网端点（HTTPS + HTTP 双路，经 DNS+边缘 TLS+隧道+nginx 全链路）
pair="$(probe_both)"
if both_ok "$pair"; then
    [ -f "$STATE_FILE" ] && [ "$(cat "$STATE_FILE" 2>/dev/null)" != "0" ] && log "双路探测恢复（${pair}），清零计数"
    echo 0 >"$STATE_FILE"
    exit 0
fi

hc="${pair%%,*}"; hcode="${pair##*,}"
code="$hc,$hcode"   # 失败时保留双路码，便于诊断

fails=$(( $(cat "$STATE_FILE" 2>/dev/null || echo 0) + 1 ))
echo "$fails" >"$STATE_FILE"

# 2) 本机整体断网：「公网探针失败」多半是断网所致，重启 cpolar 无意义
net_code="$(probe "$NET_CHECK_URL" 10)"
if [ "$net_code" = "000" ]; then
    log "探测失败（$code，第 ${fails} 次），但本机外网也不通（$net_code）→ 只计数不重启"
    exit 0
fi

# 3) 本地栈判别：外网通、公网挂，但本机回环亦挂 ⇒ 判为后端故障，不重启 cpolar
local_code="$(probe "$LOCAL_PROBE_URL" 5)"
if [ "$local_code" != "$LOCAL_EXPECT_CODE" ]; then
    log "探测失败（$code，第 ${fails} 次），且本地栈异常（$local_code ≠ $LOCAL_EXPECT_CODE）→ 判为本地栈故障，不重启 cpolar"
    exit 0
fi

# 4) 同轮二次确认：边缘抖动常秒级自愈，~RECHECK_SEC 后复探，仍未恢复才算数
#    （把检测延迟从最多 4 分钟压到 ~2.5 分钟）
sleep "$RECHECK_SEC"
pair2="$(probe_both)"
if both_ok "$pair2"; then
    echo 0 >"$STATE_FILE"
    log "双路复探恢复（${pair} → ${pair2}），判为边缘抖动，清零计数"
    exit 0
fi

if [ "$fails" -lt "$FAIL_THRESHOLD" ]; then
    log "探测失败（${pair} → ${pair2}，第 ${fails}/${FAIL_THRESHOLD} 次），暂未动作"
    exit 0
fi

# 5) 达阈值：重启 + 验证循环（至多 VERIFY_SEC，每 10s 复探）
log "★连续 ${fails} 次双路探测失败（最近=${pair2}），重启 $UNIT"
if [ "${WATCHDOG_DRYRUN:-0}" = 1 ]; then
    log "DRYRUN：跳过实际重启"
    echo 0 >"$STATE_FILE"
    exit 0
fi

sudo -n /usr/bin/systemctl restart "$UNIT" >>"$LOG_FILE" 2>&1
after_pair="000,000"; waited=0
while [ "$waited" -lt "$VERIFY_SEC" ]; do
    sleep 10; waited=$(( waited + 10 ))
    after_pair="$(probe_both)"
    both_ok "$after_pair" && break
done

if both_ok "$after_pair"; then
    log "重启后复探双路均 OK（${after_pair}，等待 ${waited}s）"
    echo 0 >"$STATE_FILE"
    bb_event "[$(date '+%H:%M:%S')] cpolar 看门狗：连续 ${fails} 次探测失败（${pair2}）→ 已 restart $UNIT，${waited}s 内复探=${after_pair}（恢复）。—— 看门狗自动记录"
else
    log "重启后 ${VERIFY_SEC}s 内双路未恢复（最近=${after_pair}）→ 进入 ${COOLDOWN_SEC}s 冷却，并唤醒助手自救"
    echo "$(( now + COOLDOWN_SEC ))" >"$COOLDOWN_FILE"
    echo 0 >"$STATE_FILE"
    bb_event "[$(date '+%H:%M:%S')] cpolar 看门狗：连续 ${fails} 次探测失败（${pair2}）→ 已 restart $UNIT，${waited}s 内复探=${after_pair}（未恢复），进入冷却。—— 看门狗自动记录"
    rescue_handoff "$fails" "$pair2" "$after_pair" "$waited"
fi

# 日志截尾（保留最近 300 行）
[ "$(wc -l <"$LOG_FILE" 2>/dev/null || echo 0)" -gt 300 ] && tail -300 "$LOG_FILE" >"$LOG_FILE.tmp" && mv "$LOG_FILE.tmp" "$LOG_FILE"
exit 0
