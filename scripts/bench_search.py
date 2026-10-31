# -*- coding: utf-8 -*-
"""检索耗时分解：把 search_one 的每个阶段分开计时，给"换 Rust"提供基线。

不改 src/search.py，只调用它内部的函数，按同样的顺序走一遍。
"""
from __future__ import annotations

import sqlite3
import statistics
import sys
import time

sys.path.insert(0, ".")

from src.normalize import norm_core, norm_loose, norm_strict
from src.search import (FTS_POOL_CAP, LCS_CHAR_BUDGET, TOP_FOR_LCS,
                        QuerySide, fts_terms, gram_norm, lcs_span, load_idf, ngrams,
                        query_clauses, similarity_side, _runs_to_spans, common_runs)
from src.normalize import norm_core_map

DB = "data/queries.db"

QUERIES = [
    ("短(30字)", "说明研发人员认定标准及划分依据"),
    ("中位(74字)", "结合报告期各期存货库龄结构、在手订单及期后销售情况，说明存货跌价准备计提是否充分、计提方法是否符合企业会计准则的规定"),
    ("长粘贴(约300字)",
     "1.关于收入确认。请发行人结合合同条款、业务实质及风险报酬转移时点，说明在经销商模式下收入确认的具体方法、时点及依据，"
     "是否符合企业会计准则的规定；说明报告期内是否存在跨期确认收入的情形，相关内部控制是否健全有效，"
     "请保荐机构、申报会计师核查并发表明确意见。2.关于毛利率。请发行人结合产品结构、单价及单位成本变动，"
     "量化分析报告期各期毛利率与同行业可比公司的差异原因及合理性，说明毛利率变动趋势是否与可比公司一致。"),
]


def timed_search_one(conn, question, board=None):
    t = {}
    t0 = time.perf_counter()
    q_core = norm_core(question)
    q_loose = norm_loose(question)
    q_strict = norm_strict(question)
    q_clauses = query_clauses(question)
    idf = load_idf(conn)
    t["归一化+IDF"] = time.perf_counter() - t0

    where, params = [], []
    if board:
        where.append("board = ?")
        params.append(board)
    where.append("is_boilerplate = 0")

    t0 = time.perf_counter()
    terms = fts_terms(conn, q_loose)
    t["粗筛建词(FTS串)"] = time.perf_counter() - t0

    if terms:
        sub = "SELECT q_id FROM query_fts WHERE query_fts MATCH ?"
        if FTS_POOL_CAP:
            sub += " ORDER BY bm25(query_fts) LIMIT " + str(FTS_POOL_CAP)
        where.append("q_id IN (" + sub + ")")
        params.append(terms)

    clause = ("WHERE " + " AND ".join(where)) if where else ""
    t0 = time.perf_counter()
    rows = conn.execute("SELECT * FROM query " + clause, params).fetchall()
    t["SQL取候选行"] = time.perf_counter() - t0
    t["候选条数"] = len(rows)

    q_grams = ngrams(q_core)
    clause_sides = [QuerySide(ngrams(c), idf) for c in q_clauses]
    q_side = QuerySide(q_grams, idf)
    use_clauses = len(clause_sides) > 1

    t0 = time.perf_counter()
    scored = []
    for r in rows:
        target = r["norm_core"] or r["norm_loose"]
        tg = ngrams(target)
        nb = gram_norm(tg, idf)
        sim = similarity_side(q_side, tg, idf, nb)
        if use_clauses:
            best = max(similarity_side(cs, tg, idf, nb) for cs in clause_sides)
            best *= 0.9
            if best > sim:
                sim = best
        exact = bool(q_strict) and q_strict in r["norm_strict"]
        scored.append([sim, exact, r])
    t["打分循环"] = time.perf_counter() - t0

    t0 = time.perf_counter()
    scored.sort(key=lambda x: (-x[0], -x[1]))
    scored = scored[:max(40, min(TOP_FOR_LCS, LCS_CHAR_BUDGET // max(1, len(q_core) ** 2)))]
    t["排序+截断"] = time.perf_counter() - t0
    t["进LCS条数"] = len(scored)

    t0 = time.perf_counter()
    for sim, exact, r in scored:
        target = r["norm_core"] or r["norm_loose"]
        lcs_span(q_core, target)
    t["LCS"] = time.perf_counter() - t0

    t0 = time.perf_counter()
    for sim, exact, r in scored[:10]:
        core, idx = norm_core_map(r["raw_text"])
        if core:
            _runs_to_spans(common_runs(q_core, core), idx, 1)
    t["高亮(前10)"] = time.perf_counter() - t0

    t0 = time.perf_counter()
    r2 = conn.execute("SELECT norm_core FROM query WHERE 0").fetchall()
    t["(空SQL)"] = time.perf_counter() - t0

    t["总耗时"] = sum(v for k, v in t.items() if isinstance(v, float))
    return t


def main():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA cache_size = -65536")   # 64MB，跟 server.py 一致的话可改

    # 预热：先跑一遍，让页缓存热起来
    timed_search_one(conn, QUERIES[0][1])

    for label, q in QUERIES:
        runs = []
        for _ in range(3):
            t = timed_search_one(conn, q)
            runs.append(t)
        # 取中位数那一轮
        med = statistics.median(r["总耗时"] for r in runs)
        pick = min(runs, key=lambda r: abs(r["总耗时"] - med))
        print("=" * 78)
        print("%s  字符数 %d  候选 %d 条  进LCS %d 条  合计 %.0f ms"
              % (label, len(q), pick["候选条数"], pick["进LCS条数"], pick["总耗时"] * 1000))
        for k, v in pick.items():
            if k in ("候选条数", "进LCS条数"):
                continue
            pct = v / pick["总耗时"] * 100
            bar = "#" * int(pct / 2)
            print("    %-14s %7.1f ms  %5.1f%%  %s" % (k, v * 1000, pct, bar))

    print("=" * 78)
    head = ("估算：候选 %d 条 × 平均 90 字 ≈ 每条约 87 个 gram，"
            "打分循环是 O(候选 × gram 数) 的纯 Python 循环" % FTS_POOL_CAP)
    print(head)


if __name__ == "__main__":
    main()
