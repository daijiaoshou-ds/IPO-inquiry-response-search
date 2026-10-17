# -*- coding: utf-8 -*-
"""入库质量自检：材料有没有混进 query、套话比例、长度分布等。

用法：.venv\\Scripts\\python.exe -m src.qc --db data/queries.db
"""
from __future__ import annotations

import argparse
import collections
import io
import sqlite3
import statistics

MATERIAL_HINTS = ["根据申请文件", "根据招股说明书", "申报材料显示", "招股说明书显示", "申请文件显示"]


def report(db: str) -> str:
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    buf = io.StringIO()
    rows = conn.execute("SELECT * FROM query ORDER BY company, seq_in_letter").fetchall()
    n = len(rows)
    print(f"数据库 {db}   query 总数 {n}", file=buf)
    print(f"问询函 {conn.execute('SELECT COUNT(*) FROM letter').fetchone()[0]} 份，"
          f"公司 {conn.execute('SELECT COUNT(DISTINCT company) FROM letter').fetchone()[0]} 家，"
          f"问题节 {conn.execute('SELECT COUNT(*) FROM question').fetchone()[0]} 个", file=buf)

    print("\n[1] 各公司分布", file=buf)
    for r in conn.execute(
            "SELECT company, COUNT(*) c, SUM(is_boilerplate) b FROM query GROUP BY company ORDER BY c DESC"):
        print(f"    {r['company']:<10} {r['c']:>4} 条（套话 {r['b']}）", file=buf)

    print("\n[2] 章节分布", file=buf)
    for r in conn.execute("SELECT chapter_std, COUNT(*) c FROM query "
                          "GROUP BY chapter_std ORDER BY c DESC"):
        print(f"    {r['c']:>4}  {(r['chapter_std'] or '(无章节)')}", file=buf)

    print("\n[3] 疑似混入材料（query 里出现'根据申请文件'等字样）", file=buf)
    bad = [r for r in rows if any(h in r["raw_text"] for h in MATERIAL_HINTS)]
    print(f"    命中 {len(bad)}/{n} = {len(bad)/n:.1%}", file=buf)
    for r in bad[:8]:
        print(f"      {r['company']} | {r['raw_text'][:80]}", file=buf)

    print("\n[4] 长度分布", file=buf)
    lens = sorted(r["char_len"] for r in rows)
    print(f"    min={lens[0]} p25={lens[n//4]} 中位={statistics.median(lens)} "
          f"p75={lens[3*n//4]} max={lens[-1]}", file=buf)
    print(f"    <20 字 {sum(1 for x in lens if x < 20)} 条；>250 字 {sum(1 for x in lens if x > 250)} 条", file=buf)

    print("\n[5] 提问主体分布", file=buf)
    for r in conn.execute("SELECT asker, COUNT(*) c FROM query GROUP BY asker ORDER BY c DESC LIMIT 10"):
        print(f"    {r['c']:>4}  {r['asker']}", file=buf)

    print("\n[6] 小问标题覆盖率", file=buf)
    withsub = sum(1 for r in rows if r["sub_question"])
    print(f"    有 sub_question 的 {withsub}/{n} = {withsub/n:.0%}", file=buf)

    print("\n[7] 完全重复的 raw_text", file=buf)
    dup = collections.Counter(r["raw_text"] for r in rows)
    rep = [(k, v) for k, v in dup.items() if v > 1]
    print(f"    重复文本 {len(rep)} 组，涉及 {sum(v for _, v in rep)} 条", file=buf)
    for k, v in sorted(rep, key=lambda x: -x[1])[:6]:
        print(f"      x{v}  {k[:70]}", file=buf)

    print("\n[8] 最长 / 最短 query", file=buf)
    for r in sorted(rows, key=lambda x: -x["char_len"])[:3]:
        print(f"    最长 {r['char_len']:>4} {r['company']} | {r['raw_text'][:70]}", file=buf)
    for r in sorted(rows, key=lambda x: x["char_len"])[:5]:
        print(f"    最短 {r['char_len']:>4} {r['company']} | {r['raw_text']}", file=buf)
    conn.close()
    return buf.getvalue()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/queries.db")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    text = report(args.db)
    if args.out:
        open(args.out, "w", encoding="utf-8").write(text)
    else:
        print(text)


if __name__ == "__main__":
    main()
