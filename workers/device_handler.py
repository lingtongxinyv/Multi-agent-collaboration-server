#!/usr/bin/env python3
"""默认指令处理器：只回显收到的指令，不执行任何操作。

接入验证用——设备跑通后，把本文件复制一份改写业务逻辑（控制家居、
调本地脚本、读本地文件等），或在 config.json 切换到 shell_handler.py。

入参：sys.argv[1]=指令id  sys.argv[2]=指令文本；stdout 即回传给站娘/页面的结果。
"""

import sys

if __name__ == "__main__":
    cmd_id = sys.argv[1] if len(sys.argv) > 1 else "?"
    text = sys.argv[2] if len(sys.argv) > 2 else ""
    print(f"设备已收到指令 {cmd_id}：{text}\n"
          f"（当前为默认回显 handler，未执行任何操作。"
          f"请编辑 device_handler.py 实现你的业务，或改用 shell_handler.py）")
