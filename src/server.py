# -*- coding: utf-8 -*-
"""本地 Web 服务：FastAPI + 单页前端（无构建步骤）。

启动：
    .venv\\Scripts\\python.exe -m src.server
然后打开 http://127.0.0.1:8765
"""
from __future__ import annotations

import argparse
import base64
import io
import os
import re
import secrets
import sqlite3
import time
from collections import OrderedDict
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

from .guide import build_workbook, guess_company, make_meta, rows_from_letter
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
    # 单页应用没有构建步骤，改一行 HTML 就是一次前端更新。
    # 不给缓存头的话，浏览器会按 Last-Modified 做启发式缓存，用户看到的是旧页面
    # ——「改了前端但界面上没有」这个坑已经踩过一次，用 no-store 一次解决。
    return FileResponse(WEBUI / "index.html",
                        headers={"Cache-Control": "no-store, must-revalidate"})


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

# ---- 导出缓存 ----------------------------------------------------------
# 「上传 -> 看结果 -> 导出 Excel」中间隔着几十秒检索。导出时重新跑一遍检索是不可
# 接受的（同一份函跑两次还可能因为并发取数不同而给出两个数字），所以上传算完就
# 把 rows 留在内存里，导出只做"把 rows 写成 xlsx"，秒回。
# 只留最近几份、两小时过期：这是一台自己用的机器，不做持久化，也不做清理线程。
_EXPORTS: "OrderedDict[str, dict]" = OrderedDict()
_EXPORT_MAX = 6
_EXPORT_TTL = 2 * 3600


def _remember_export(rows: list[dict], meta: dict, top: int, name: str) -> str:
    now = time.time()
    for k in [k for k, v in _EXPORTS.items() if now - v["at"] > _EXPORT_TTL]:
        _EXPORTS.pop(k, None)
    token = secrets.token_urlsafe(9)
    _EXPORTS[token] = {"rows": rows, "meta": meta, "top": top, "name": name, "at": now}
    while len(_EXPORTS) > _EXPORT_MAX:
        _EXPORTS.popitem(last=False)      # 先丢最旧的
    return token


def _xlsx_name(filename: str) -> str:
    stem = re.sub(r"\.(pdf|docx|doc|txt|md)$", "", filename or "", flags=re.I).strip()
    stem = re.sub(r'[\\/:*?"<>|]', "_", stem)[:60] or "问询函"
    return "%s_参考答案指引_%s.xlsx" % (stem, datetime.now().strftime("%Y%m%d"))


@app.get("/api/export/{token}")
def api_export(token: str):
    """把刚才那次上传的结果写成 xlsx 直接下载。

    表由 src/guide.py 的 build_workbook 生成 —— 和命令行 `scripts.make_guide`
    出的是同一个函数，同一个 rows，逐格一致。
    """
    item = _EXPORTS.get(token)
    if item is None:
        raise HTTPException(
            status_code=404,
            detail="这份结果已经过期（服务重启或超过 2 小时），请重新上传一次再导出")
    buf = io.BytesIO()
    try:
        build_workbook(item["rows"], item["meta"], item["top"]).save(buf)
    except ImportError as e:      # 镜像里没装 openpyxl
        raise HTTPException(
            status_code=501,
            detail="这个部署没装 openpyxl，导出只在装了它的环境可用（本机版、离线包都带）："
                   + str(e))
    name = item["name"]
    # 中文文件名要走 RFC 5987；只给 filename= 的话，老浏览器会存成一串 %E9%97%AE…
    disp = "attachment; filename=\"guide_%s.xlsx\"; filename*=UTF-8''%s" % (
        datetime.now().strftime("%Y%m%d"), quote(name))
    return Response(
        content=buf.getvalue(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": disp,
                 "Content-Length": str(buf.tell()),
                 "Cache-Control": "no-store"},
    )


@app.post("/api/upload")
async def api_upload(file: UploadFile = File(...),
                     board: str = Form(""),
                     top: int = Form(5),
                     expand: bool = Form(True)) -> dict:
    """上传一整份问询函（pdf / docx / txt），自动拆题拆小问，逐个检索。

    返回结构和 /api/search 一致，多三样：`source`（文件名）、每组上的
    `label`（题N · 小问K）、`export`（一个 token，凭它下载 Excel）。

    检索范围固定是全板块：整份函里的小问五花八门，按某个板块硬过滤只会漏。
    `board` 只体现给抽取器（"这份文件按哪套题号体例切"），不参与过滤。
    """
    t0 = time.perf_counter()
    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="文件是空的")
    if len(raw) > MAX_UPLOAD:
        raise HTTPException(status_code=413, detail="文件太大（上限 60 MB）")
    try:
        letter = load_bytes(raw, file.filename or "", board=board.strip())
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except ImportError as e:
        # 部署镜像（deploy/requirements.txt）刻意只装 fastapi+uvicorn，没有 PDF/Word 解析栈。
        # 与其 500，不如明说这个环境没有上传能力。
        raise HTTPException(status_code=501,
                            detail="这个部署没装文件解析组件（pdfplumber / python-docx），"
                                   "上传整份函只在本机版和离线包里可用：" + str(e))
    except Exception as e:                      # 坏 PDF、加密 PDF 等
        raise HTTPException(status_code=400,
                            detail="解析失败：" + type(e).__name__ + " " + str(e)[:120])

    if not letter.queries:
        raise HTTPException(status_code=400,
                            detail="没能从这份文件里拆出问题，确认一下是不是问询函正文")
    if len(letter.queries) > MAX_SUBS:
        raise HTTPException(status_code=400,
                            detail="拆出 %d 个小问，超过上限 %d，像是拆分出了问题，请检查文件"
                                   % (len(letter.queries), MAX_SUBS))

    # 自己不能当自己的参考：文件名里抠得出公司名就排掉（抠不出就算了，宁可不错排）
    exclude: set[str] = set()
    guess = guess_company(file.filename or "")
    if guess:
        exclude.add(guess)

    conn = connect()
    try:
        rows = rows_from_letter(conn, letter, top, 0.0, exclude)
        meta = make_meta(file.filename or "", len(letter.questions),
                         len(letter.queries), conn, top)
    finally:
        conn.close()

    groups = [{
        "text": r["ask"],
        "label": "题%s · 小问%s" % (r["qno"], r["sub_idx"]),
        "question": r["qtitle"],
        "hits": r["hits"],
        "cover": r["cover"],
    } for r in rows]

    merged: dict = {}
    for r in rows:
        for h in r["hits"]:
            old = merged.get(h["q_id"])
            if old is None or h["score"] > old["score"]:
                merged[h["q_id"]] = h
    flat = sorted(merged.values(), key=lambda h: (-h["score"], h["q_id"]))[:top]

    name = _xlsx_name(file.filename or "")
    token = _remember_export(rows, meta, top, name)
    return {
        "source": file.filename,
        "kind": getattr(letter, "kind", ""),
        "questions": [{"no": q["no"], "title": q["title"]} for q in letter.questions],
        "parts": [g["text"] for g in groups],
        "groups": groups,
        "flat": flat,
        "export": {"token": token, "name": name},
        "elapsed": round(time.perf_counter() - t0, 1),
    }


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
