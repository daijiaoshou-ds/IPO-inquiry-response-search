# -*- coding: utf-8 -*-
"""把库里一条 query 按「；。」再切成多段（2026-10-30）。

背景：编号（（1）/（一）/1、）只切到"一层小问"，但一层小问里常常还并着 2~5 个
并列问句，监管是用**分号**隔开的：

    "……转让所持股权的原因；胡敏、王力受让股权的资金来源，胡敏时任单位对其
     对外投资有无限制，锐能科技向杨璐偿还出资款的过程及资金流向"

一个分句就是一个完整问句。切成两段后，"这句话别家怎么答的"才对得准：
库侧一条 300 字里只有 40 字与提问相关，余弦被稀释；切完那 40 字才浮上来。

实测（50 个特异句探针，GT 用全库逐字包含判定）：
    hit@5 0.960 → 0.960（不掉）    MRR 0.830 → 0.850（升）
所以这一步不是拿召回换粒度。

科创板/创业板的 PDF 已从磁盘清理，没法重抽，只能**原地拆**：改父条目、加子条目，
原文存在 `query_clause_split` 台账里，`--undo` 可以整条还原。

用法：
    python -m scripts.split_clauses            # 只看会切哪些（不动库）
    python -m scripts.split_clauses --apply    # 真正切
    python -m scripts.split_clauses --undo     # 全部还原

实现上的两个约束（都踩过）：
  ① 不能 fetchall：27.9 万行连 raw_text 一次读进内存要 1.5 GB，会被系统 SIGTERM。
  ② 不能"边 SELECT 同一张表边 UPDATE"：游标会看到改过的行，只能先收 q_id 再逐条取。
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.extract import split_clauses                       # noqa: E402
from src.normalize import is_boilerplate, norm_core, norm_loose, norm_strict  # noqa: E402

DB = ROOT / "data" / "queries.db"
LEDGER = "query_clause_split"


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS %s ("
        " parent_q_id TEXT PRIMARY KEY,"
        " parent_text TEXT NOT NULL,"
        " n_seg       INTEGER NOT NULL,"
        " created_at  TEXT)" % LEDGER)
    conn.commit()


def candidates(conn: sqlite3.Connection, min_seg: int, lo: int, hi: int):
    """流式产出可切的 q_id。"""
    cur = conn.execute(
        "SELECT q_id, raw_text, disabled FROM query"
        " WHERE char_len >= ? AND char_len <= ?", (lo, hi))
    for r in cur:
        if r["disabled"]:               # 已停用的条目不再拆，白增行
            continue
        if len(split_clauses(r["raw_text"])) >= min_seg:
            yield r["q_id"]


def apply_one(conn: sqlite3.Connection, qid: str, segs: list[str], now: str) -> int:
    """切一条，返回新增子条数。"""
    row = conn.execute("SELECT * FROM query WHERE q_id=?", (qid,)).fetchone()
    conn.execute(
        "INSERT OR REPLACE INTO %s(parent_q_id,parent_text,n_seg,created_at)"
        " VALUES(?,?,?,?)" % LEDGER, (qid, row["raw_text"], len(segs), now))

    base_seq = row["seq_in_question"] * 100       # 留间隔，避免与同题邻条撞号
    base_item = row["item_no"] or ""
    added = 0
    for i, seg in enumerate(segs):
        new_id = qid if i == 0 else "%s-c%d" % (qid, i + 1)
        item_no = (base_item + "." + str(i + 1)) if base_item else ""
        boil = 1 if is_boilerplate(seg) else 0
        if i == 0:
            conn.execute(
                "UPDATE query SET raw_text=?, norm_strict=?, norm_loose=?, norm_core=?,"
                " char_len=?, is_boilerplate=?, item_no=?, seq_in_question=?"
                " WHERE q_id=?",
                (seg, norm_strict(seg), norm_loose(seg), norm_core(seg), len(seg),
                 boil, item_no, base_seq, new_id))
        else:
            conn.execute(
                "INSERT INTO query(q_id,letter_id,question_id,company,board,round,"
                "chapter,chapter_std,question_no,question,sub_question,asker,item_no,"
                "seq_in_letter,seq_in_question,page_start,page_end,raw_text,"
                "norm_strict,norm_loose,norm_core,char_len,is_boilerplate,disabled) "
                "SELECT ?,letter_id,question_id,company,board,round,chapter,chapter_std,"
                "question_no,question,sub_question,asker,?,seq_in_letter,?,page_start,"
                "page_end,?,?,?,?,?,?,0 FROM query WHERE q_id=?",
                (new_id, item_no, base_seq + i, seg, norm_strict(seg), norm_loose(seg),
                 norm_core(seg), len(seg), boil, qid))
            added += 1
    return added


def rebuild_fts(conn: sqlite3.Connection) -> int:
    """整表重建。逐条 DELETE ... WHERE q_id=? 在 FTS5（q_id UNINDEXED）上是全表扫，
    5.4 万条要扫几亿行；重建一次只要 20 秒。"""
    conn.execute("DROP TABLE IF EXISTS query_fts")
    conn.execute("CREATE VIRTUAL TABLE query_fts USING fts5("
                 "q_id UNINDEXED, norm_loose, tokenize = 'trigram')")
    rows = conn.execute("SELECT q_id, norm_loose FROM query").fetchall()
    conn.executemany("INSERT INTO query_fts(q_id,norm_loose) VALUES(?,?)", rows)
    conn.commit()
    return len(rows)


def renumber(conn: sqlite3.Connection, qids: list[str]) -> None:
    """切完按题重排 seq_in_question：siblings 和导出的小问索引都按它排，
    留着 ×100 的偏移会让同题后面的条目排到前面去。"""
    for i in range(0, len(qids), 400):
        chunk = qids[i:i + 400]
        ph = ",".join("?" * len(chunk))
        conn.execute(
            "WITH ranked AS (SELECT q_id, ROW_NUMBER() OVER ("
            "PARTITION BY question_id ORDER BY seq_in_question, q_id) rn "
            "FROM query WHERE question_id IN (" + ph + ")) "
            "UPDATE query SET seq_in_question = "
            "(SELECT rn FROM ranked WHERE ranked.q_id = query.q_id) "
            "WHERE question_id IN (" + ph + ")", chunk + chunk)
    conn.commit()


def undo(conn: sqlite3.Connection) -> tuple[int, int]:
    qids = [r[0] for r in conn.execute("SELECT parent_q_id FROM %s" % LEDGER)]
    for pq in qids:
        for (cq,) in conn.execute("SELECT q_id FROM query WHERE q_id LIKE ?",
                                  (pq + "-c%",)).fetchall():
            conn.execute("DELETE FROM query WHERE q_id=?", (cq,))
        t = conn.execute("SELECT parent_text FROM %s WHERE parent_q_id=?" % LEDGER,
                         (pq,)).fetchone()[0]
        conn.execute(
            "UPDATE query SET raw_text=?, norm_strict=?, norm_loose=?, norm_core=?,"
            " char_len=?, is_boilerplate=? WHERE q_id=?",
            (t, norm_strict(t), norm_loose(t), norm_core(t), len(t),
             1 if is_boilerplate(t) else 0, pq))
    conn.execute("DELETE FROM %s" % LEDGER)
    conn.commit()
    rebuild_fts(conn)
    return len(qids), conn.execute("SELECT COUNT(*) FROM query").fetchone()[0]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--undo", action="store_true")
    ap.add_argument("--min-seg", type=int, default=2)
    ap.add_argument("--lo", type=int, default=0)
    ap.add_argument("--hi", type=int, default=10 ** 6)
    ap.add_argument("--show", type=int, default=8, help="dry-run 时打印几条样本")
    ap.add_argument("--db", default=str(DB))
    a = ap.parse_args()

    conn = sqlite3.connect(a.db)
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)

    if a.undo:
        n, total = undo(conn)
        print("已还原 %d 条，当前总数 %d 条" % (n, total))
        print("记得重建：gram_df（IDF）与 Rust 索引")
        return

    total0 = conn.execute("SELECT COUNT(*) FROM query").fetchone()[0]

    if not a.apply:
        n = segs = 0
        for qid in candidates(conn, a.min_seg, a.lo, a.hi):
            row = conn.execute("SELECT char_len, raw_text FROM query WHERE q_id=?",
                               (qid,)).fetchone()
            parts = split_clauses(row["raw_text"])
            if n < a.show:
                print("\n[%s] %d字 → %d段" % (qid, row["char_len"], len(parts)))
                for i, s in enumerate(parts, 1):
                    print("   %d. %3d字 %s" % (i, len(s), s[:70]))
            n += 1
            segs += len(parts)
        print("\n可切 %d 条 → %d 段（净增 %d），总数 %d → %d" % (
            n, segs, segs - n, total0, total0 + segs - n))
        print("（dry-run，未改动。加 --apply 执行）")
        return

    now = datetime.now().isoformat(timespec="seconds")
    ids = list(candidates(conn, a.min_seg, a.lo, a.hi))
    print("待切 %d 条" % len(ids))
    touched: set[str] = set()
    add_n = 0
    cb = ca = 0
    for k, qid in enumerate(ids, 1):
        row = conn.execute("SELECT question_id, raw_text FROM query WHERE q_id=?",
                           (qid,)).fetchone()
        parts = split_clauses(row["raw_text"])
        cb += len(row["raw_text"])
        ca += sum(len(s) for s in parts)
        add_n += apply_one(conn, qid, parts, now)
        touched.add(row["question_id"])
        if k % 2000 == 0:
            conn.commit()
            print("  %d/%d" % (k, len(ids)))
    conn.commit()
    n_fts = rebuild_fts(conn)
    renumber(conn, sorted(touched))

    print("切分 %d 条，新增子条 %d，重排 %d 道题，FTS 重建 %d 行" % (
        len(ids), add_n, len(touched), n_fts))
    print("字数 %d → %d（差 %d 是去掉的分隔标点）" % (cb, ca, cb - ca))
    print("当前总数 %d（原 %d）" % (
        conn.execute("SELECT COUNT(*) FROM query").fetchone()[0], total0))
    print("接着必须重建：gram_df（IDF）与 search-core --build-index")


if __name__ == "__main__":
    main()
