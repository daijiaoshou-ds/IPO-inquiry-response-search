# -*- coding: utf-8 -*-
"""把一份问询函做成「参考答案指引」：拆题 → 逐小问检索 → 汇总成表。

给人看的，不是给 AI 看的。表里只有"去哪看"：哪个板块、哪家公司、第几轮、
哪道题，以及撞上的是哪一句问法。不做任何答案总结——答案要人自己翻原文。

用法：
    .venv\\Scripts\\python.exe -m scripts.make_guide --pdf 新收到的函.pdf
    .venv\\Scripts\\python.exe -m scripts.make_guide --pdf xxx.pdf --out 指引.xlsx --top 5
    .venv\\Scripts\\python.exe -m scripts.make_guide --text 粘贴的题.txt --md

输出（默认到 out/ 目录）：
    <函名>_参考答案指引_<日期>.xlsx    四张表：说明 / 命中排行 / 明细TOP2 / 明细TOP5
    <函名>_参考答案指引_<日期>.md      同样的内容，纯文本版（--md 时生成）

表本身由 src/guide.py 生成——网页上「导出 Excel」走的是同一个函数，
命令行和网页出的表必须逐格一致，否则人会以为其中一份是错的。
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.guide import (  # noqa: E402  (要在 sys.path 之后)
    DEFAULT_MIN, band, guess_company, guide_of, make_meta, rank_objects,
    rows_from_letter, write_xlsx,
)


def load_letter(path: Path, board: str):
    """PDF / docx / txt 三路统一入口，拆分规则都在 src/ingest.py。"""
    from src.ingest import load_file
    return load_file(path, board=board)


def write_md(path: Path, rows: list[dict], meta: dict) -> None:
    L = ["# 参考答案指引", "",
         "- 待答函：%s" % meta["source"],
         "- 拆出：%d 道题 / %d 个小问" % (meta["n_q"], meta["n_sub"]),
         "- 语料：%s" % meta["corpus"],
         "- 生成：%s" % meta["ts"], "",
         "> 只给定位，不给答案摘要。", "",
         "## 命中排行", "",
         "| 对象 | 问题命中数量 | 占问题比重 |", "| --- | --- | --- |"]
    ranked = [x for x in rank_objects(rows) if x[1] >= 2] or rank_objects(rows)[:10]
    for name, n, ratio in ranked:
        L.append("| %s | %d | %.2f%% |" % (name, n, ratio * 100))

    L += ["", "## 明细", "",
          "| 题号 | 问题 | 小问索引 | 小问 | 参考公司 | 检索到的问题 | 相似度 | 指引 |",
          "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for row in rows:
        for h in row["hits"]:
            L.append("| %s | %s | %s | %s | %s | %s | %s | %s |" % (
                row["qno"], row["qtitle"].replace("|", "／"), row["sub_idx"],
                row["ask"].replace("|", "／"), h["company"],
                h["raw_text"].replace("|", "／"), band(h["score"]), guide_of(h)))

    empty_subs = [r for r in rows if not r["hits"]]
    if empty_subs:
        L += ["", "### 未找到参考的小问", ""]
        for r in empty_subs:
            L.append("- 题%s·小问%s（最像 %.2f）" % (
                r["qno"], r["sub_idx"], r.get("best_any", 0.0)))
    path.write_text("\n".join(L), encoding="utf-8")


def search_letter(conn, letter, top: int, min_score: float,
                  exclude: set[str]) -> list[dict]:
    """逐个最小 query 检索，摊成"一行一个小问"的中间结构（表和网页共用）。"""
    return rows_from_letter(conn, letter, top, min_score, exclude)


def main() -> None:
    ap = argparse.ArgumentParser()
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--pdf", help="待答的问询函 PDF")
    src.add_argument("--text", help="待答的题目文本文件（.txt/.md）")
    src.add_argument("--docx", help="待答的问询函 Word（.docx）")
    ap.add_argument("--out", help="输出 xlsx 路径；不给就放 out/")
    ap.add_argument("--board", default="问询函",
                    help="板块标签，只影响抽取器走哪套规则。新收到的问询函用默认值就行")
    ap.add_argument("--top", type=int, default=5, help="每个小问给几条参考（默认 5）")
    ap.add_argument("--min-score", type=float, default=DEFAULT_MIN)
    ap.add_argument("--exclude", default=None,
                    help="排除这家公司（默认按文件名自动排除，避免自己匹配自己）")
    ap.add_argument("--md", action="store_true", help="同时输出 Markdown 版")
    args = ap.parse_args()

    t0 = time.perf_counter()
    if args.pdf:
        path = Path(args.pdf)
    elif args.docx:
        path = Path(args.docx)
    else:
        path = Path(args.text)
    if not path.exists():
        raise SystemExit("找不到文件：" + str(path))
    try:
        letter = load_letter(path, args.board)
    except (ValueError, FileNotFoundError) as e:
        raise SystemExit(str(e))
    source = path.name
    t_split = time.perf_counter() - t0

    # 自动排除自己：文件名里的公司名，若库里存在就排掉
    exclude = set()
    if args.exclude:
        exclude.add(args.exclude)
    else:
        stem = re.sub(r"_r\d+$", "", Path(source).stem)
        exclude.add(stem)
        guess = guess_company(stem)
        if guess:
            exclude.add(guess)
    conn = sqlite3.connect(str(ROOT / "data" / "queries.db"))
    conn.row_factory = sqlite3.Row

    t1 = time.perf_counter()
    rows = search_letter(conn, letter, args.top, args.min_score, exclude)
    t_search = time.perf_counter() - t1

    meta = make_meta(source, len(letter.questions), len(letter.queries), conn,
                     args.top, args.min_score)

    out_dir = ROOT / "out"
    out_dir.mkdir(exist_ok=True)
    stem = re.sub(r"\.(pdf|txt|md|docx)$", "", source, flags=re.I)
    date = datetime.now().strftime("%Y%m%d")
    xlsx = Path(args.out) if args.out else out_dir / ("%s_参考答案指引_%s.xlsx" % (stem, date))
    write_xlsx(xlsx, rows, meta, args.top)
    made = [xlsx]
    if args.md:
        md = xlsx.with_suffix(".md")
        write_md(md, rows, meta)
        made.append(md)

    total_hits = sum(len(r["hits"]) for r in rows)
    empty = sum(1 for r in rows if not r["hits"])
    print("拆题 %.1f s ／ 检索 %.1f s ／ 合计 %.1f s" % (
        t_split, t_search, time.perf_counter() - t0))
    print("拆出 %d 道题 / %d 个小问；取回 %d 条参考（%d 个小问没到门槛）" % (
        len(letter.questions), len(letter.queries), total_hits, empty))

    # 把识别到的题目打印出来——切分对不对，看这一列最快。
    # 换一份没见过的函（另一个交易所、另一个年份）时，先核对这里再信表。
    print()
    print("识别到的题目：")
    cnt: dict = {}
    for q in letter.queries:
        cnt[q.question_no] = cnt.get(q.question_no, 0) + 1
    for q in letter.questions:
        print("   题%-2s  %-34s %2d 个小问" % (
            q["no"], (q["title"] or "—")[:34], cnt.get(q["no"], 0)))
    # 题太少、小问太多 = 题目没切开，全并进一题了。这种表看着能用，其实全错位。
    n_q, n_s = len(letter.questions), len(letter.queries)
    if n_q <= 1 or (n_q <= 3 and n_s >= 15):
        print()
        print("!! 拆出 %d 题 / %d 小问，题目很可能没切开（正常一份函 5~25 题，"
              "每题几个小问）。" % (n_q, n_s))
        print("   判据是「问题N.」/「1.关于…」/「短标题行 + 下一行是正文」，三种都不中"
              "就会并成一题。请核对上面「识别到的题目」那一列。")
    print()
    for m in made:
        print("已生成：" + str(m))


if __name__ == "__main__":
    main()
