# -*- coding: utf-8 -*-
"""把 data/queries.db 整理成一个干净的、方便传出去的副本。

    .venv\\Scripts\\python.exe deploy\\pack_db.py          # 只整理
    .venv\\Scripts\\python.exe deploy\\pack_db.py --gz     # 再压成 .gz

为什么不直接 scp data/queries.db：
  * 它是 WAL 模式，万一有个没合并的 -wal 文件，拷走的 .db 会缺最近的写入；
  * 420 MB 里带了不少碎片，传起来更慢。
VACUUM INTO 生成的副本是单文件、journal_mode=delete、无碎片，
就算落到只读文件系统上也能直接打开（Web 服务只读不写）。
"""
from __future__ import annotations

import argparse
import gzip
import shutil
import sqlite3
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "data" / "queries.db"


def human(n: int) -> str:
    return "%.1f MB" % (n / 1048576)


def main() -> None:
    ap = argparse.ArgumentParser(description="整理 / 压缩数据库副本")
    ap.add_argument("--src", default=str(SRC))
    ap.add_argument("--out", default="", help="默认写成 <src>_pack.db")
    ap.add_argument("--gz", action="store_true", help="顺带压成 .gz")
    args = ap.parse_args()

    src = Path(args.src)
    if not src.exists():
        raise SystemExit("找不到 " + str(src))
    out = Path(args.out) if args.out else src.with_name(src.stem + "_pack.db")

    con = sqlite3.connect(str(src))
    try:
        print("源：%s  %s  journal_mode=%s"
              % (src, human(src.stat().st_size),
                 con.execute("pragma journal_mode").fetchone()[0]))
        t0 = time.time()
        print("integrity_check：", con.execute("pragma integrity_check").fetchone()[0])
        out.unlink(missing_ok=True)
        con.execute("VACUUM INTO '" + str(out).replace("'", "''") + "'")
    finally:
        con.close()

    tidy = sqlite3.connect(str(out))
    try:
        tidy.execute("pragma journal_mode=delete")
        n = tidy.execute("select count(*) from query").fetchone()[0]
    finally:
        tidy.close()
    print("产出：%s  %s  共 %d 条 query  用时 %.1f s"
          % (out, human(out.stat().st_size), n, time.time() - t0))

    if args.gz:
        gz = out.with_suffix(out.suffix + ".gz")
        t1 = time.time()
        with open(out, "rb") as fi, gzip.open(gz, "wb", compresslevel=6) as fo:
            shutil.copyfileobj(fi, fo, 1 << 20)
        print("压缩：%s  %s（%.1fx，用时 %.1f s）"
              % (gz, human(gz.stat().st_size),
                 out.stat().st_size / max(1, gz.stat().st_size), time.time() - t1))


if __name__ == "__main__":
    main()
