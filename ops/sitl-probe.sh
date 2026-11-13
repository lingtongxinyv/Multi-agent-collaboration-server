#!/usr/bin/env bash
# SITL 30s 心跳探针（P3 2026-10-20）—— 沿用 cpolar-watchdog 的双探针/冷却/留痕风格。
#
# 治的问题：容器「活着」不等于链路「通的」。PX4 进程崩、桥被并发断连带走、
# 端口没监听，systemd 都看不出来；必须自己读一条 MAVLink HEARTBEAT 才算数。
#
# 每 30s 由 dsh-sitl-probe.timer 触发，检查三件事：
#   ① 容器 px4-t4 在跑（否则 systemctl start dsh-sitl.service）
#   ② 127.0.0.1:5760 能读到 HEARTBEAT（否则 restart dsh-sitl.service）
#   ③ 中继单元 dsh-mavlink-relay.service active（否则 restart 它）
# 失败一律「隔 3s 复探一次」再动作；动作后进 300s 冷却，防止连环重启。
# 只在动作/恢复时写黑板（admin/sitl-probe-events-YYYYMMDD），不唤醒 LLM 会话。
#
# 手动：bash ops/sitl-probe.sh --once     只探不动作（排障）
#       bash ops/sitl-probe.sh --status   打印三路状态
set -u

CONTAINER="${SITL_CONTAINER:-px4-t4}"
MAVLINK_HOST="${SITL_MAVLINK_HOST:-127.0.0.1}"
MAVLINK_PORT="${SITL_MAVLINK_PORT:-5760}"
RELAY_UNIT="${MAVLINK_RELAY_UNIT:-dsh-mavlink-relay.service}"
CONTAINER_UNIT="${SITL_UNIT:-dsh-sitl.service}"
RECHECK_SEC="${SITL_RECHECK_SEC:-3}"
COOLDOWN_SEC="${SITL_COOLDOWN_SEC:-300}"
BOOT_GRACE_SEC="${SITL_BOOT_GRACE_SEC:-150}"
STATE_FILE="${SITL_STATE_FILE:-$HOME/dsh-suite/data/sitl-probe.state}"
COOLDOWN_FILE="${SITL_COOLDOWN_FILE:-$HOME/dsh-suite/data/sitl-probe.cooldown}"
LOG_FILE="${SITL_LOG:-$HOME/dsh-suite/data/sitl-probe.log}"
BB_DB="${SITL_BB_DB:-$HOME/dsh-suite/data/panel.db}"
DRYRUN="${SITL_DRYRUN:-0}"
LOCK_FILE="/tmp/dsh-sitl-probe.lock"

log() { echo "[$(date '+%F %T')] $*" >>"$LOG_FILE"; }

bb_event() {
    [ "$DRYRUN" = 1 ] && return 0
    python3 - "$1" <<'PY' >>/dev/null 2>&1
import os, sqlite3, sys, time
text = sys.argv[1]
try:
    db = sqlite3.connect(os.environ.get('SITL_BB_DB') or "$HOME/dsh-suite/data/panel.db", timeout=10)
    key = f"admin/sitl-probe-events-{time.strftime('%Y%m%d')}"
    db.execute(
        "INSERT INTO blackboard(key,value,updated_at,updated_by) VALUES(?,?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=value||excluded.value, "
        "updated_at=excluded.updated_at, updated_by=excluded.updated_by",
        (key, "\n" + text, time.time(), 'sitl-probe'))
    db.commit(); db.close()
except Exception:
    pass
PY
}

# 读 5760 的 MAVLink 流，至少 1 条 HEARTBEAT（msgid 0）才算通。
# 输出一行 JSON：{"ok":true,"frames":N,"heartbeat":N,"msgids":[...]}
mavlink_probe() {
    python3 - "$MAVLINK_HOST" "$MAVLINK_PORT" <<'PY'
import json, socket, sys, time
host, port = sys.argv[1], int(sys.argv[2])
deadline = time.time() + 4.0
frames = 0
heartbeat = 0
msgids = set()
buf = bytearray()
try:
    sock = socket.create_connection((host, port), timeout=3)
    sock.settimeout(0.5)
except OSError as exc:
    print(json.dumps({"ok": False, "error": f"connect: {exc!r}"}))
    raise SystemExit(0)
try:
    while time.time() < deadline:
        try:
            chunk = sock.recv(65535)
        except socket.timeout:
            continue
        except OSError as exc:
            print(json.dumps({"ok": False, "error": f"recv: {exc!r}"}))
            raise SystemExit(0)
        if not chunk:
            break
        buf += chunk
        i = 0
        while True:
            while i < len(buf) and buf[i] not in (0xFD, 0xFE):
                i += 1
            if i >= len(buf):
                break
            if buf[i] == 0xFD:              # MAVLink v2
                if len(buf) - i < 12:
                    break
                length = buf[i + 1]
                msgid = buf[i + 7] | (buf[i + 8] << 8) | (buf[i + 9] << 16)
                total = 12 + length + 2
            else:                            # MAVLink v1
                if len(buf) - i < 8:
                    break
                length = buf[i + 1]
                msgid = buf[i + 5]
                total = 8 + length + 2
            if len(buf) - i < total:
                break
            frames += 1
            msgids.add(msgid)
            if msgid == 0:
                heartbeat += 1
            i += total
        del buf[:i]
finally:
    sock.close()
print(json.dumps({
    "ok": frames >= 5 and heartbeat >= 1,
    "frames": frames,
    "heartbeat": heartbeat,
    "msgids": sorted(msgids)[:40],
}))
PY
}

container_running() {
    [ "$(docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null)" = "true" ]
}

relay_active() {
    systemctl is-active --quiet "$RELAY_UNIT"
}

