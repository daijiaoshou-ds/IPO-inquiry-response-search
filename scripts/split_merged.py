"""把"多个问题被合并成一条"的 query 拆开。

背景（2026-10-30）：库里有一批 100~900 字的 query，其实是若干小问被并成了一条——
不是材料混入，也不是回复混入，是**拆分规则没覆盖某些编号体例**。

根因在 `src/extract.py:178`：

    ITEM_RE = re.compile(r"(①|②|…|（\\s*\\d{1,2}\\s*）)")

只认全角 `（1）`。而泰凌微电子、贝克制药、华曙高科技用**半角** `(1)`，
国科恒泰医疗科技用**中文** `（一）`，于是整段没被切开。科创板 PDF 已从磁盘清理，
没法重抽，所以只能原地拆。

判据不是"长度"，是**编号序列**：
  - 找从 1 开始、逐段 +1 的连续编号（编号必须≥2 个）
  - 编号必须落在句首/标点之后，前面是字母的不算（`T1、T2` 不是编号）
  - 数字后面紧跟数字的不算（`11145.44`、`5.5`）
  - 统计 raw_text，不统计 norm_core（后者已归一化，数字全没了）

用法：
  python -m scripts.split_merged              # 只看会拆哪些（不动库）
  python -m scripts.split_merged --apply      # 真正拆
  python -m scripts.split_merged --undo       # 全部还原
  python -m scripts.split_merged --min-seg 3  # 只拆 ≥3 段的（更保守）
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.normalize import is_boilerplate, norm_core, norm_loose, norm_strict  # noqa: E402

DB = ROOT / "data" / "queries.db"

CN = "一二三四五六七八九十"
CNMAP = {ch: i + 1 for i, ch in enumerate(CN)}
BOUND = r'(?:^|[；;。.，,\n：:\s])'

# 按"最外层优先"排。一份函里（一）套 (1) 的情况很常见，只切最外那一层，
# 否则一层嵌套会被切两遍，出来一堆碎片。
LAYERS = [
    ("（一）", re.compile(r'[（(]\s*([' + CN + r']{1,2})\s*[）)]')),
    # [（(] 同时覆盖全角（）与英文 ()，实测库里两种都有
    ("（1）", re.compile(r'[（(]\s*([1-9１-９])\s*[）)]')),
    # 1）2）这种没有左括号的，前面不能是括号，否则那是 (1) 的右半边
    ("1）",  re.compile(r'(?<![\dA-Za-z.)）（(])([1-9１-９])\s*[）)](?!\d)')),
    ("1、",  re.compile(BOUND + r'\s*([1-9１-９])\s*[、](?!\d)')),
    # 点号编号在正文里太容易撞（"5.5 元""第 3.1 条"），要求 ≥3 段才认
    ("1.",   re.compile(BOUND + r'\s*([1-9１-９])\s*[.．](?!\d)')),
]

# 拆出来的段还得"像问题"。同一套编号体例，既可能是真小问，也可能是回复里
# "1、查阅了…2、取得了…"或合同条款"1）卖方违反…"。实测 1、/1）那 29 条里
# 一半是回复/正文，所以这道判据必须有，不能只看编号。
Q_RE = re.compile(r'是否|说明|披露|分析|列示|论证|补充|请[^，。；]{0,8}(?:说明|披露|核查|发表)')
Q_RATIO = 0.6      # 至少六成段像问题才拆


def num_of(s: str) -> int:
    s = s.strip()
    if s in CNMAP:
        return CNMAP[s]
    if s == "十":
        return 10
    if len(s) == 2 and s[0] == "十":          # 十一、十二
        return 10 + CNMAP.get(s[1], 0)
    return int(s.translate(str.maketrans("１２３４５６７８９", "123456789")))


def find_seq(text: str) -> tuple[str, list[tuple[int, int, int]]]:
    """找从 1 开始、逐段 +1 的连续编号序列。返回 (体例名, [(start, end, num)])。"""
    for name, rx in LAYERS:
        need_min = 3 if name == "1." else 2
        cands = []
        for m in rx.finditer(text):
            n = num_of(m.group(1))
            if n <= 12:
                cands.append((m.start(), m.end(), n))
        best: list[tuple[int, int, int]] = []
        for i, (s, _e, n) in enumerate(cands):
            if n != 1:
                continue
            seq = [(s, _e, n)]
            for s2, e2, n2 in cands[i + 1:]:
                if n2 == seq[-1][2] + 1:
                    seq.append((s2, e2, n2))
            if len(seq) > len(best):
                best = seq
        if len(best) >= need_min:
            return name, best
    return "", []


def split_text(text: str) -> list[tuple[str, str]]:
    """切成 [(item_no, 段文本)]。第一段若在编号之前还有实质内容，单独作一条。"""
    name, seq = find_seq(text)
    if not seq:
        return []
    cuts = [s for s, _e, _n in seq] + [len(text)]
    out: list[tuple[str, str]] = []
    head = text[:cuts[0]].strip(" ；;。，,")
    if len(head) >= 8:                     # 编号前有题干，保留
        out.append(("", head))
    for i in range(len(cuts) - 1):
        seg = text[cuts[i]:cuts[i + 1]].strip(" ；;。，,\n：:")
        if len(seg) >= 8:
            out.append((str(seq[i][2]), seg))
    # 像问题的段不到六成 -> 这不是"多小问合并"，是回复/合同/表格，不拆
    if out:
        good = sum(1 for _no, t in out if Q_RE.search(t))
        if good / len(out) < Q_RATIO:
            return []
    return out


def ensure_schema(conn: sqlite3.Connection) -> None:
    """拆分台账：parent 原文留档，--undo 才能整条还原。"""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS query_split (
          parent_q_id TEXT PRIMARY KEY,
          parent_text TEXT NOT NULL,      -- 原 raw_text，undo 时原样写回
          n_seg       INTEGER NOT NULL,
          style       TEXT,
          was_banned  INTEGER NOT NULL DEFAULT 0,
          created_at  TEXT NOT NULL
        )""")


