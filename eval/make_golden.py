# -*- coding: utf-8 -*-
"""建黄金集：把当前 Python 引擎的检索结果固化成文件，作为 Rust 版的对照标准。

为什么要这个文件：
  git 能回退"代码"，但回退不了"结果"。Rust 版跑出来分数正常、排序正常，
  却可能让本该排第一的那条掉到第七 —— 这种退化没人会发现。
  黄金集把"200 条真实查询的 top-10"存下来，任何实现换进来都能一条命令比对。

产出 eval/golden.json，结构（同时也是 Rust 侧的输入契约）：
  {
    "meta": {...},                      # 库指纹、时间、git 版本
    "cases": [{
      "id": 0, "text": "原始输入", "board": null,
      "parts": [{
         "text": "拆出来的分句（原文）",
         "core": "...", "loose": "...", "strict": "...",   # 三层归一化，Python 算好
         "clauses": ["..."],                                # 分句比对用的 norm_core 片段
         "hits": [{"q_id":..,"score":..,"sim":..,"lcs":..,"exact":..,"board":..,"company":..}],
         "cover_ratio": 0.0
      }],
      "flat": [{"q_id":..,"score":..}]                       # 全局合并后的 top
    }]
  }

用法：
    python -m eval.make_golden                 # 默认 200 条
    python -m eval.make_golden --n 40          # 小样本，先试
    python -m eval.make_golden --out eval/golden.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 黄金集必须由 Python 引擎生成——它是"参照物"。search_grouped 默认会走 Rust，
# 所以这里强制关掉，否则拿 Rust 的结果当基准，比对就变成自己跟自己比。
os.environ["QB_ENGINE"] = "py"

from src.normalize import is_boilerplate, norm_core, norm_loose, norm_strict  # noqa: E402
from src.search import GRAM_N, load_idf, query_clauses, search_grouped  # noqa: E402

BOARDS = ["北交所", "新三板", "创业板", "深主板", "科创板", "沪主板"]


def db_fingerprint(conn: sqlite3.Connection) -> dict:
    """库的指纹：黄金集只在这一版库上有意义，换了库必须重建。"""
    q = conn.execute("SELECT COUNT(*), MAX(q_id), SUM(char_len) FROM query").fetchone()
    return {
        "queries": q[0],
        "max_q_id": q[1],
        "sum_char_len": q[2],
        "grams": conn.execute("SELECT COUNT(*) FROM gram_df").fetchone()[0],
        "letters": conn.execute("SELECT COUNT(*) FROM letter").fetchone()[0],
        "gram_n": GRAM_N,
    }


def git_rev() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT,
                              capture_output=True, text=True, timeout=10).stdout.strip()
    except Exception:
        return ""


def sample_cases(conn: sqlite3.Connection, n: int, seed: int) -> list[dict]:
    """分层抽样：板块 × 长度 × 套话 × 长粘贴。

    不是随便抽 n 条。要覆盖的是"算法会走不同分支"的那些情形：
      - 6 个板块（board 过滤分支）
      - 短/中/长/超长（分句预算、LCS 预算、FTS 词数上限分支）
      - 套话（BOILERPLATE_PENALTY 分支）
      - 多段拼接（search_grouped 的多 parts 合并分支）
    """
    rnd = random.Random(seed)
    picked: list[dict] = []
    seen: set[int] = set()

    def add(q_id: int, text: str, board, tag: str):
        if q_id in seen or not text.strip():
            return
        seen.add(q_id)
        picked.append({"q_id": q_id, "text": text, "board": board, "tag": tag})

    def rows(sql: str, params=()):
        return conn.execute(sql, params).fetchall()

    # 1) 每个板块 4 个长度区间各若干条。200 条时每档 5 条 → 6×4×5=120 条
    per_bucket = max(2, n // 40)
    for b in BOARDS:
        buckets = [
            ("短", "AND char_len < 45"),
            ("中", "AND char_len BETWEEN 45 AND 95"),
            ("长", "AND char_len BETWEEN 95 AND 260"),
            ("超长", "AND char_len > 260"),
        ]
        for tag, cond in buckets:
            r = rows("SELECT q_id, raw_text FROM query WHERE board = ? AND is_boilerplate = 0 "
                     + cond + " ORDER BY RANDOM() LIMIT ?", (b, per_bucket))
            for q_id, text in r:
                add(q_id, text, b, b + "/" + tag)

    # 2) 套话（选上的会被 include_boilerplate=False 过滤掉，正好测这条通路）
    for q_id, text, b in rows(
            "SELECT q_id, raw_text, board FROM query WHERE is_boilerplate = 1 "
            "ORDER BY RANDOM() LIMIT ?", (max(3, n // 20),)):
        add(q_id, text, b, "套话")

    # 3) 整段粘贴：用户真实用法是把一整道题（含几个小问）粘进来。
    #    这里按"（1）…；（2）…；（3）…"拼，实测 split_user_input 会拆成 3 段；
    #    纯换行或"1."编号反而不拆（split_items 只认分号和括号编号），别写成那样。
    long_rows = rows("SELECT q_id, raw_text FROM query WHERE is_boilerplate = 0 "
                     "AND char_len BETWEEN 30 AND 80 ORDER BY RANDOM() LIMIT 90")
    i = 0
    n_concat = max(3, n // 10)
    while i // 3 < n_concat and i + 2 < len(long_rows):
        a, b_, c = long_rows[i][1], long_rows[i + 1][1], long_rows[i + 2][1]
        text = "（1）" + a + "；（2）" + b_ + "；（3）" + c
        picked.append({"q_id": -1 - i, "text": text, "board": None, "tag": "整段粘贴"})
        i += 3

    # 3b) 带提问主体的写法（ASKER_RE 命中那条路径，切出来的段会带"核查并发表明确意见"前缀）
    for j in range(max(2, n // 50)):
        if i + 1 >= len(long_rows):
            break
        a, b_ = long_rows[i][1], long_rows[i + 1][1]
        picked.append({"q_id": -1000 - j, "text": "请发行人" + a + "。请保荐机构" + b_,
                       "board": None, "tag": "提问主体"})
        i += 2

    # 4) 不够就随机补
    while len(picked) < n:
        r = rows("SELECT q_id, raw_text, board FROM query WHERE is_boilerplate = 0 "
                 "ORDER BY RANDOM() LIMIT 1")
        if not r:
            break
        add(r[0][0], r[0][1], r[0][2], "随机")

    rnd.shuffle(picked)
    return picked[:n]


def run_case(conn, case: dict, top: int) -> dict:
    t0 = time.perf_counter()
    res = search_grouped(conn, case["text"], board=case["board"], top=top)
    dt = time.perf_counter() - t0

    parts = []
    for g in res["groups"]:
        part_text = g["text"]
        parts.append({
            "text": part_text,
            # 三层归一化在这里算好落盘：Rust 侧不需要重写 normalize.py
            "core": norm_core(part_text),
            "loose": norm_loose(part_text),
            "strict": norm_strict(part_text),
            "clauses": query_clauses(part_text),
            "cover_ratio": g["cover"]["ratio"],
            "hits": [{
                "q_id": h["q_id"], "score": h["score"], "sim": h["sim"],
                "lcs": h["lcs"], "exact": h["exact"],
                "board": h["board"], "company": h["company"],
            } for h in g["hits"]],
        })
    return {
        "id": case["q_id"],
        "tag": case["tag"],
        "text": case["text"],
        "board": case["board"],
        "ms": round(dt * 1000, 1),
        "parts": parts,
        "flat": [{"q_id": h["q_id"], "score": h["score"]} for h in res["flat"]],
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="生成检索黄金集")
    ap.add_argument("--db", default=str(ROOT / "data" / "queries.db"))
    ap.add_argument("--out", default=str(ROOT / "eval" / "golden.json"))
    ap.add_argument("--n", type=int, default=200)
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--seed", type=int, default=20261028)
    args = ap.parse_args(argv)

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    fp = db_fingerprint(conn)
    cases = sample_cases(conn, args.n, args.seed)
    print("抽样 " + str(len(cases)) + " 条：")
    stat: dict[str, int] = {}
    for c in cases:
        stat[c["tag"]] = stat.get(c["tag"], 0) + 1
    for k in sorted(stat):
        print("    " + k + "  " + str(stat[k]))

    idf = load_idf(conn)   # 先把 307 万条 IDF 读进缓存，别算进单条耗时
    _ = idf

    out = []
    t_all = time.perf_counter()
    for i, c in enumerate(cases, 1):
        out.append(run_case(conn, c, args.top))
        if i % 20 == 0 or i == len(cases):
            print("    %d/%d  %.1fs" % (i, len(cases), time.perf_counter() - t_all))

    doc = {
        "meta": {
            "created": time.strftime("%Y-%m-%d %H:%M:%S"),
            "git": git_rev(),
            "db": str(args.db),
            "fingerprint": fp,
            "top": args.top,
            "seed": args.seed,
            "cases": len(out),
            "elapsed_s": round(time.perf_counter() - t_all, 1),
        },
        "cases": out,
    }
    p = Path(args.out)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")

    ms = sorted(c["ms"] for c in out)
    print()
    print("已写入 " + str(p) + "（%.1f MB）" % (p.stat().st_size / 1048576))
    print("  库指纹 query=%d grams=%d gram_n=%d"
          % (fp["queries"], fp["grams"], fp["gram_n"]))
    print("  单条耗时 中位 %.0f ms / p95 %.0f ms / 最大 %.0f ms"
          % (ms[len(ms) // 2], ms[int(len(ms) * 0.95)], ms[-1]))
    print("  sha256 " + hashlib.sha256(p.read_bytes()).hexdigest()[:16])
    return 0


if __name__ == "__main__":
    sys.exit(main())
