"""把不该进检索范围的 query 标记为禁用。

设计要点（2026-10-30 与使用者确认）：
  1. **不删除**。语料是不可再生资产，规则一定会误伤，禁用必须可回滚。
  2. 标注写在独立的 `query_ban` 表，不写在 `query` 上。因为
     `build_db.import_letter()` 重导一份函是先 `DELETE FROM query` 再插，
     挂在 query 上的标注会随重导消失，而且不报错。
     `query.disabled` 只是派生缓存，建库时从 ban 表回填。
  3. 默认 dry-run，只打清单；`--apply` 才动库。

两类规则（都先用 dry-run 看过再 apply）：

  tail  函尾程序性通知段。特征：含"请你们在N个交易日内""审核系统上传"，
        且在函内是最后一条。实测 621 条、100% 新三板、100% 是最后一条、
        500~900 字。它是"发函说明"，不是问题。
  long  深主板/科创板/沪主板/创业板 中 char_len > 500 的条目。使用者肉眼看
        认为基本是"材料或回复混入"。注意：这批里约 3/4 是 (1)(2)(3)
        多小问合并的真问题，ban 掉会损失参考，所以单独给 kind='long'，
        将来想恢复时 `--undo long` 一条命令挑回来。

用法：
  python -m scripts.mark_banned                 # 出清单（不改库）
  python -m scripts.mark_banned --apply         # 真正写库
  python -m scripts.mark_banned --undo long     # 按类别恢复
  python -m scripts.mark_banned --undo all
  python -m scripts.mark_banned --keep 科创板-xxx-r1-0007   # 白名单，不进清单
"""

from __future__ import annotations

import argparse
import csv
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DB = ROOT / "data" / "queries.db"

# 函尾"发函说明"的标志句：都是对收函方（发行人/中介）的操作指令，
# 不是要它回答的问题。单独的"个交易日内"会误伤正文，所以要求"请你们在"。
TAIL_SIG = re.compile(r"请你们在\s*\d+\s*个交易日内|审核系统上传")

# 使用者指定：这四个板块 char_len > 500 一律禁用（北交所与新三板不受此规则影响）
LONG_BOARDS = ("深主板", "科创板", "沪主板", "创业板")
LONG_MIN = 500


