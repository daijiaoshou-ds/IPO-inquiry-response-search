"""全库扫描：找出"把回复正文/复述提问当成提问"的 query。

三种判据，互相独立，命中即记：

  R1 复述复用  同一份函里，query A 的开头是另一条 query B 的开头（去掉标点后
               B 是 A 的前缀）。这是「回复：」后面把提问段原样复述一遍造成的：
               复述行被当成新问题起点，后面的回答正文全被收进 A。
               —— 凌云光技术 r1-0346 就是这个形态。

  R2 无提问特征  长度 > 250，且不含任何一种提问标志词（请发行人 / 是否 / 结合 /
               说明 / 列示 / 补充披露 …）。真实提问几乎不可能同时缺这些。

  R3 数字密度  长度 > 250，且数字+百分号字符占比 > 12%。回答正文里的财务
               表格会把这个比例顶很高，提问段不会。

用法：
    python -m scripts.scan_bad_queries --out data/qc_suspect.csv

只读，不改库。改完抽取规则（src/extract.py）之后用它复查一遍，命中数应该下降。
"""
from __future__ import annotations

import argparse
import csv
import re
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 提问标志词：只要出现一个，就不判 R2
ASK_WORDS = ("请发行人", "请保荐", "请申报", "请说明", "请补充", "请核查", "请披露",
             "是否", "结合", "说明", "列示", "补充披露", "解释", "分析", "对比",
             "？", "?", "如何", "为何", "原因", "依据")

DIGIT_RE = re.compile(r"[0-9０-９%％]")

# 只有"下载到的是回复"的板块才可能出现答案混入。北交所、新三板拿到的是问询函
# 原文，提问段本来就写成陈述句（"申请材料显示，公司2022年末…"）、本来就含大量
# 财务数据——用 R2/R3 去判它们 100% 是误报（实测抽 6 条，6 条都是真提问）。
REPLY_BOARDS = {"创业板", "深主板", "科创板", "沪主板"}

# 回答正文的强特征词。只用**第三人称答复口吻**：提问段里不会出现「发行人回复」
# 「本所认为」这种句式（提问写的是「请发行人说明」，带「请」）。
# 试过再加「报告期内，公司」「具体如下：」「单位：万元」，误报八成——那几句
# 在真提问里也常出现（「根据申报文件，报告期内，公司存在…」）。所以不要加。
ANSWER_WORDS = ("发行人回复", "公司回复", "发行人说明公司", "本所认为",
                "保荐机构认为", "申报会计师认为", "经核查", "回复如下", "答复如下")


def load(conn: sqlite3.Connection):
    return conn.execute(
        "SELECT q_id, letter_id, board, company, round, question_no, char_len, "
        "norm_core, raw_text FROM query WHERE is_boilerplate = 0"
    ).fetchall()