def scan(conn: sqlite3.Connection, min_seg: int, lo: int, hi: int) -> list[dict]:
    rows = conn.execute(
        "SELECT q_id, letter_id, question_id, question_no, seq_in_question, seq_in_letter,"
        "       char_len, raw_text, disabled, board "
        "FROM query WHERE char_len BETWEEN ? AND ?", (lo, hi)).fetchall()
    out = []
    for r in rows:
        if conn.execute("SELECT 1 FROM query_split WHERE parent_q_id=?",
                        (r["q_id"],)).fetchone():
            continue                        # 已经拆过
        segs = split_text(r["raw_text"])
        if len(segs) < min_seg:
            continue
        out.append({"row": r, "segs": segs,
                    "style": find_seq(r["raw_text"])[0]})
    return out


def apply(conn: sqlite3.Connection, items: list[dict]) -> dict:
    now = datetime.now().isoformat(timespec="seconds")
    stat = {"parents": 0, "children": 0, "chars_before": 0, "chars_after": 0,
            "unbanned": 0, "questions": 0}
    touched: set[str] = set()
    for it in items:
        r, segs = it["row"], it["segs"]
        pq = r["q_id"]
        # 未拆前总字数（用来核对拆分没吞掉内容）
        stat["chars_before"] += len(r["raw_text"])
        stat["chars_after"] += sum(len(s[1]) for s in segs)

        was_banned = 1 if r["disabled"] else 0
        conn.execute(
            "INSERT OR REPLACE INTO query_split(parent_q_id,parent_text,n_seg,style,"
            "was_banned,created_at) VALUES(?,?,?,?,?,?)",
            (pq, r["raw_text"], len(segs), it["style"], was_banned, now))
        # 合并题不是废料：拆分即解禁，同时把 long 类的 ban 记录摘掉，
        # 否则 --undo long 会把已经拆好的父条目又禁回去。
        conn.execute("DELETE FROM query_ban WHERE q_id=?", (pq,))

        new_seq = r["seq_in_question"] * 100    # 留出段内间隔，避免与同题邻条撞号
        for i, (item_no, seg) in enumerate(segs):
            qid = pq if i == 0 else "%s-%d" % (pq, i + 1)
            conn.execute("DELETE FROM query_fts WHERE q_id=?", (qid,))
            if i == 0:
                conn.execute(
                    "UPDATE query SET raw_text=?, norm_strict=?, norm_loose=?, "
                    "norm_core=?, char_len=?, is_boilerplate=?, item_no=?, "
                    "seq_in_question=?, disabled=0 WHERE q_id=?",
                    (seg, norm_strict(seg), norm_loose(seg), norm_core(seg),
                     len(seg), 1 if is_boilerplate(seg) else 0,
                     item_no, new_seq + i, qid))
            else:
                conn.execute(
                    "INSERT INTO query(q_id,letter_id,question_id,company,board,round,"
                    "chapter,chapter_std,question_no,question,sub_question,asker,item_no,"
                    "seq_in_letter,seq_in_question,page_start,page_end,raw_text,"
                    "norm_strict,norm_loose,norm_core,char_len,is_boilerplate,disabled) "
                    "SELECT ?,letter_id,question_id,company,board,round,chapter,chapter_std,"
                    "question_no,question,sub_question,asker,?,seq_in_letter,?,page_start,"
                    "page_end,?,?,?,?,?,?,0 FROM query WHERE q_id=?",
                    (qid, item_no, new_seq + i, seg, norm_strict(seg),
                     norm_loose(seg), norm_core(seg), len(seg),
                     1 if is_boilerplate(seg) else 0, pq))
                stat["children"] += 1
            conn.execute("INSERT INTO query_fts(q_id,norm_loose) VALUES(?,?)",
                         (qid, norm_loose(seg)))
        stat["parents"] += 1
        stat["unbanned"] += was_banned
        touched.add(r["question_id"])

    # 拆完必须按题重排 seq_in_question。上面用 ×100 只是为了"插在中间不撞号"，
    # 但 siblings 和导出的"小问索引"都按这个字段排，留着 100、101 的偏移会让
    # 同一题里后面的条目排到前面去。
    qids = sorted(touched)
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
    stat["questions"] = len(qids)
    return stat


