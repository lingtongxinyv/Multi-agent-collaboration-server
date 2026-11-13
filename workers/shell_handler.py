#!/usr/bin/env python3
"""可选 shell 指令处理器：把指令文本作为 shell 命令在设备本机执行。

⚠ 风险提示：启用后，任何拿到设备 token 的人（包括站娘对话）都能在你设备上
执行任意命令。仅在自有信任设备上使用；token 泄露请立即在「我的设备」删除设备。

启用方式：config.json 里 "handler": "shell_handler.py"
超时 600 秒；stdout+stderr 均回传（上限由服务端截断）。
"""

import subprocess
import sys

TIMEOUT = 600

if __name__ == "__main__":
    text = sys.argv[2] if len(sys.argv) > 2 else ""
    if not text.strip():
        print("（空指令）")
        sys.exit(1)
    proc = subprocess.run(text, shell=True, capture_output=True,
                          text=True, timeout=TIMEOUT)
    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    result = "\n".join(x for x in (out, err) if x) or f"(无输出, 退出码 {proc.returncode})"
    print(result)
    sys.exit(0 if proc.returncode == 0 else 1)