def ensure_schema(conn: sqlite3.Connection) -> None:
    """补上 disabled 列与 query_ban 表（老库没有）。可重复执行。"""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(query)")}
    if "disabled" not in cols:
        conn.execute("ALTER TABLE query ADD COLUMN disabled INTEGER NOT NULL DEFAULT 0")
        print("  + query.disabled 列已添加")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS query_ban (
          q_id       TEXT PRIMARY KEY,
          kind       TEXT NOT NULL,
          reason     TEXT,
          created_at TEXT NOT NULL,
          source     TEXT
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ban_kind ON query_ban(kind)")


def scan_tail(conn: sqlite3.Connection) -> list[dict]:
    """函尾程序性通知段。判据是"标志句 + 函内最后一条"，不看长度。"""
    last_seq = {
        lid: mx for lid, mx in conn.execute(
            "SELECT letter_id, MAX(seq_in_letter) FROM query GROUP BY letter_id")
    }
    out = []
    for r in conn.execute(
            "SELECT q_id, letter_id, board, char_len, seq_in_letter, question, raw_text "
            "FROM query WHERE disabled = 0"):
        if not TAIL_SIG.search(r["raw_text"]):
            continue
        if r["seq_in_letter"] != last_seq.get(r["letter_id"]):
            continue
        out.append({
            "q_id": r["q_id"], "kind": "tail", "board": r["board"],
            "char_len": r["char_len"], "question": r["question"] or "",
            "head": r["raw_text"][:80],
            "reason": "函尾发函说明（要求 N 个交易日内上传回复），不是问题",
        })
    return out


def scan_long(conn: sqlite3.Connection) -> list[dict]:
    """四板块超长条目。使用者判定基本是材料/回复混入。"""
    qs = ",".join("?" * len(LONG_BOARDS))
    out = []
    for r in conn.execute(
            "SELECT q_id, board, char_len, question, raw_text FROM query "
            "WHERE disabled = 0 AND char_len > ? AND board IN (" + qs + ") "
            "ORDER BY char_len DESC", (LONG_MIN, *LONG_BOARDS)):
        # 粗略标一下"像多小问合并"还是"像混入"，方便人工过目；
        # 不影响是否禁用（按使用者口径一律禁用）
        t = r["raw_text"].lstrip()
        multi = bool(re.match(r"[（(]\s*[1１]|问题\s*\d+|[一二三四五六七八九]、", t))
        out.append({
            "q_id": r["q_id"], "kind": "long", "board": r["board"],
            "char_len": r["char_len"], "question": r["question"] or "",
            "head": r["raw_text"][:80],
            "reason": ("超长（%d 字），疑似多小问合并" % r["char_len"]) if multi
                      else ("超长（%d 字），疑似材料/回复混入" % r["char_len"]),
        })
    return out


def apply_ban(conn: sqlite3.Connection, rows: list[dict], source: str) -> int:
    now = datetime.now().isoformat(timespec="seconds")
    n = 0
    for r in rows:
        cur = conn.execute(
            "INSERT OR IGNORE INTO query_ban(q_id,kind,reason,created_at,source) "
            "VALUES(?,?,?,?,?)", (r["q_id"], r["kind"], r["reason"], now, source))
        n += cur.rowcount
    # 派生列回填：只动 ban 表覆盖到的，避免全表扫
    conn.execute("UPDATE query SET disabled = 1 "
                 "WHERE q_id IN (SELECT q_id FROM query_ban)")
    return n


def undo(conn: sqlite3.Connection, kind: str) -> tuple[int, int]:
    if kind == "all":
        cur = conn.execute("DELETE FROM query_ban")
    else:
        cur = conn.execute("DELETE FROM query_ban WHERE kind = ?", (kind,))
    n = cur.rowcount
    conn.execute("UPDATE query SET disabled = 0 WHERE q_id NOT IN (SELECT q_id FROM query_ban)")
    return n, conn.execute("SELECT COUNT(*) FROM query WHERE disabled = 1").fetchone()[0]


def dump_csv(rows: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # utf-8-sig：Excel 双击打开不乱码（和导出指引同一个理由）
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["q_id", "kind", "board", "char_len",
                                          "question", "reason", "head"])
        w.writeheader()
        w.writerows(rows)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真正写库（默认只打印）")
    ap.add_argument("--undo", choices=["tail", "long", "manual", "all"], help="恢复某一类禁用")
    ap.add_argument("--keep", nargs="*", default=[], help="白名单 q_id，不进清单")
    ap.add_argument("--db", default=str(DB))
    ap.add_argument("--csv", default=str(ROOT / "out" / "ban_list.csv"))
    a = ap.parse_args()

    conn = sqlite3.connect(a.db)
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)

    if a.undo:
        n, left = undo(conn, a.undo)
        conn.commit()
        print("已恢复 %d 条（kind=%s），仍禁用 %d 条" % (n, a.undo, left))
        return

    rows = scan_tail(conn) + scan_long(conn)
    keep = set(a.keep)
    if keep:
        rows = [r for r in rows if r["q_id"] not in keep]
        print("白名单跳过 %d 条" % len(keep))

    by_kind: dict[str, list[dict]] = {}
    for r in rows:
        by_kind.setdefault(r["kind"], []).append(r)

    total = conn.execute("SELECT COUNT(*) FROM query").fetchone()[0]
    print("\n命中 %d 条（全库 %d 条，占 %.2f%%）" % (len(rows), total, 100 * len(rows) / total))
    for k, v in by_kind.items():
        boards: dict[str, int] = {}
        for r in v:
            boards[r["board"]] = boards.get(r["board"], 0) + 1
        print("  %-5s %5d 条   %s" % (k, len(v), "  ".join(
            "%s=%d" % (b, n) for b, n in sorted(boards.items(), key=lambda x: -x[1]))))

    print("\n--- 抽样（每类 3 条）---")
    for k, v in by_kind.items():
        print("【%s】" % k)
        step = max(1, len(v) // 3)
        for r in v[::step][:3]:
            print("  [%s] %s %d字 | %s" % (r["q_id"], r["board"], r["char_len"], r["head"][:60]))

    if not a.apply:
        dump_csv(rows, Path(a.csv))
        print("\n清单已写到 %s（Excel 可直接打开）" % a.csv)
        print("确认无误后加 --apply 写库。恢复用 --undo tail / --undo long / --undo all")
        return

    n = apply_ban(conn, rows, source="rule:%s-v1" % "+".join(sorted(by_kind)))
    conn.commit()
    banned = conn.execute("SELECT COUNT(*) FROM query WHERE disabled = 1").fetchone()[0]
    print("\n已写入 %d 条（新增），库里累计禁用 %d 条" % (n, banned))
    print("下一步：重建 Rust 索引（库变了不重建会被启动校验拦下）")


if __name__ == "__main__":
    main()
