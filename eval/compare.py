# -*- coding: utf-8 -*-
"""黄金集比对：任何检索实现（Python / Rust / 未来的别的）都可以拿它验一遍。

核心问题只有一个：**换了实现之后，用户看到的答案有没有变**。
分数差 0.001 无所谓；top-1 换了、或前十掉三条，就是事故。

用法：
    # 1) 自检：用现在的 Python 引擎重跑一遍，应该 100% 一致
    python -m eval.compare --rerun

    # 2) 验 Rust：让 Rust 把结果写成同结构 JSON，再比
    python -m eval.compare --results eval/rust_out.json

    # 3) 只看某一档（比如只验长粘贴）
    python -m eval.compare --results eval/rust_out.json --tag 三段拼接

判定线（可用参数改）：
    top-1 一致率  >= 98%
    top-3 集合一致 >= 95%
    top-10 平均重叠 >= 95%
    完全逐条一致   >= 90%
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PASS_TOP1 = 0.98
PASS_TOP3 = 0.95
PASS_OVERLAP = 0.95
PASS_EXACT = 0.90


def load_json(p: str) -> dict:
    return json.loads(Path(p).read_text(encoding="utf-8"))


def rerun_python(golden: dict, db: str) -> dict:
    """用当前 Python 引擎重跑黄金集里的输入，产出同结构的"实际结果"。"""
    from src.search import search_grouped

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    out = []
    t_all = time.perf_counter()
    for i, c in enumerate(golden["cases"], 1):
        t0 = time.perf_counter()
        res = search_grouped(conn, c["text"], board=c["board"], top=golden["meta"]["top"])
        ms = (time.perf_counter() - t0) * 1000
        out.append({
            "id": c["id"], "tag": c.get("tag"), "ms": round(ms, 1),
            "parts": [{
                "text": g["text"],
                "hits": [{"q_id": h["q_id"], "score": h["score"], "sim": h["sim"],
                          "lcs": h["lcs"], "exact": h["exact"]} for h in g["hits"]],
            } for g in res["groups"]],
            "flat": [{"q_id": h["q_id"], "score": h["score"]} for h in res["flat"]],
        })
        if i % 50 == 0:
            print("    重跑 %d/%d  %.0fs" % (i, len(golden["cases"]),
                                            time.perf_counter() - t_all), flush=True)
    return {"meta": {"engine": "python", "cases": len(out)}, "cases": out}


def cmp_part(gold: dict, cand: dict) -> dict:
    g_ids = [h["q_id"] for h in gold["hits"]]
    c_ids = [h["q_id"] for h in cand.get("hits", [])]
    g10, c10 = g_ids[:10], c_ids[:10]
    inter = len(set(g10) & set(c10))
    # 分数偏差：只比两边都有的条目
    gmap = {h["q_id"]: h["score"] for h in gold["hits"]}
    cmap = {h["q_id"]: h["score"] for h in cand.get("hits", [])}
    common = set(gmap) & set(cmap)
    dmax = max((abs(gmap[q] - cmap[q]) for q in common), default=0.0)
    # 分数序列：同分并列时 q_id 可以不同（同公司 r1/r2 常逐字重复同一题），
    # 但"第几名拿多少分"必须一致。这一项比 q_id 对齐更能说明算法是否等价。
    gseq = [h["score"] for h in gold["hits"]][:10]
    cseq = [h["score"] for h in cand.get("hits", [])][:10]
    seq = len(gseq) == len(cseq) and all(abs(a - b) < 1e-9 for a, b in zip(gseq, cseq))
    # 支配：每一名的分数都不低于参照。候选集扩大（bm25 截断 6000 -> 全部命中）时，
    # 这是"没有漏候选、且只会更好"的充要判据，比 q_id 对齐严格得多。
    dom = (not gseq) or all(
        cseq[k] >= gseq[k] - 1e-9 for k in range(min(len(gseq), len(cseq))))
    return {
        "dom": dom,
        "seq": seq,
        "exact": g_ids == c_ids,
        "top1": bool(g_ids) == bool(c_ids) and (not g_ids or g_ids[0] == c_ids[0]),
        "top3": set(g_ids[:3]) == set(c_ids[:3]),
        "overlap": inter / max(1, len(g10)),
        "dscore": dmax,
        "gold_ids": g_ids, "cand_ids": c_ids,
        "text_mismatch": gold["text"] != cand.get("text"),
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="黄金集比对")
    ap.add_argument("--golden", default=str(ROOT / "eval" / "golden.json"))
    ap.add_argument("--results", default="", help="待验实现的结果 JSON（同结构）")
    ap.add_argument("--rerun", action="store_true", help="用当前 Python 引擎重跑后比对")
    ap.add_argument("--db", default=str(ROOT / "data" / "queries.db"))
    ap.add_argument("--tag", default="", help="只比某一档（子串匹配 tag）")
    ap.add_argument("--show", type=int, default=8, help="列出最差的几条")
    ap.add_argument("--accept-ties", action="store_true",
                    help="把同分并列视为等价：用\"分数序列一致\"代替 top-1/top-3 判定。"
                         "候选集变大后（自建索引不再用 bm25 截断）必然出现，属预期")
    args = ap.parse_args(argv)

    golden = load_json(args.golden)
    if args.rerun:
        print("用当前 Python 引擎重跑黄金集…", flush=True)
        cand = rerun_python(golden, args.db)
        label = "python(重跑)"
    elif args.results:
        cand = load_json(args.results)
        label = Path(args.results).name
    else:
        ap.error("要么 --rerun，要么 --results")

    # 库指纹校验：换了库，黄金集就不作数了
    fp = golden["meta"]["fingerprint"]
    conn = sqlite3.connect(args.db)
    now = {
        "queries": conn.execute("SELECT COUNT(*) FROM query").fetchone()[0],
        "grams": conn.execute("SELECT COUNT(*) FROM gram_df").fetchone()[0],
        "gram_n": golden["meta"]["fingerprint"]["gram_n"],
    }
    if now["queries"] != fp["queries"] or now["grams"] != fp["grams"]:
        print("!! 库和黄金集不一致：黄金集 query=%d grams=%d，当前 query=%d grams=%d"
              % (fp["queries"], fp["grams"], now["queries"], now["grams"]))
        print("!! 先跑 python -m eval.make_golden 重建黄金集，否则比出来的差异没有意义")
        return 2

    gmap = {c["id"]: c for c in golden["cases"]}
    cmap = {c["id"]: c for c in cand["cases"]}

    rows = []
    for cid, gc in gmap.items():
        if args.tag and args.tag not in (gc.get("tag") or ""):
            continue
        cc = cmap.get(cid)
        if cc is None:
            print("!! 结果里缺 case id=%s" % cid)
            continue
        for i, gp in enumerate(gc["parts"]):
            if i >= len(cc["parts"]):
                rows.append({"id": cid, "tag": gc.get("tag"), "text": gp["text"],
                             "exact": False, "top1": False, "top3": False,
                             "overlap": 0.0, "dscore": 0.0,
                             "gold_ids": [h["q_id"] for h in gp["hits"]],
                             "cand_ids": [], "missing": True})
                continue
            r = cmp_part(gp, cc["parts"][i])
            r.update({"id": cid, "tag": gc.get("tag"), "text": gp["text"]})
            rows.append(r)

    if not rows:
        print("没有可比的 case")
        return 1

    n = len(rows)
    exact = sum(r["exact"] for r in rows) / n
    top1 = sum(r["top1"] for r in rows) / n
    top3 = sum(r["top3"] for r in rows) / n
    overlap = sum(r["overlap"] for r in rows) / n
    seq = sum(r["seq"] for r in rows) / n
    dom = sum(r["dom"] for r in rows) / n
    dmax = max(r["dscore"] for r in rows)
    tmiss = sum(1 for r in rows if r.get("text_mismatch"))

    print()
    print("参照 eval/golden.json（%d 条查询 / %d 个分句），对比 %s"
          % (len(golden["cases"]), n, label))
    print("=" * 68)
    print("  逐条完全一致    %6.1f%%   %d/%d" % (exact * 100, sum(r["exact"] for r in rows), n))
    print("  top-1 一致      %6.1f%%   判定线 %.0f%%" % (top1 * 100, PASS_TOP1 * 100))
    print("  top-3 集合一致  %6.1f%%   判定线 %.0f%%" % (top3 * 100, PASS_TOP3 * 100))
    print("  top-10 平均重叠 %6.1f%%   判定线 %.0f%%" % (overlap * 100, PASS_OVERLAP * 100))
    print("  top-10 分数序列一致 %6.1f%%   （同分并列视为等价）" % (seq * 100))
    print("  每名都不低于参照   %6.1f%%   （超集检索的支配性判据）" % (dom * 100))
    print("  分数最大偏差    %8.4f" % dmax)
    if tmiss:
        print("  !! 有 %d 个分句的切分文本不一致（分句规则变了？）" % tmiss)

    # 分档看：长粘贴和套话往往是最先崩的
    tags: dict[str, list] = {}
    for r in rows:
        tags.setdefault((r["tag"] or "?").split("/")[0], []).append(r)
    if len(tags) > 1:
        print()
        print("  按档看 top-10 重叠：")
        for t in sorted(tags):
            sub = tags[t]
            print("    %-12s %5.1f%%  (n=%d)"
                  % (t, sum(r["overlap"] for r in sub) / len(sub) * 100, len(sub)))

    worst = sorted(rows, key=lambda r: (r["overlap"], r["top1"]))[:args.show]
    if worst and not all(r["exact"] for r in worst[:1]):
        print()
        print("  最差的 %d 条：" % len(worst))
        for r in worst:
            if r["exact"]:
                continue
            print("    case %-4s [%s] 重叠 %.0f%%  %s"
                  % (r["id"], r["tag"], r["overlap"] * 100,
                     (r["text"] or "")[:34].replace("\n", " ")))
            print("        参照 %s" % r["gold_ids"][:5])
            print("        实际 %s" % r["cand_ids"][:5])

    if args.accept_ties:
        # 候选集扩大后 q_id 必然批量变化，改判"不劣 + 重叠下限"
        ok = dom >= PASS_TOP1 and overlap >= PASS_EXACT
    else:
        ok = (top1 >= PASS_TOP1 and top3 >= PASS_TOP3
              and overlap >= PASS_OVERLAP and exact >= PASS_EXACT)
    print()
    print("=" * 68)
    print("  结论：" + ("通过" if ok else "未通过（低于判定线）"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
