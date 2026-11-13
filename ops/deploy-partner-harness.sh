#!/usr/bin/env bash
# 把 agent 可写的 harness 仓，同步成 root 只读副本，供 dsh-partner（root）执行。
#
# 为什么要有这个脚本（J3 / open-items §8 A′ 姊妹项）：
#   partner 以 root 跑 headless，但 dsh_dir 过去指向 $HOME/deepseek-harness
#   —— agent 可写。改一行代码等下次重启就能以 root 执行 ⇒ 提权路径。
#   副本属主 root、去掉 group/other 写位后，agent 只能读、不能改。
#
# 用法：
#   sudo ops/deploy-partner-harness.sh            # 同步 + 记录版本
#   sudo ops/deploy-partner-harness.sh --check    # 只比对，不写
#
# 注意：
#   · 默认排除 .git（40M，运行时不需要）；node_modules 必须带（tsx 解析依赖它）。
#   · --delete 会删掉副本里源仓已不存在的文件——这是刻意的，副本必须与源一致。
#   · 改完 harness 代码后必须重跑本脚本，否则 partner 跑的是旧副本（65-A 精神）。
set -euo pipefail

SRC=${1:-$HOME/deepseek-harness}
DST=${2:-/opt/dsh-partner/harness}
CHECK=0
[[ "${1:-}" == "--check" ]] && { CHECK=1; SRC=$HOME/deepseek-harness; DST=/opt/dsh-partner/harness; }

[[ -d "$SRC" ]] || { echo "源不存在: $SRC" >&2; exit 2; }
[[ "$(id -u)" == "0" ]] || { echo "需要 root（副本要 root:root）" >&2; exit 3; }

# root 读 agent 的仓库会触发 git「dubious ownership」；用 safe.directory 显式放行，
# 且管道整体兜底（set -o pipefail 下 git 的 128 会把脚本带崩——首跑即踩）。
git_() { git -c safe.directory="$SRC" -C "$SRC" "$@"; }
rev=$(git_ rev-parse --short HEAD 2>/dev/null || echo unknown)
dirty=$(git_ status --porcelain 2>/dev/null | wc -l | tr -d ' ') || dirty=0

if [[ "$CHECK" == "1" ]]; then
  echo "src=$SRC ($rev, dirty=$dirty)  dst=$DST"
  rsync -an --delete --exclude .git "$SRC/" "$DST/" | head -20
  echo "（上面是差异预览；空 = 一致）"
  exit 0
fi

mkdir -p "$DST"
rsync -a --delete --exclude .git "$SRC/" "$DST/"
chown -R root:root "$DST"
chmod -R go-w "$DST"
printf 'source=%s\ncommit=%s\ndirty=%s\ndeployed_at=%s\n' \
  "$SRC" "$rev" "$dirty" "$(date -Iseconds)" > "$DST/.deploy-info"
chmod 644 "$DST/.deploy-info"

echo "已部署 $rev -> $DST"
echo "校验："
du -sh "$DST" 2>/dev/null
stat -c 'owner=%U:%G mode=%a' "$DST" "$DST/apps/cli/src/bin.ts"
