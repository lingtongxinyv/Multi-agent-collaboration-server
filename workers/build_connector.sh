#!/usr/bin/env bash
# 把铃枢连接器打包成单文件可执行程序（免 Python、双击即用）。
#
# ⚠ 必须在**目标系统**上构建：Windows 用 build_connector.ps1，Linux 用本脚本。
#   PyInstaller 不做跨平台交叉编译；要出 Windows .exe 就在 Windows 机器上跑。
#
# 用法：bash workers/build_connector.sh
set -euo pipefail
cd "$(dirname "$0")"

if ! python3 -m PyInstaller --version >/dev/null 2>&1; then
  echo "缺少 pyinstaller，请先安装：python3 -m pip install pyinstaller" >&2
  exit 1
fi

rm -rf build dist-connector bellhub-connector.spec
python3 -m PyInstaller --onefile --name bellhub-connector \
  --distpath dist-connector \
  --console bellhub_connector.py

echo
echo "✅ 产物：$(pwd)/dist-connector/bellhub-connector"
# 版本旁文件：面板读不到二进制内部的 VERSION，靠它告诉连接器"这份产物是哪一版"
VER=$(grep -m1 '^VERSION' bellhub_connector.py | sed -E 's/.*"([^"]+)".*/\1/')
printf '%s\n' "$VER" > dist-connector/bellhub-connector.version
echo "版本旁文件：dist-connector/bellhub-connector.version = $VER"
sha256sum dist-connector/bellhub-connector 2>/dev/null || true
echo "分发：把这个文件发给用户，双击运行 pair 子命令即可（也可直接双击后按提示操作）。"
