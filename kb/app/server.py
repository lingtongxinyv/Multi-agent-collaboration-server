"""FastAPI 服务：网页问答 + 检索接口 + 远程 MCP（streamable HTTP）。"""
from __future__ import annotations

import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from . import config
from .answer import answer as gen_answer
from .embed import Embedder
from .ingest import run_ingest
from .mcp_server import mcp
from .search import Searcher
from .store import Store

ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = ROOT / "web"

cfg = config.load()
store = Store(cfg)
embedder = Embedder(cfg)
searcher = Searcher(store, embedder)


@asynccontextmanager
async def lifespan(_app):
    """MCP 的 streamable HTTP 会话管理需要随应用生命周期启动。"""
    async with mcp.session_manager.run():
        yield


app = FastAPI(title="忆兮", version="0.1.0", lifespan=lifespan)

_INGEST: dict[str, Any] = {"running": False, "last": None, "started_at": None}


class SearchIn(BaseModel):
    query: str
    top_k: int = 6
    sources: list[str] | None = None
    include_sessions: bool = True


class AskIn(SearchIn):
    pass


def _check_token(request: Request) -> None:
    token = cfg.get("server", {}).get("token", "")
    if token and request.headers.get("x-kb-token") != token:
        raise HTTPException(status_code=401, detail="token 无效")


@app.middleware("http")
async def mcp_auth(request: Request, call_next):
    """远程 MCP 端点用 Bearer token 保护；server.token 为空时不校验。"""
    token = cfg.get("server", {}).get("token", "")
    if token and request.url.path.startswith("/mcp"):
        auth = request.headers.get("authorization", "")
        if auth != f"Bearer {token}" and request.headers.get("x-kb-token") != token:
            return JSONResponse(status_code=401, content={"error": "MCP 认证失败"})
    return await call_next(request)


@app.get("/")
def index():
    return FileResponse(WEB_DIR / "index.html")


@app.get("/api/stats")
def stats():
    data = store.stats()
    data["embedding"] = {
        "model": embedder.model,
        "available": embedder.available(),
    }
    return data


@app.post("/api/search")
async def search(body: SearchIn, request: Request):
    _check_token(request)
    if not body.query.strip():
        return {"results": []}
    hits = searcher.search(body.query, body.top_k, body.sources, body.include_sessions)
    return {"results": hits}


@app.post("/api/ask")
def ask(body: AskIn, request: Request):
    _check_token(request)
    if not body.query.strip():
        raise HTTPException(status_code=400, detail="query 不能为空")
    hits = searcher.search(body.query, body.top_k, body.sources, body.include_sessions)
    out = gen_answer(body.query, hits, config.load())
    return out


@app.post("/api/ingest")
def ingest(request: Request, only: list[str] | None = None):
    _check_token(request)
    if _INGEST["running"]:
        return {"started": False, "reason": "已有入库任务在运行", "state": _INGEST}

    def worker():
        _INGEST.update(running=True, started_at=time.time(), last=None)
        try:
            _INGEST["last"] = run_ingest(config.load(reload=True), only=only, verbose=False)
        finally:
            _INGEST["running"] = False

    threading.Thread(target=worker, daemon=True).start()
    return {"started": True}


@app.get("/api/ingest/status")
def ingest_status():
    return _INGEST


@app.exception_handler(Exception)
async def on_error(request: Request, exc: Exception):
    return JSONResponse(status_code=500, content={"error": str(exc)})


app.mount("/mcp", mcp.streamable_http_app())


def serve():
    import uvicorn

    conf = cfg.get("server", {})
    uvicorn.run(
        "app.server:app",
        host=conf.get("host", "127.0.0.1"),
        port=int(conf.get("port", 8787)),
        reload=False,
    )