def undo(conn: sqlite3.Connection) -> tuple[int, int]:
    rows = conn.execute("SELECT * FROM query_split").fetchall()
    for r in rows:
        pq = r["parent_q_id"]
        for i in range(2, r["n_seg"] + 1):
            cq = "%s-%d" % (pq, i)
            conn.execute("DELETE FROM query_fts WHERE q_id=?", (cq,))
            conn.execute("DELETE FROM query WHERE q_id=?", (cq,))
        t = r["parent_text"]
        conn.execute(
            "UPDATE query SET raw_text=?, norm_strict=?, norm_loose=?, norm_core=?, "
            "char_len=?, is_boilerplate=?, item_no=NULL, disabled=? WHERE q_id=?",
            (t, norm_strict(t), norm_loose(t), norm_core(t), len(t),
             1 if is_boilerplate(t) else 0, r["was_banned"], pq))
        conn.execute("DELETE FROM query_fts WHERE q_id=?", (pq,))
        conn.execute("INSERT INTO query_fts(q_id,norm_loose) VALUES(?,?)",
                     (pq, norm_loose(t)))
        if r["was_banned"]:
            conn.execute("INSERT OR IGNORE INTO query_ban(q_id,kind,reason,created_at,source)"
                         " VALUES(?,?,?,?,?)",
                         (pq, "long", "超长，原判为混入；undo 时恢复", r["created_at"], "undo:split"))
    conn.execute("DELETE FROM query_split")
    conn.commit()
    return len(rows), conn.execute("SELECT COUNT(*) FROM query").fetchone()[0]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--undo", action="store_true")
    ap.add_argument("--min-seg", type=int, default=2, help="至少切出几段才拆（默认 2）")
    ap.add_argument("--lo", type=int, default=100)
    ap.add_argument("--hi", type=int, default=900)
    ap.add_argument("--db", default=str(DB))
    a = ap.parse_args()

    conn = sqlite3.connect(a.db)
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)

    if a.undo:
        n, total = undo(conn)
        print("已还原 %d 条合并 query，当前总数 %d 条" % (n, total))
        print("记得重建：gram_df（IDF）与 Rust 索引")
        return

    items = scan(conn, a.min_seg, a.lo, a.hi)
    if not items:
        print("没有可拆的条目")
        return

    total_seg = sum(len(i["segs"]) for i in items)
    banned = sum(1 for i in items if i["row"]["disabled"])
    print("可拆 %d 条 → 拆成 %d 条（净增 %d）" % (len(items), total_seg, total_seg - len(items)))
    print("其中当前被禁用的 %d 条（拆分后解禁）" % banned)

    from collections import Counter
    print("板块：", dict(Counter(i["row"]["board"] for i in items)))
    print("体例：", dict(Counter(i["style"] for i in items)))

    print("\n--- 抽样 6 条 ---")
    for it in items[::max(1, len(items) // 6)][:6]:
        r = it["row"]
        print("\n[%s] %d字 → %d 段（%s）" % (r["q_id"], r["char_len"],
                                             len(it["segs"]), it["style"]))
        for no, seg in it["segs"][:3]:
            print("    %-4s %3d字 %s" % (no or "题干", len(seg), seg[:56]))

    if not a.apply:
        print("\n（dry-run，未改动数据库。加 --apply 执行，--undo 可整条还原）")
        return

    stat = apply(conn, items)
    print("\n完成：%d 条父条目已拆，新增 %d 条子条目" % (stat["parents"], stat["children"]))
    print("字数核对：拆前 %d 字 → 拆后 %d 字（差 %d，应接近 0，差额是去掉的分隔标点）"
          % (stat["chars_before"], stat["chars_after"],
             stat["chars_before"] - stat["chars_after"]))
    print("解禁 %d 条 | 重排 %d 道题的小问序号" % (stat["unbanned"], stat["questions"]))
    print("下一步：重建 gram_df 与 Rust 索引")


if __name__ == "__main__":
    main()
