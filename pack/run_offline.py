# -*- coding: utf-8 -*-
"""问询检索 · 离线版启动器（读者双击 启动.bat 时跑的就是这个）。

做四件事：
  1. 检查数据文件在不在、这个文件夹能不能写；
  2. 第一次运行时自建检索索引（实测约 6 秒，之后秒开）；
  3. 找一个没被占用的端口；
  4. 起服务并自动打开浏览器。
"""
from __future__ import annotations

import os
import socket
import sqlite3
import sys
import threading
import time
import traceback
import webbrowser
from pathlib import Path

APP = Path(__file__).resolve().parent          # ...\app
ROOT = APP.parent                              # 包根目录
DB = ROOT / "data" / "queries.db"
LOG = ROOT / "启动日志.txt"

sys.path.insert(0, str(APP))


def line(text: str = "") -> None:
    print(text, flush=True)


def title() -> None:
    line()
    line("  ================================================================")
    line("   问询检索 · 离线版")
    line("  ================================================================")
    line()


def writable_here() -> bool:
    probe = ROOT / ".write_test"
    try:
        probe.write_text("x", encoding="utf-8")
        probe.unlink()
        return True
    except Exception:
        return False


def build_index(conn: sqlite3.Connection) -> None:
    """自建 FTS5 触发索引 + 4 个二级索引（约 6 秒）。"""
    t0 = time.time()
    line("[2/4] 第一次运行，正在建立检索索引（约 6 秒，只需一次）...")
    conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS query_fts USING fts5("
                 "q_id UNINDEXED, norm_loose, tokenize = 'trigram')")
    conn.execute("INSERT INTO query_fts(q_id, norm_loose) SELECT q_id, norm_loose FROM query")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_q_align ON query(chapter_std, question_no)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_q_src   ON query(board, round)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_q_norm  ON query(norm_strict)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_q_letter ON query(letter_id)")
    conn.commit()
    line("      完成，用时 %.1f 秒" % (time.time() - t0))


def pick_port(start: int = 8765) -> int:
    for port in range(start, start + 40):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise SystemExit("本机 8765~8804 端口都被占用了，请关掉一些程序再试。")


def open_later(port: int) -> None:
    with socket.socket() as s:
        s.settimeout(0.5)
        for _ in range(120):
            if s.connect_ex(("127.0.0.1", port)) == 0:
                time.sleep(0.4)
                webbrowser.open("http://127.0.0.1:%d" % port)
                return
            time.sleep(0.25)


def run() -> None:
    title()

    if not DB.exists():
        line("  [错误] 找不到数据文件：")
        line("         " + str(DB))
        line("         多半是压缩包没解压完整，请重新解压一次（要解压，不能直接在压缩包里双击）。")
        return

    if not writable_here():
        line("  [错误] 这个文件夹不可写，没法建立索引。")
        line("         请把整个文件夹解压到【桌面】或【D 盘】，不要放在 C:\\Program Files 下面。")
        return

    line("[1/4] 数据文件 OK（%.0f MB）" % (DB.stat().st_size / 1048576))

    conn = sqlite3.connect(str(DB))
    try:
        if conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='query_fts'").fetchone()[0] == 0:
            build_index(conn)
        else:
            line("[2/4] 索引已就绪，跳过")
    finally:
        conn.close()

    port = pick_port()
    line("[3/4] 端口 %d 可用" % port)
    line("[4/4] 启动服务，浏览器马上自动打开")
    line("       没有自动打开的话，手动访问：http://127.0.0.1:%d" % port)
    line()
    line("  ----------------------------------------------------------------")
    line("   这个窗口关掉，网站就停了。想再开就再双击一次 启动.bat。")
    line("  ----------------------------------------------------------------")
    line()

    threading.Thread(target=open_later, args=(port,), daemon=True).start()

    import uvicorn
    import src.server as srv
    # 包里 src/ 在 app/ 下面，server.py 按"自己的上两级"推数据库路径会推到 app/data，
    # 而数据库在包根目录的 data/。这里直接指定，别让它去猜。
    srv.DB_PATH = DB
    uvicorn.run(srv.app, host="127.0.0.1", port=port, log_level="warning")


def main() -> None:
    try:
        run()
    except KeyboardInterrupt:
        line("\n已停止。")
    except Exception:
        detail = traceback.format_exc()
        line()
        line("  出错了，把下面这段（或者 " + LOG.name + "）发给作者：")
        line()
        line(detail)
        try:
            LOG.write_text(detail, encoding="utf-8")
            line("  已写入：" + str(LOG))
        except Exception:
            pass


if __name__ == "__main__":
    main()
