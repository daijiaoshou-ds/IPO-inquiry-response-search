# -*- coding: utf-8 -*-
"""本地 Web 服务：FastAPI + 单页前端（无构建步骤）。

启动：
    .venv\\Scripts\\python.exe -m src.server
然后打开 http://127.0.0.1:8765
"""
from __future__ import annotations

import argparse
import base64
import os
import secrets
import sqlite3
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

from .search import search_grouped, siblings, stats, top_grams

ROOT = Path(__file__).resolve().parent.parent
WEBUI = Path(__file__).with_name("webui")
DEFAULT_DB = ROOT / "data" / "queries.db"

app = FastAPI(title="问询回复检索", docs_url=None, redoc_url=None)
DB_PATH = DEFAULT_DB
AUTH = ""  # "用户名:密码" 则开启 Basic 认证；空串表示不校验（本机自己用）
IDF_TABLE: dict[str, float] = {}


@app.middleware("http")
async def _basic_auth(request: Request, call_next):
    """把服务挂到公网时，至少挡一层口令。

    浏览器第一次会弹一次账号密码框，之后同源的 fetch 会自动带上
    Authorization 头，所以前端一行都不用改。
    """
    if AUTH:
        head = request.headers.get("authorization", "")
        got = ""
        if head.startswith("Basic "):
            try:
                got = base64.b64decode(head[6:]).decode("utf-8")
            except Exception:
                got = ""
        if not secrets.compare_digest(got, AUTH):
            return Response(status_code=401, headers={
                "WWW-Authenticate": 'Basic realm="wenxun", charset="UTF-8"'})
    return await call_next(request)


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


class SearchIn(BaseModel):
    q: str
    board: str | None = None
    chapter: str | None = None
    round: int | None = None
    top: int = 5
    expand: bool = False
    boilerplate: bool = False


@app.get("/")
def index() -> FileResponse:
    return FileResponse(WEBUI / "index.html")


@app.get("/api/stats")
def api_stats() -> dict:
    conn = connect()
    try:
        s = stats(conn)
        s["top_grams"] = top_grams(conn, 8)
        return s
    finally:
        conn.close()


@app.post("/api/search")
def api_search(body: SearchIn) -> dict:
    if not body.q.strip():
        raise HTTPException(status_code=400, detail="请输入问题")
    conn = connect()
    try:
        return search_grouped(
            conn, body.q, board=body.board, chapter=body.chapter, round_no=body.round,
            top=body.top, include_boilerplate=body.boilerplate, expand=body.expand,
        )
    finally:
        conn.close()


@app.get("/api/siblings")
def api_siblings(question_id: str) -> dict:
    conn = connect()
    try:
        return {"items": siblings(conn, question_id)}
    finally:
        conn.close()


def _already_running(url: str) -> bool:
    """端口上是不是已经有我们的服务在跑（而不是别的程序占了这个端口）。"""
    import json
    import urllib.error
    import urllib.request
    try:
        with urllib.request.urlopen(url + "/api/stats", timeout=1.5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code == 401  # 401 = 是我们的服务，只是加了口令
    except Exception:
        return False
    return isinstance(data, dict) and "queries" in data


def _open_when_ready(url: str, host: str, port: int, timeout: float = 30.0) -> None:
    """等端口真的能连上，再用默认浏览器打开，避免浏览器抢跑看到连接失败。"""
    import socket
    import time
    import webbrowser
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket() as s:
            s.settimeout(0.4)
            if s.connect_ex((host, port)) == 0:
                time.sleep(0.3)
                webbrowser.open(url)
                return
        time.sleep(0.25)


def main() -> None:
    global DB_PATH, AUTH
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.environ.get("QX_DB", str(DEFAULT_DB)))
    ap.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8765")))
    ap.add_argument("--auth", default=os.environ.get("AUTH", ""),
                    help="开启 Basic 认证，格式 用户名:密码；留空表示不校验")
    ap.add_argument("--reload", action="store_true")
    ap.add_argument("--open", action="store_true", help="启动后用默认浏览器打开页面")
    args = ap.parse_args()

    AUTH = args.auth.strip()
    DB_PATH = Path(args.db)
    if not DB_PATH.exists():
        raise SystemExit(
            "找不到数据库 " + str(DB_PATH)
            + "。请先运行：.venv\\Scripts\\python.exe -m src.build_db --rebuild")

    shown = "127.0.0.1" if args.host in ("0.0.0.0", "::") else args.host
    url = "http://" + shown + ":" + str(args.port)
    if args.open:
        if _already_running(url):
            print("服务已经在运行，直接打开 " + url)
            import webbrowser
            webbrowser.open(url)
            return
        import threading
        threading.Thread(target=_open_when_ready, args=(url, args.host, args.port),
                         daemon=True).start()

    import uvicorn
    print("问询回复检索已启动 -> " + url + "   （Ctrl+C 停止）")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
