"""文本分块：按 Markdown 标题分节，再按行打包成块，保留精确行号。"""
from __future__ import annotations

import re

HEADING_RE = re.compile(r"^(#{1,6})\s+")


def _explode_long_lines(lines: list[tuple[int, str]], max_chars: int) -> list[tuple[int, str]]:
    """超长单行（压缩文本 / 大 JSON）按字符切开，行号保持不变。"""
    out: list[tuple[int, str]] = []
    for lineno, text in lines:
        if len(text) <= max_chars:
            out.append((lineno, text))
            continue
        for start in range(0, len(text), max_chars):
            out.append((lineno, text[start:start + max_chars]))
    return out


def _pack(lines: list[tuple[int, str]], max_chars: int, overlap: int) -> list[list[tuple[int, str]]]:
    """把行贪心打包成不超过 max_chars 的块，块间保留少量重叠行。"""
    blocks: list[list[tuple[int, str]]] = []
    cur: list[tuple[int, str]] = []
    cur_len = 0
    for lineno, text in lines:
        size = len(text) + 1
        if cur_len + size > max_chars and cur:
            blocks.append(cur)
            tail: list[tuple[int, str]] = []
            tail_len = 0
            for item in reversed(cur):
                if tail_len + len(item[1]) > overlap:
                    break
                tail.insert(0, item)
                tail_len += len(item[1]) + 1
            cur = tail
            cur_len = tail_len
        cur.append((lineno, text))
        cur_len += size
    if cur:
        blocks.append(cur)
    return blocks


def _sections(lines: list[tuple[int, str]]) -> list[tuple[str, list[tuple[int, str]]]]:
    """按 Markdown 标题切节，返回 (标题路径, 行列表)。"""
    sections: list[tuple[str, list[tuple[int, str]]]] = []
    stack: list[tuple[int, str]] = []  # (level, title)
    buf: list[tuple[int, str]] = []
    path = ""
    for lineno, text in lines:
        m = HEADING_RE.match(text)
        if m:
            if buf:
                sections.append((path, buf))
                buf = []
            level = len(m.group(1))
            title = text[m.end():].strip()
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, title))
            path = " > ".join(t for _, t in stack)
        buf.append((lineno, text))
    if buf:
        sections.append((path, buf))
    return sections


def split(text: str, max_chars: int = 1000, overlap: int = 120, markdown: bool = True) -> list[dict]:
    """切成 [{seq, start_line, end_line, heading, text}]，行号从 1 开始。"""
    raw_lines = text.splitlines()
    lines = [(i + 1, line) for i, line in enumerate(raw_lines)]
    lines = _explode_long_lines(lines, max_chars)
    sections = _sections(lines) if markdown else [("", lines)]

    chunks: list[dict] = []
    seq = 0
    for heading, sec_lines in sections:
        for block in _pack(sec_lines, max_chars, overlap):
            body = "\n".join(t for _, t in block).strip()
            if not body:
                continue
            chunks.append({
                "seq": seq,
                "start_line": block[0][0],
                "end_line": block[-1][0],
                "heading": heading,
                "text": body,
            })
            seq += 1
    return chunks
