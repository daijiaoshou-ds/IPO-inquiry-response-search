# -*- coding: utf-8 -*-
"""扫描 问询函/ 目录 -> 抽取 -> 写入 SQLite 并建索引。

用法：
    .venv\\Scripts\\python.exe -m src.build_db 问询函 --db data/queries.db
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime
from pathlib import Path

from .extract import Letter, scan_dir, scan_letter
from .normalize import norm_core
from .search import GRAM_N

SCHEMA = Path(__file__).with_name("schema.sql")

# 各家公司给章节编的号不一样（同为财务章节，有的叫"三、"有的叫"四、"），
# 所以跨公司章节对齐必须用去掉编号的标准名。
_CHAPTER_NO_RE = re.compile(r"^[一二三四五六七八九十]{1,3}、\s*")


def chapter_std(chapter: str | None) -> str | None:
    return _CHAPTER_NO_RE.sub("", chapter).strip() if chapter else chapter


def connect(db_path: str | Path) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA.read_text(encoding="utf-8"))
    return conn


def letter_id(letter: Letter) -> str:
    """如 北交所-皇裕精密-r1。

    必须带板块：同一家公司完全可能先在新三板挂牌、后去北交所上市，
    两个板块各有一份问询函。不带板块前缀的话 letter_id 会撞车，
    import_letter 里的 DELETE 会把先入库的那份悄悄删掉。
    """
    return f"{letter.board}-{letter.company}-r{letter.round}"


def question_id(letter: Letter, no: int | None) -> str:
    return f"{letter_id(letter)}-q{no}"


def q_id(letter: Letter, seq: int) -> str:
    return f"{letter_id(letter)}-{seq:04d}"


def load_sources() -> dict[str, str]:
    """相对路径 -> 交易所原文链接（由下载阶段写进清单）。

    注意用【相对路径】而不是文件名做 key：两个板块都可能有 贝尔生物_r1.pdf，
    只按文件名索引会互相覆盖。
    """
    path = Path(__file__).resolve().parent.parent / "data" / "letters_manifest.json"
    if not path.exists():
        return {}
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return {}
    return {str(r["file"]).replace("\\", "/"): r.get("url") or ""
            for r in rows if r.get("file")}


def import_letter(conn: sqlite3.Connection, letter: Letter,
                  sources: dict[str, str] | None = None,
                  force: bool = False) -> tuple[int, int]:
    """返回 (新增 query 数, 跳过 query 数)。已存在的文件直接跳过。"""
    lid = letter_id(letter)
    key = str(letter.file_path).replace("\\", "/")
    source_url = (sources or {}).get(key) or letter.source_url
    if not force:
        row = conn.execute("SELECT 1 FROM letter WHERE file_hash=?", (letter.file_hash,)).fetchone()
        if row:
            return 0, 0
    else:
        # 强制重抽前先看一眼：这份 PDF 的内容是不是已经被【另一家】占着
        # （file_hash 是唯一约束）。是的话照老规矩跳过，别撞约束炸掉整次建库。
        other = conn.execute("SELECT 1 FROM letter WHERE file_hash=? AND letter_id<>?",
                             (letter.file_hash, lid)).fetchone()
        if other:
            return 0, 0
    # 强制重抽（--force-board）：PDF 没变但抽取规则变了，得把这家公司旧的行先清掉。
    # 顺序不能反：query 引用 question、question 引用 letter，外键是开着的。
    # query_fts 没有外键，只能按 q_id 逐条删，否则会留下搜得到、但主表已经没有的孤儿行。
    old_ids = [r[0] for r in conn.execute("SELECT q_id FROM query WHERE letter_id=?", (lid,))]
    if old_ids:
        conn.executemany("DELETE FROM query_fts WHERE q_id=?", [(i,) for i in old_ids])
    conn.execute("DELETE FROM query WHERE letter_id=?", (lid,))
    conn.execute("DELETE FROM question WHERE letter_id=?", (lid,))
    conn.execute("DELETE FROM letter WHERE letter_id=?", (lid,))

    conn.execute(
        "INSERT INTO letter(letter_id,company,board,round,source_url,file_path,file_hash,"
        "pages,chapters,imported_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (lid, letter.company, letter.board, letter.round, source_url,
         letter.file_path, letter.file_hash, letter.pages,
         " ".join(letter.chapters), datetime.now().isoformat(timespec="seconds")),
    )

    for q in letter.questions:
        conn.execute(
            "INSERT OR REPLACE INTO question(question_id,letter_id,chapter,question_no,title,"
            "page_start,page_end) VALUES(?,?,?,?,?,?,?)",
            (question_id(letter, q["no"]), lid, q["chapter"], q["no"], q["title"],
             q["page_start"], q["page_end"]),
        )

    added = skipped = 0
    seq_in_question: dict[int, int] = {}
    for q in letter.queries:
        if q.is_boilerplate and len(q.raw_text) < 14:
            skipped += 1
            continue
        seq_in_question[q.question_no] = seq_in_question.get(q.question_no, 0) + 1
        conn.execute(
            "INSERT INTO query(q_id,letter_id,question_id,company,board,round,chapter,"
            "chapter_std,question_no,question,sub_question,asker,item_no,seq_in_letter,"
            "seq_in_question,page_start,page_end,raw_text,norm_strict,norm_loose,norm_core,"
            "char_len,is_boilerplate) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (q_id(letter, q.seq), lid, question_id(letter, q.question_no), letter.company,
             letter.board, letter.round, q.chapter, chapter_std(q.chapter), q.question_no,
             q.question, q.sub_question, q.asker, q.item_no, q.seq,
             seq_in_question[q.question_no], q.page_start, q.page_end,
             q.raw_text, q.norm_strict, q.norm_loose, norm_core(q.raw_text), len(q.raw_text),
             1 if q.is_boilerplate else 0),
        )
        conn.execute("INSERT INTO query_fts(q_id, norm_loose) VALUES(?,?)",
                     (q_id(letter, q.seq), q.norm_loose))
        added += 1
    return added, skipped


def rebuild_gram_df(conn: sqlite3.Connection) -> int:
    """重建 n-gram 文档频率表（IDF 用）。

    df = 含有该 n-gram 的 query 条数。df 越高 -> IDF 越低 -> 权重越小，
    于是"企业会计准则""是否符合""相关规定"这类通用词会自动被压下去。
    n 取 src.search.GRAM_N（打分和 IDF 必须用同一个 n）。
    """
    conn.execute("DELETE FROM gram_df")
    counts: dict[str, int] = {}
    n = GRAM_N
    for row in conn.execute("SELECT norm_core FROM query WHERE norm_core <> ''"):
        s = row[0]
        for g in {s[i:i + n] for i in range(len(s) - n + 1)}:
            counts[g] = counts.get(g, 0) + 1
    conn.executemany("INSERT INTO gram_df(gram, df) VALUES(?,?)", list(counts.items()))
    return len(counts)


def _scan_one(job: tuple[str, str]) -> tuple[Letter | None, tuple | None]:
    """多进程用：一个 PDF 一个任务。

    **单个文件解析失败不能拖垮整次建库。** 以前是 pool.map 直接调 scan_letter，
    一份坏 PDF 抛异常，`list(pool.map(...))` 就把 930 份的解析结果全丢了
    （实测 科创板-快可光伏电子 r1/r2 是权限位为负的加密 PDF，pdfminer 在
    `struct.pack("<L", self.p)` 处抛 `'L' format requires 0 <= number <= 4294967295`，
    整批 40 分钟的解析白跑）。
    现在改成把异常收成返回值，跳过并报出来，其余照常入库。
    """
    path, board = job
    try:
        return scan_letter(path, board=board), None
    except Exception as exc:  # noqa: BLE001
        return None, (path, type(exc).__name__, str(exc)[:160])


def quick_hash(path: str) -> str:
    """只算 hash、不解析 PDF —— 用来判断"这个文件是不是已经入过库了"。

    之前增量模式是【先把 1600 多份 PDF 全解析一遍】，再在插入时按 hash 跳过；
    省的是写、不是读，20 多分钟照花。先算 hash 就能把绝大部分文件挡在解析之前
    —— 一只 PDF 大约 850 KB，全部算一遍十几秒。
    """
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


# 默认扫描范围：目录 -> 板块。目录不存在就跳过，所以少一个板块也能跑。
# 创业板/深主板/科创板的 PDF 是【回复】，问题原文要从里面的提问段抄回来
# （见 src/extract.REPLY_SOURCED）。
# 目录结构：问询函/<板块名>/*.pdf —— 一个板块一个子目录。
DEFAULT_SOURCES = (("问询函/北交所", "北交所"), ("问询函/新三板", "新三板"),
                   ("问询函/创业板", "创业板"), ("问询函/深主板", "深主板"),
                   ("问询函/科创板", "科创板"), ("问询函/沪主板", "沪主板"))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", action="append", default=[],
                    help="目录=板块，可重复；不给就扫 问询函/ 下已有的板块子目录")
    ap.add_argument("--db", default="data/queries.db")
    ap.add_argument("--rebuild", action="store_true", help="清库重建")
    ap.add_argument("--allow-missing", action="store_true",
                    help="配合 --rebuild：清单里有 PDF 已从磁盘删掉时，允许照样从空库重建"
                         "（那些函的内容会永久丢失，除非重新下载）")
    ap.add_argument("--jobs", type=int, default=1, help="解析 PDF 的并行进程数（大库建议 4~8）")
    ap.add_argument("--force-board", action="append", default=[], metavar="板块",
                    help="强制重新解析指定板块。改过抽取规则（src/extract.py）但 PDF 没变时用，"
                         "只重抽这一个板块，其余板块照常走 hash 跳过")
    ap.add_argument("--force-file", action="append", default=[], metavar="PDF",
                    help="强制重新解析指定的一份或几份 PDF（相对项目根或绝对路径都行）。"
                         "改了抽取规则、但只有个别文件受影响时用它，比 --force-board 快得多")
    args = ap.parse_args()

    # --rebuild 是从空库重来：磁盘上已经没有的 PDF，其 letter/question/query 会永久消失。
    # 增量建库不会删行，所以"删 PDF 释放空间"只有在误跑 --rebuild 时才致命。
    # 2026-10 起默认拒绝，逼人显式确认。
    if args.rebuild and not args.allow_missing:
        man = Path(__file__).resolve().parent.parent / "data" / "letters_manifest.json"
        try:
            rows = json.loads(man.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            rows = []
        # 有意删掉的（扫描件等）记在 data/pruned_pdfs.txt 里，不算"缺文件"
        pruned_path = Path(__file__).resolve().parent.parent / "data" / "pruned_pdfs.txt"
        pruned: set[str] = set()
        if pruned_path.exists():
            pruned = {ln.split("#")[0].strip().replace("\\", "/")
                      for ln in pruned_path.read_text(encoding="utf-8").splitlines()
                      if ln.split("#")[0].strip()}
        missing = [r for r in rows
                   if r.get("file") and not Path(r["file"]).exists()
                   and str(r["file"]).replace("\\", "/") not in pruned]
        if missing:
            raise SystemExit(
                "拒绝 --rebuild：清单里有 %d 份 PDF 已经不在磁盘上（例如 %s）。\n"
                "--rebuild 从空库重来，这些函的内容会永久丢掉（增量建库不受影响，"
                "缺失的函会原样留在库里）。\n"
                "要重下：用本地下载脚本按清单重新拉一遍（脚本不在公开仓库里）\n"
                "确实要从空库重建：加 --allow-missing。"
                % (len(missing), missing[0]["file"]))

    db_path = Path(args.db)
    # 一律建到临时文件，全部成功后再原子换名。
    # 以前是直接 os.unlink(args.db) 再重建，中途去查会看到空库或半截库
    # （有人就正好撞上了这个空窗）。现在失败也不会破坏现有库。
    build_path = db_path.with_name(db_path.name + ".building")
    for suffix in ("", "-wal", "-shm"):
        stale = Path(str(build_path) + suffix)
        if stale.exists():
            stale.unlink()
    if not args.rebuild and db_path.exists():
        shutil.copy2(db_path, build_path)      # 增量：以现有库为底

    conn = connect(build_path)
    sources = load_sources()

    pairs: list[tuple[str, str]] = []
    for spec in (args.source or []):
        d, _, b = spec.partition("=")
        pairs.append((d, b or "北交所"))
    if not pairs:
        pairs = [(d, b) for d, b in DEFAULT_SOURCES if Path(d).is_dir()]
    if not pairs:
        raise SystemExit("找不到任何问询函目录（应为 问询函/<板块名>/，例如 问询函/北交所/）")

    jobs: list[tuple[str, str]] = []
    for d, b in pairs:
        files = sorted(Path(d).glob("*.pdf"))
        print(f"板块 {b}: {d}  共 {len(files)} 份 PDF")
        jobs.extend((str(p), b) for p in files)

    force_boards = set(args.force_board)
    if force_boards:
        unknown = force_boards - {b for _d, b in pairs}
        if unknown:
            raise SystemExit("--force-board 里的板块没有对应目录：" + "、".join(sorted(unknown)))

    # --force-file：按"绝对路径"比对，免得 ./a/b.pdf、a\b.pdf 这种写法对不上
    def _key(p) -> str:
        return str(Path(p).resolve()).replace("\\", "/")

    force_files = {_key(p) for p in args.force_file}
    if force_files:
        have = {_key(p) for p, _b in jobs}
        missing = force_files - have
        if missing:
            raise SystemExit("--force-file 里的文件不在扫描范围（或不存在）：\n  "
                             + "\n  ".join(sorted(missing)))

    if not args.rebuild:
        known = {r[0] for r in conn.execute("SELECT file_hash FROM letter")}
        if known:
            before = len(jobs)
            jobs = [j for j in jobs
                    if j[1] in force_boards or _key(j[0]) in force_files
                    or quick_hash(j[0]) not in known]
            print("其中 " + str(before - len(jobs)) + " 份内容与库里一致（sha256 相同），跳过解析；"
                  "本次实际解析 " + str(len(jobs)) + " 份"
                  + ("（其中 " + "、".join(sorted(force_boards)) + " 为 --force-board 强制重抽）"
                     if force_boards else "")
                  + ("（另有 " + str(len(force_files)) + " 份由 --force-file 强制重抽）"
                     if force_files else ""))

    print("扫描 " + str(len(jobs)) + " 份 PDF（并行 " + str(max(args.jobs, 1)) + " 进程）…")
    if args.jobs > 1:
        with ProcessPoolExecutor(max_workers=args.jobs) as pool:
            results = list(pool.map(_scan_one, jobs))
    else:
        results = [_scan_one(j) for j in jobs]
    letters = [r[0] for r in results if r[0] is not None]
    failed = [r[1] for r in results if r[0] is None]

    total_added = total_skipped = 0
    empty_pages: list[Letter] = []
    for letter in letters:
        if letter.pages == 0:
            # 扫不出任何文字层：多半是扫描件/纯图片 PDF。必须报出来，
            # 否则它安安静静进库、query 为 0，谁也不知道漏了。
            empty_pages.append(letter)
        added, skipped = import_letter(conn, letter, sources,
                                        force=(letter.board in force_boards
                                               or _key(letter.file_path) in force_files))
        kept = len(letter.queries) - skipped
        print(f"{letter.board}/{letter.company:<8} 页数{letter.pages:>3}  "
              f"问题节{len(letter.questions):>3}  最小query {kept:>3}"
              f"（入库 {added}，跳过过短 {skipped}）")
        total_added += added
        total_skipped += skipped
    conn.commit()

    grams = rebuild_gram_df(conn)
    conn.commit()

    if empty_pages:
        print()
        # 注意：这里【不能】用 ⚠️ 这类 emoji。Windows 控制台默认 GBK，
        # 打印会在最后一步抛 UnicodeEncodeError —— 而这一步在 os.replace 之前，
        # 整次建库就白跑了（原子替换保住了旧库，但新库要重扫一遍）。
        print("[警告] 下面 " + str(len(empty_pages)) + " 份 PDF 解析出 0 页（没有文字层，"
              "多半是扫描件/纯图片），已入库但不会有任何 query，请人工确认：")
        for letter in empty_pages:
            print("     " + letter.board + "/" + letter.company + "  " + letter.file_path)

    if failed:
        print()
        print("[警告] 下面 " + str(len(failed)) + " 份 PDF 解析失败，已跳过（不入库，"
              "库里不会有它们的内容）。加密 PDF 可以先用 pikepdf 解密另存再重跑：")
        for path, kind, msg in failed:
            print("     " + path + "  [" + kind + "] " + msg)

    n = conn.execute("SELECT COUNT(*) FROM query").fetchone()[0]
    fts = conn.execute("SELECT COUNT(*) FROM query_fts").fetchone()[0]
    print(f"\n入库合计 {total_added} 条（跳过过短 {total_skipped}），"
          f"库内 query {n} 条，FTS 索引 {fts} 条，"
          f"IDF 词表 {grams} 个 {GRAM_N}-gram -> {args.db}")
    for row in conn.execute("SELECT board, COUNT(*) letters, COUNT(DISTINCT company) comps "
                            "FROM letter GROUP BY board ORDER BY board"):
        q = conn.execute("SELECT COUNT(*) FROM query WHERE board = ?",
                         (row["board"],)).fetchone()[0]
        print(f"  {row['board']}: 问询函 {row['letters']} 份 / "
              f"公司 {row['comps']} 家 / query {q} 条")

    # 收尾：把 WAL 落盘，然后原子换名 —— 到这一步现有库才被替换，
    # 之前的所有查询都还看到旧库（零空窗）。
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()
    for suffix in ("-wal", "-shm"):
        stale = Path(str(db_path) + suffix)
        if stale.exists():
            stale.unlink()
    os.replace(build_path, db_path)
    print()
    print("已原子替换 -> " + str(db_path))


if __name__ == "__main__":
    main()