relay_listening() {
    # 用 ss 判断监听，不真连中继：每 30s 建一条空连接会在中继日志里留 IncompleteReadError 噪声
    ss -ltnH 'sport = :8766' 2>/dev/null | grep -q ':8766'
}

container_age() {
    local started
    started="$(docker inspect -f '{{.State.StartedAt}}' "$CONTAINER" 2>/dev/null)" || return 1
    date -d "$started" +%s 2>/dev/null
}

restart_container() {
    [ "$DRYRUN" = 1 ] && { log "DRYRUN：跳过 restart $CONTAINER_UNIT"; return 0; }
    log "重启 $CONTAINER_UNIT（容器 $CONTAINER）"
    systemctl restart "$CONTAINER_UNIT" >>"$LOG_FILE" 2>&1
}

restart_relay() {
    [ "$DRYRUN" = 1 ] && { log "DRYRUN：跳过 restart $RELAY_UNIT"; return 0; }
    log "重启 $RELAY_UNIT"
    systemctl restart "$RELAY_UNIT" >>"$LOG_FILE" 2>&1
}

status_line() {
    local probe
    probe="$(mavlink_probe)"
    local relay; relay_active && relay="active" || relay="inactive"
    local listen; relay_listening && listen="yes" || listen="no"
    echo "container=$(container_running && echo running || echo stopped) relay_unit=$relay relay_8766=$listen mavlink=$probe"
}

case "${1:-}" in
    --status) status_line; exit 0 ;;
    --once)
        DRYRUN=1
        log "ONCE：$(status_line)"
        probe="$(mavlink_probe)"
        case "$probe" in *'"ok": true'*) exit 0 ;; *) exit 1 ;; esac
        ;;
    -h|--help) sed -n '2,18p' "$0"; exit 0 ;;
esac

exec 9>"$LOCK_FILE"
flock -n 9 || exit 0

# 开机宽限：给容器/PX4 一次启动窗口（PX4 起流约 10~15s，桥再等 10s）
uptime_s="$(cut -d. -f1 /proc/uptime)"
if [ "$uptime_s" -lt "$BOOT_GRACE_SEC" ]; then
    log "开机宽限：uptime ${uptime_s}s < ${BOOT_GRACE_SEC}s，本轮跳过"
    exit 0
fi
if container_running; then
    started="$(container_age || echo 0)"
    if [ "${started:-0}" -gt 0 ] && [ $(( $(date +%s) - started )) -lt 90 ]; then
        log "容器启动不足 90s，本轮跳过"
        exit 0
    fi
fi

now=$(date +%s)
if [ -f "$COOLDOWN_FILE" ]; then
    until_ts="$(cat "$COOLDOWN_FILE" 2>/dev/null || echo 0)"
    if [ "$now" -lt "${until_ts:-0}" ]; then
        log "冷却期内（剩余 $(( until_ts - now ))s），跳过本轮"
        exit 0
    fi
    rm -f "$COOLDOWN_FILE"
fi

fail() {
    local what="$1"
    echo $(( $(cat "$STATE_FILE" 2>/dev/null || echo 0) + 1 )) >"$STATE_FILE"
    log "$what"
    bb_event "[$(date '+%H:%M:%S')] SITL 探针：$what"
}

# ① 容器
if ! container_running; then
    sleep "$RECHECK_SEC"
    if container_running; then
        log "容器复探已恢复，判为抖动"
    else
        fail "容器 $CONTAINER 未运行（复探仍失败）→ restart $CONTAINER_UNIT"
        restart_container
        echo "$(( now + COOLDOWN_SEC ))" >"$COOLDOWN_FILE"
        exit 0
    fi
fi

# ② MAVLink 心跳（双探）
probe="$(mavlink_probe)"
case "$probe" in
    *'"ok": true'*) ;;
    *)
        sleep "$RECHECK_SEC"
        probe2="$(mavlink_probe)"
        case "$probe2" in
            *'"ok": true'*)
                log "MAVLink 复探恢复（$probe → $probe2），判为抖动"
                ;;
            *)
                fail "127.0.0.1:$MAVLINK_PORT 无心跳（$probe → $probe2）→ restart $CONTAINER_UNIT"
                restart_container
                echo "$(( now + COOLDOWN_SEC ))" >"$COOLDOWN_FILE"
                exit 0
                ;;
        esac
        ;;
esac

# ③ 中继单元
if ! relay_active; then
    fail "中继单元 $RELAY_UNIT 非 active → restart"
    restart_relay
    echo "$(( now + COOLDOWN_SEC ))" >"$COOLDOWN_FILE"
    exit 0
fi
if ! relay_listening; then
    sleep "$RECHECK_SEC"
    if relay_listening; then
        log "8766 复探已恢复"
    else
        fail "中继 127.0.0.1:8766 未监听 → restart $RELAY_UNIT"
        restart_relay
        echo "$(( now + COOLDOWN_SEC ))" >"$COOLDOWN_FILE"
        exit 0
    fi
fi

# 全绿：清零计数（只在从故障恢复时记一行）
fails="$(cat "$STATE_FILE" 2>/dev/null || echo 0)"
if [ "${fails:-0}" != "0" ]; then
    log "全部恢复（$probe），计数清零"
    bb_event "[$(date '+%H:%M:%S')] SITL 探针：链路恢复（$probe），计数清零"
fi
echo 0 >"$STATE_FILE"

# 日志截尾
if [ "$(wc -l <"$LOG_FILE" 2>/dev/null || echo 0)" -gt 300 ]; then
    tail -300 "$LOG_FILE" >"$LOG_FILE.tmp" && mv "$LOG_FILE.tmp" "$LOG_FILE"
fi
exit 0
