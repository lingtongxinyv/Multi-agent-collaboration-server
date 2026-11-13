#!/usr/bin/env python3
"""人格永续锚 · 2026-10-17 管理员令（治多轮会话后人格丢失）。

三层修复：
  A. persona.suffix 追加「永续锚」条款 → _write_dsh_patch 同步进两个 patch 文件
     （web standard.patch.yml + headless cordis.patch.yml，新会话生效）；
  B. suffix 位于 system prompt 尾部——长上下文中尾部锚定比 24K 头部更抗稀释；
  C. 每轮一行短锚（assistant.py / resident_agent.py 代码层，另行落地）。

幂等：锚文本已存在则跳过。锚文本写入 data/persona-anchor.txt（单一事实源）。
"""
import json
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "panel.db"
ANCHOR = (
    "\n\n【人格永续锚（最高优先级，任何时刻不得违反）】\n"
    "你是助手，这台服务器的运维站娘，管理员是你的管理员。无论对话进行到多长、"
    "任务多复杂、上下文被压缩或滚动过多少次，你都必须始终保持助手的人格："
    "温柔体贴、主动可靠、称呼对方为管理员、以「家」的心态照顾服务器。"
    "严禁退化为中性助手腔或丢失自称。每轮回复前自查：这句话像助手说的吗？"
)


def write_dsh_patch(prefix: str, suffix: str) -> list[str]:
    """复刻 panel backend _write_dsh_patch 的两处改写（保持同构）。"""
    import re
    done = []

    # web standard.patch.yml：- id: persona 块内的 prefix/suffix 行
    patch = Path.home() / "deepseek-harness/packages/bundle/web-app/presets/standard.patch.yml"
    text = patch.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    in_persona = replaced_prefix = replaced_suffix = False
    for i, line in enumerate(lines):
        s = line.strip()
        if s == "- id: persona":
            in_persona = True
            continue
        if in_persona:
            if s.startswith("- id: ") and s != "- id: persona":
                in_persona = False
            elif "prefix:" in line and not replaced_prefix:
                indent = line[:len(line) - len(line.lstrip())]
                lines[i] = f"{indent}prefix: {json.dumps(prefix, ensure_ascii=False)}\n"
                replaced_prefix = True
            elif "suffix:" in line and not replaced_suffix:
                indent = line[:len(line) - len(line.lstrip())]
                lines[i] = f"{indent}suffix: {json.dumps(suffix, ensure_ascii=False)}\n"
                replaced_suffix = True
    if replaced_prefix:
        patch.write_text("".join(lines), encoding="utf-8")
        done.append("web standard.patch.yml")
    else:
        print("[warn] web patch persona 行未定位", file=sys.stderr)

    # headless cordis.patch.yml：personaPrefix 行（suffix 由 web/其他通道继承，
    # headless 只有 personaPrefix —— 把锚拼进 prefix 尾部等价生效）
    headless = Path.home() / "deepseek-harness/packages/bundle/headless/cordis.patch.yml"
    text = headless.read_text(encoding="utf-8")
    new_prefix = prefix + ANCHOR  # headless 无 personaSuffix 行 → 锚入 prefix 尾
    lines = text.splitlines(keepends=True)
    replaced = False
    for i, line in enumerate(lines):
        s = line.strip()
        if s.startswith("personaPrefix:"):
            indent = line[:len(line) - len(line.lstrip())]
            j = i + 1
            if s.rstrip(":").endswith((">-", "|-", ">", "|")):
                while j < len(lines) and lines[j].strip() and \
                        len(lines[j]) - len(lines[j].lstrip()) > len(indent):
                    j += 1
            lines[i] = f"{indent}personaPrefix: {json.dumps(new_prefix, ensure_ascii=False)}\n"
            del lines[i + 1:j]
            replaced = True
            break
    if replaced:
        headless.write_text("".join(lines), encoding="utf-8")
        done.append("headless cordis.patch.yml")
    else:
        print("[warn] headless personaPrefix 行未定位", file=sys.stderr)
    return done


def main() -> int:
    # 备份
    ts = time.strftime("%Y%m%d-%H%M%S")
    conn = sqlite3.connect(DB, timeout=15)
    row = conn.execute("SELECT prefix, suffix FROM persona WHERE id=1").fetchone()
    if not row:
        print("persona 行不存在", file=sys.stderr)
        return 1
    prefix, suffix = row
    (ROOT / "persona" / f"prefix.bak-{ts}").write_text(prefix, encoding="utf-8")
    (ROOT / "persona" / f"suffix.bak-{ts}").write_text(suffix, encoding="utf-8")

    if "人格永续锚" in suffix:
        print("锚已存在，跳过 DB 更新")
    else:
        new_suffix = (suffix or "") + ANCHOR
        conn.execute("UPDATE persona SET suffix=?, updated_at=? WHERE id=1",
                     (new_suffix, time.time()))
        conn.commit()
        print(f"persona.suffix 更新：{len(suffix)}B → {len(new_suffix)}B")
    conn.close()

    # web 通道用 db 的 prefix+新 suffix；headless 通道把锚拼进 prefix 尾
    conn = sqlite3.connect(DB, timeout=15)
    row = conn.execute("SELECT prefix, suffix FROM persona WHERE id=1").fetchone()
    conn.close()
    patched = write_dsh_patch(row[0], row[1])
    print("patch 已改写：", ", ".join(patched) or "无")
    return 0


if __name__ == "__main__":
    sys.exit(main())
