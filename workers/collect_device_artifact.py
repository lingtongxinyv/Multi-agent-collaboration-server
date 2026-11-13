#!/usr/bin/env python3
"""收取设备交回的构建产物并发布到面板下载目录。

desktop-01 上跑完 build_windows_desktop-01.ps1 后，exe 会落在
    ~/dsh-suite/data/device-artifacts/<设备名>/
本脚本把它搬到 workers/dist-connector/，使面板「我的智能体」页自动出现 Windows 下载按钮。

用法：
    ~/dsh-suite/.venv/bin/python workers/collect_device_artifact.py [设备名]
默认设备名 desktop-01。产物名固定为 bellhub-connector.exe（及 .version 旁文件）。
"""
import hashlib
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DATA = REPO / "data"
DIST = REPO / "workers" / "dist-connector"
DEVICE = (sys.argv[1] if len(sys.argv) > 1 else "desktop-01")


def main() -> int:
    src_dir = DATA / "device-artifacts" / DEVICE
    exe = src_dir / "bellhub-connector.exe"
    sidecar = src_dir / "bellhub-connector.exe.version"
    if not exe.is_file():
        print(f"✗ 还没收到产物：{exe}")
        print(f"  目录内容：{sorted(p.name for p in src_dir.glob('*')) if src_dir.is_dir() else '（目录不存在）'}")
        return 2

    blob = exe.read_bytes()
    # 只做最基本的形态校验，避免把半截文件发布出去
    if not blob.startswith(b"MZ"):
        print(f"✗ 不像 Windows 可执行文件（缺 MZ 头，前 4 字节 {blob[:4]!r}）")
        return 3
    if len(blob) < 1_000_000:
        print(f"✗ 文件偏小（{len(blob)} 字节），可能是构建失败的空壳，拒绝发布")
        return 4

    sha = hashlib.sha256(blob).hexdigest()
    ver = sidecar.read_text(encoding="utf-8").strip() if sidecar.is_file() else ""
    if not ver:
        print("⚠ 没收到版本旁文件，下载页会把产物版本标为 null（连接器退回用源码版本比较）")

    DIST.mkdir(parents=True, exist_ok=True)
    shutil.copy2(exe, DIST / "bellhub-connector.exe")
    if ver:
        (DIST / "bellhub-connector.exe.version").write_text(ver + "\n", encoding="utf-8")

    print(f"✅ 已发布 {DIST / 'bellhub-connector.exe'}")
    print(f"   版本  ：{ver or '（未知）'}")
    print(f"   大小  ：{len(blob)} 字节")
    print(f"   sha256：{sha}")
    print("\n下一步：确认属主与权限，然后在线上核对下载页列出 win-x64。")
    print(f"   chown agent:agent {DIST}/*")
    return 0


if __name__ == "__main__":
    sys.exit(main())
