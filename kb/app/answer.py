"""答案生成：DeepSeek 官方 API；失败时降级为纯检索结果。"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

SYSTEM_PROMPT = (
    "你叫忆兮，是用户的个人开发知识库助手。只能依据下面提供的资料回答问题，"
    "不要编造资料里没有的内容。回答用中文，简洁直接，必要时分点。"
    "凡是用到某条资料，就在该句末尾标注对应编号，例如 [1]；"
    "如果资料不足以回答，明确说明缺少什么，不要猜测。"
)


def build_context(results: list[dict[str, Any]]) -> str:
    blocks = []
    for i, r in enumerate(results, 1):
        head = f"[{i}] {r['path']}:{r['start_line']}-{r['end_line']}"
        if r.get("heading"):
            head += f"（{r['heading']}）"
        blocks.append(f"{head}\n{r['text']}")
    return "\n\n".join(blocks)


def call_llm(messages: list[dict], cfg: dict[str, Any]) -> str:
    conf = cfg.get("answer", {})
    key = os.environ.get(conf.get("api_key_env", "DEEPSEEK_API_KEY"), "")
    if not key:
        raise RuntimeError("未找到 DEEPSEEK_API_KEY 环境变量")
    url = conf.get("base_url", "https://api.deepseek.com").rstrip("/") + "/chat/completions"
    payload = {
        "model": conf.get("model", "deepseek-chat"),
        "messages": messages,
        "temperature": 0.2,
        "max_tokens": 1200,
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data["choices"][0]["message"]["content"]


def answer(question: str, results: list[dict[str, Any]], cfg: dict[str, Any]) -> dict[str, Any]:
    """返回 {answer, citations, mode}。mode=llm 或 retrieval（降级）。"""
    if not results:
        return {"answer": "知识库里没有检索到相关内容。", "citations": [], "mode": "retrieval"}
    context = build_context(results)
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"资料：\n{context}\n\n问题：{question}"},
    ]
    try:
        text = call_llm(messages, cfg)
        return {"answer": text, "citations": results, "mode": "llm"}
    except (urllib.error.URLError, urllib.error.HTTPError, KeyError, RuntimeError, TimeoutError) as exc:
        fallback = (
            f"⚠️ 生成模型暂不可用（{exc}）。以下是直接检索到的原文片段，请自行阅读：\n\n"
            + "\n\n".join(
                f"[{i}] {r['path']}:{r['start_line']}\n{r['text'][:600]}"
                for i, r in enumerate(results, 1)
            )
        )
        return {"answer": fallback, "citations": results, "mode": "retrieval"}