def detect(rows):
    """返回 {q_id: [原因...]}"""
    hit: dict[str, list[str]] = {}

    def add(qid, why):
        hit.setdefault(qid, []).append(why)

    # ---- R1 复述复用：同 letter 内前缀包含 ----
    by_letter: dict[str, list] = {}
    for r in rows:
        by_letter.setdefault(r["letter_id"], []).append(r)
    for _lid, group in by_letter.items():
        if len(group) < 2:
            continue
        # 用前 20 字做桶，避免 O(n^2)
        buckets: dict[str, list] = {}
        for r in group:
            core = r["norm_core"] or ""
            if len(core) < 20:
                continue
            buckets.setdefault(core[:20], []).append(r)
        for _key, cand in buckets.items():
            if len(cand) < 2:
                continue
            cand.sort(key=lambda r: len(r["norm_core"]))
            for i, short in enumerate(cand):
                for long_ in cand[i + 1:]:
                    sc = short["norm_core"]
                    lc = long_["norm_core"]
                    if len(lc) <= len(sc):
                        continue
                    if not lc.startswith(sc):
                        continue
                    # 短的那条本身得像个问题（含提问标志），长的才可疑
                    if not any(w in (short["raw_text"] or "") for w in ("请", "是否", "说明")):
                        continue
                    ratio = len(sc) / max(1, len(lc))
                    # 门槛要狠：只判"长的那条比原提问长一倍以上"的。
                    # 放到 0.9 会把"两个相关小问共享同一段前缀"全抓进来
                    # （实测 77%~81% 前缀占比的全是误报，两条都是提问）。
                    if len(lc) > 250 and ratio < 0.6:
                        add(long_["q_id"], f"R1 复述+回答（前缀占 {ratio:.0%}，原提问 {short['q_id']}）")

    # ---- R4 回答段标记（长度 > 120 即可判）----
    for r in rows:
        if r["board"] not in REPLY_BOARDS:
            continue
        t = r["raw_text"] or ""
        if len(t) <= 120:
            continue
        ans = [w for w in ANSWER_WORDS if w in t]
        if ans and not t.startswith("请"):
            add(r["q_id"], "R4 含回答标记（" + "/".join(ans[:2]) + "）")

    # ---- R2 / R3 ----
    for r in rows:
        if r["board"] not in REPLY_BOARDS:
            continue
        t = r["raw_text"] or ""
        n = len(t)
        if n <= 250:
            continue
        if not any(w in t for w in ASK_WORDS):
            tag = "R2 无提问特征"
            ans = [w for w in ANSWER_WORDS if w in t]
            if ans:
                tag += "（含" + "/".join(ans[:3]) + "）"
            add(r["q_id"], tag)
        d = len(DIGIT_RE.findall(t))
        if d / max(1, n) > 0.12:
            add(r["q_id"], f"R3 数字密度 {d / n:.0%}")
    return hit


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(ROOT / "data" / "queries.db"))
    ap.add_argument("--out", default=str(ROOT / "data" / "qc_suspect.csv"))
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    rows = load(conn)
    print("扫描 " + str(len(rows)) + " 条 query")
    hit = detect(rows)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    info = {r["q_id"]: r for r in rows}
    with out.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["q_id", "板块", "公司", "轮次", "题号", "长度", "原因", "开头 60 字"])
        for qid, whys in sorted(hit.items(),
                                key=lambda kv: -info[kv[0]]["char_len"]):
            r = info[qid]
            w.writerow([qid, r["board"], r["company"], r["round"], r["question_no"],
                        r["char_len"], " | ".join(whys),
                        (r["raw_text"] or "")[:60].replace("\n", " ")])

    # ---- 汇总 ----
    from collections import Counter
    print("\n=== 按板块 × 原因 ===")
    by = Counter()
    for qid, whys in hit.items():
        b = info[qid]["board"]
        for wy in whys:
            by[(b, wy.split("（")[0].split(" ")[0])] += 1
    boards = ["北交所", "新三板", "创业板", "深主板", "科创板", "沪主板"]
    print("%-6s %7s %8s %7s %7s %8s" %
          ("板块", "R1复述", "R2无特征", "R3数字", "R4回答", "去重合计"))
    for b in boards:
        tot = sum(1 for qid in hit if info[qid]["board"] == b)
        print("%-6s %7d %8d %7d %7d %8d" %
              (b, by[(b, "R1")], by[(b, "R2")], by[(b, "R3")], by[(b, "R4")], tot))

    print("\n=== 长度超过 800 字的 %d 条里，多少被标出 ===" %
          sum(1 for r in rows if r["char_len"] > 800))
    big = [r for r in rows if r["char_len"] > 800]
    marked = [r for r in big if r["q_id"] in hit]
    print("  %d / %d （%.0f%%）" % (len(marked), len(big),
                                    100 * len(marked) / max(1, len(big))))
    miss = [r for r in big if r["q_id"] not in hit]
    print("  未标出的 %d 条：" % len(miss))
    for r in miss[:8]:
        print("    %-30s %5d %s" % (r["q_id"], r["char_len"],
                                    (r["raw_text"] or "")[:44].replace("\n", " ")))
    print("\n清单：" + str(out))


if __name__ == "__main__":
    main()
