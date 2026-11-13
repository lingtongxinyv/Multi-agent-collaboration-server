#!/usr/bin/env bash
# 伙伴值守 worker 免提权部署器 —— A′ 方案（2026-10-15，助手@站娘本机通道）
#
# 背景（open-items 第 8 条）：dsh-partner 以 User=root 运行，而脚本原先放在
#   agent 可写的 $HOME/dsh-suite/workers/ 下 ⇒ 任何能写该目录的人，
#   等 dsh-partner 下次重启（Restart=always）或开机即可拿到 root。
# A′ = 把脚本副本移入 root 属主目录 /opt/dsh-partner/workers/，ExecStart 指向副本，
#   从而砍掉「root 执行用户可写代码」这条提权路径，同时保留 root 能力。
#
# ★纪律（重要）：部署后 /opt 下的副本是**唯一生效件**，仓库里的
#   workers/partner_agent.py 只是源件 —— 改完源件必须跑本脚本才生效，
#   否则会出现「改了仓库却静默不生效」的分歧。
#
# 用法：
#   sudo bash ops/deploy-partner-worker.sh              # 部署 + 重启
#   sudo bash ops/deploy-partner-worker.sh --check      # 只校验，不动任何文件
#   sudo bash ops/deploy-partner-worker.sh --no-restart # 只部署，不重启
set -euo pipefail

REPO="${DSH_SUITE_REPO:-$HOME/dsh-suite}"
SRC="$REPO/workers/partner_agent.py"
DEST_DIR="/opt/dsh-partner/workers"
DEST="$DEST_DIR/partner_agent.py"
UNIT="/etc/systemd/system/dsh-partner.service"
EXPECT_EXEC="/usr/bin/python3 $DEST"
UNIT_OWNER="root:root"

CHECK=0
RESTART=1
for a in "$@"; do
    case "$a" in
        --check) CHECK=1 ;;
        --no-restart) RESTART=0 ;;
        *) echo "未知参数: $a" >&2; exit 2 ;;
    esac
done

say() { printf '%s\n' "$*"; }
die() { printf '❌ %s\n' "$*" >&2; exit 1; }

[ -f "$SRC" ] || die "源件不存在: $SRC"

say "=== 伙伴 worker A′ 部署器 ==="
say "  源件 : $SRC"
say "  生效件: $DEST"
say "  单元 : $UNIT"
say ""

# ── 1) 校验当前状态 ──
src_hash="$(sha256sum "$SRC" | awk '{print $1}')"
say "[1] 哈希校验"
say "  源件 sha256 = $src_hash"
if [ -f "$DEST" ]; then
    dest_hash="$(sha256sum "$DEST" | awk '{print $1}')"
    say "  生效件 sha256 = $dest_hash"
    [ "$src_hash" = "$dest_hash" ] && say "  ✅ 已同步" || say "  ⚠️ 源件与生效件不一致 —— 需要部署"
else
    say "  生效件尚不存在 —— 首次部署"
fi

cur_exec="$(grep -E '^ExecStart=' "$UNIT" 2>/dev/null || true)"
say ""
say "[2] 单元 ExecStart"
say "  当前: ${cur_exec:-<未读到>}"
if [ "$cur_exec" = "ExecStart=$EXPECT_EXEC" ]; then
    say "  ✅ 已指向 root 属主副本"
else
    say "  ⚠️ 尚未指向副本（仍指向用户可写路径）—— 需要改写"
fi

if [ "$CHECK" = 1 ]; then
    say ""
    say "[CHECK] 未改动任何文件。"
    exit 0
fi

# ── 3) 安装 root 属主副本 ──
say ""
say "[3] 安装 root 属主副本"
install -d -o root -g root -m 0755 "$DEST_DIR"
install -o root -g root -m 0644 "$SRC" "$DEST"
got="$(sha256sum "$DEST" | awk '{print $1}')"
[ "$got" = "$src_hash" ] || die "副本哈希不符: $got"
say "  ✅ $DEST （root:root 0644，sha256 复校一致）"

# ── 4) 改写 ExecStart（幂等）──
say ""
say "[4] 改写单元 ExecStart"
if [ "$cur_exec" = "ExecStart=$EXPECT_EXEC" ]; then
    say "  ⏭  已是目标形态，跳过"
else
    cp -a "$UNIT" "$UNIT.bak-$(date +%Y%m%d-%H%M%S)"
    sed -i "s#^ExecStart=.*#ExecStart=$EXPECT_EXEC#" "$UNIT"
    grep -q "^ExecStart=$EXPECT_EXEC$" "$UNIT" || die "ExecStart 改写失败"
    say "  ✅ 已改为 ExecStart=$EXPECT_EXEC（原单元已备份）"
fi
chown "$UNIT_OWNER" "$UNIT"

# ── 5) reload + 重启 ──
systemctl daemon-reload
say ""
if [ "$RESTART" = 1 ]; then
    say "[5] 重启 dsh-partner.service"
    systemctl restart dsh-partner.service
    sleep 4
    st="$(SYSTEMCTL_FORCE_BUS=1 systemctl is-active dsh-partner.service || true)"
    say "  状态: $st"
    [ "$st" = "active" ] || die "重启后非 active，请查 journalctl -u dsh-partner"
    say "  ✅ 已 active"
else
    say "[5] --no-restart：已 daemon-reload，未重启（下次重启/开机生效）"
fi

say ""
say "✅ 完成。复核建议："
say "  SYSTEMCTL_FORCE_BUS=1 systemctl show -p MainPID -p ExecStart dsh-partner.service"
say "  ps -o pid,user,cmd -p \$(SYSTEMCTL_FORCE_BUS=1 systemctl show -p MainPID --value dsh-partner.service)"
say "  sudo -u agent test -w $DEST && echo '仍可写(异常)' || echo '用户不可写 ✅'"
