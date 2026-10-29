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

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

from .ingest import load_bytes
from .search import search_grouped, siblings, stats, top_grams

ROOT = Path(__file__).resolve().parent.parent
WEBUI = Path(__file__).with_name("webui")
DEFAULT_DB = ROOT / "data" / "queries.db"

app = FastAPI(title="问询回复检索", docs_url=None, redoc_url=None)


@app.on_event("startup")
def _warm_up() -> None:
    """把慢的一次性开销挪到启动时，别让第一个用户吃 3 秒。

    两件事：① Rust 内核常驻进程 + 索引 mmap；② Python 侧 IDF 表（307 万条 gram，
    约 2.8 s，covered_spans 判断"弱证据"要用）。都放后台线程，不阻塞端口就绪。
    """
    import threading
    from .search import load_idf

    def work():
        try:
            from .engine_rust import get_engine
            get_engine()
        except Exception as e:
            print("[engine] 预热 Rust 内核失败：" + str(e))
        try:
            load_idf(connect())
        except Exception as e:
            print("[engine] 预热 IDF 失败：" + str(e))

    threading.Thread(target=work, name="warmup", daemon=True).start()

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
    # 章节筛选：前端已下线（回复体裁板块抽出来的章节是噪声），字段留着给接口/命令行用
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


# 一份函最多多少个小问还愿意跑。50~130 是常态，超过这个数多半是文档没拆对，
# 与其让用户等三分钟，不如直接报错让人检查文件。
MAX_SUBS = 400
MAX_UPLOAD = 60 * 1024 * 1024


@app.post("/api/upload")
async def api_upload(file: UploadFile = File(...),
                     board: str = Form(""),
                     top: int = Form(5),
                     expand: bool = Form(False)) -> dict:
    """上传一整份问询函（pdf / docx / txt），自动拆题拆小问，逐个检索。

    返回结构和 /api/search 一致，多两个字段：`source`（文件名）与每组上的
    `label`（题N · 小问K），前端直接复用同一套渲染。
    """
    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="文件是空的")
    if len(raw) > MAX_UPLOAD:
        raise HTTPException(status_code=413, detail="文件太大（上限 60 MB）")
    try:
        letter = load_bytes(raw, file.filename or "", board=board.strip())
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:                      # 坏 PDF、加密 PDF 等
        raise HTTPException(status_code=400,
                            detail="解析失败：" + type(e).__name__ + " " + str(e)[:120])

    subs = list(letter.queries)
    if not subs:
        raise HTTPException(status_code=400,
                            detail="没能从这份文件里拆出问题，确认一下是不是问询函正文")
    if len(subs) > MAX_SUBS:
        raise HTTPException(status_code=400,
                            detail="拆出 %d 个小问，超过上限 %d，像是拆分出了问题，请检查文件"
                                   % (len(subs), MAX_SUBS))

    use_board = None if expand else (board.strip() or None)
    conn = connect()
    try:
        groups, merged = [], {}
        for q in subs:
            r = search_grouped(conn, q.raw_text, board=use_board, top=top,
                               include_boilerplate=False, expand=bool(expand))
            # 一个小问内部若又被拆成几句，在这里合回一条，界面上仍是"一问一组"
            hits, seen = [], set()
            for g in r["groups"]:
                for h in g["hits"]:
                    if h["q_id"] in seen:
                        continue
                    seen.add(h["q_id"])
                    hits.append(h)
                    old = merged.get(h["q_id"])
                    if old is None or h["score"] > old["score"]:
                        merged[h["q_id"]] = h
            hits.sort(key=lambda h: (-h["score"], h["q_id"]))
            cover = r["groups"][0]["cover"] if r["groups"] else {"strong": [], "weak": [], "ratio": 0}
            groups.append({
                "text": q.raw_text,
                "label": "题%s · 小问%s" % (q.question_no, q.item_no),
                "question": q.question,
                "hits": hits[:top],
                "cover": cover,
            })
        flat = sorted(merged.values(), key=lambda h: (-h["score"], h["q_id"]))[:top]
        return {
            "source": file.filename,
            "kind": getattr(letter, "kind", ""),
            "questions": [{"no": q["no"], "title": q["title"]} for q in letter.questions],
            "parts": [g["text"] for g in groups],
            "groups": groups,
            "flat": flat,
        }
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
