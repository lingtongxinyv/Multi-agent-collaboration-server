"""MCP stdio 服务：把知识库暴露成工具，供 Trae / DSH / Claude 等直接调用。"""
from __future__ import annotations

import json

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from . import config
from .answer import answer as gen_answer
from .embed import Embedder
from .search import Searcher
from .store import Store

# FastMCP 在 host=127.0.0.1 时会自动开启 DNS-rebinding 防护且只放行本机 Host，
# 经 nginx 反代过来的 Host（如 agent.example.com）会被 421 拒掉，所以按配置显式放行。
_SECURITY = TransportSecuritySettings(
    allowed_hosts=list(config.load()["server"]["allowed_hosts"])
)

mcp = FastMCP("yixi", streamable_http_path="/", transport_security=_SECURITY)
_state: dict = {}


def _searcher() -> Searcher:
    if "searcher" not in _state:
        cfg = config.load(reload=True)
        _state["cfg"] = cfg
        _state["store"] = Store(cfg)
        _state["searcher"] = Searcher(_state["store"], Embedder(cfg))
    return _state["searcher"]


def _format(hits: list[dict], limit_text: int = 700) -> str:
    blocks = []
    for i, r in enumerate(hits, 1):
        head = f"[{i}] {r['path']}:{r['start_line']}-{r['end_line']}"
        if r.get("heading"):
            head += f" | {r['heading']}"
        blocks.append(f"{head}\n{r['text'][:limit_text]}")
    return "\n\n".join(blocks)


@mcp.tool()
def kb_search(query: str, top_k: int = 6, source: str = "") -> str:
    """在个人开发知识库里检索原文片段。source 可选 memory/session/repo/notes/inbox。"""
    sources = [source] if source else None
    hits = _searcher().search(query, max(1, min(top_k, 20)), sources)
    if not hits:
        return "没有检索到相关内容。"
    return _format(hits)


@mcp.tool()
def kb_ask(question: str, top_k: int = 6) -> str:
    """基于个人知识库生成带出处的回答。"""
    cfg = _state.get("cfg") or config.load(reload=True)
    hits = _searcher().search(question, max(1, min(top_k, 12)))
    out = gen_answer(question, hits, cfg)
    return out["answer"] + "\n\n---- 出处 ----\n" + _format(out["citations"], 200)


@mcp.tool()
def kb_write(path: str, content: str, title: str = "") -> str:
    """写入或修正一条知识（Markdown）。path 是 data/kb-notes 下的相对路径，
    例如 "px4/qgc-crash.md"；同名文件会被覆盖，用来修正错误或过时的内容。
    返回写入结果与分块数。"""
    from .ingest import write_note
    cfg = config.load(reload=True)
    try:
        res = write_note(cfg, path, content, title)
    except ValueError as exc:
        return f"写入失败：{exc}"
    return json.dumps(res, ensure_ascii=False)


@mcp.tool()
def kb_delete(path: str) -> str:
    """软删除一条知识：标记 missing 并清掉分块，文件保留可追溯。"""
    from .ingest import delete_note
    cfg = config.load(reload=True)
    try:
        res = delete_note(cfg, path)
    except ValueError as exc:
        return f"删除失败：{exc}"
    return json.dumps(res, ensure_ascii=False)


@mcp.tool()
def kb_stats() -> str:
    """查看知识库规模：文档数、分块数、向量可用性。"""
    cfg = config.load(reload=True)
    data = Store(cfg).stats()
    data["embedding"] = {
        "model": cfg["embedding"]["model"],
        "available": Embedder(cfg).available(),
    }
    return json.dumps(data, ensure_ascii=False, indent=2)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
