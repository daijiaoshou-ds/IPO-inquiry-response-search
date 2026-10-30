# -*- coding: utf-8 -*-
"""检索排序：IDF 加权字符 n-gram 余弦 + 最长公共子串。

n-gram 取 4（见 GRAM_N）。实测（probe/ngram_eval3.py，200 条跨公司样本、全库排序）：
在"库里没有近逐字重复、只能靠措辞相近去捞"的改写区间，
4-gram 的 P@1 是 0.51 / MRR 0.60，2-gram 只有 0.27 / 0.33，3-gram 0.34 / 0.43；
两档 gram 频率阈值（400 / 3000）下结论一致，混合 2+4 反而输给纯 4-gram。
原因是中文字符级 4 字串已经能稳定指代一个法律要求（"研发人员认定标准"），
2 字串太碎（"说明""是否"满库都是），IDF 压完剩下的信息量仍然不够。

为什么不用纯 grep：
  grep 是布尔命中，产不出"由高相似到低相似"的排序；
  而且不同公司问同一个问题时会换措辞（实测 design.md 里的研发人员问题
  在真实语料里 grep 0 命中，但打分排序能把它排到第一位）。

为什么要 IDF 加权：
  等权 Dice 会让"是否符合《企业会计准则》的规定"这类通用表达主导相似度。
  norm_core 先清掉法规引用，IDF 再把"符合""规定""是否存在"这类满库都是的
  通用词自动压到接近 0 权重，留下的才是真正的问题特征。
"""
from __future__ import annotations

import collections
import math
import os
import re
import sqlite3

from .extract import split_user_input
from .normalize import is_boilerplate, norm_core, norm_core_map, norm_loose, norm_strict

W_GRAM = 0.70
W_LCS = 0.20
W_QUESTION = 0.05
W_CHAPTER = 0.05

# sim = W_COS × 对称余弦 + W_COV × query 覆盖度。
# 只用对称余弦时，候选只要比 query 长很多就被稀释：用户搜「说明报告期各期末
# 存货跌价准备的计提方法、计提依据及充分性」（27 字），命中一家 111 字的候选，
# 里面「存货跌价准备的计提方法」逐字相同、还写了「计提充分性」，余弦却只有 0.23，
# 因为候选有 80 多个 query 根本没提的 gram 参与了分母。
# 覆盖度 dot/|A|² 问的是"我的问题被答了多少"，对长度不敏感。
# 实测（probe/ngram_eval4.py，两档频率阈值 × 全库排序）：半边各 0.5 时
# 改写区间 P@1 0.463→0.476 / 0.476→0.524，MRR 0.556→0.569 / 0.587→0.620。
W_COS = 0.5
W_COV = 0.5

# 一条最小 query 里常常塞了好几个要求（用句号分开）。
# 分句后单独比对，再把"最佳分句"折回 0.9 权重参与打分，
# 这样"我方只命中其中一半要求"也能被合理识别。
CLAUSE_DISCOUNT = 0.9
CLAUSE_MIN_LEN = 12
# 分句比对是 O(候选数 × 分句数)。用户整段粘贴时（分句多、粗筛又捞出几万条），
# 全部算会把一次检索拖到十几秒（probe/perf4.py：394 字粘贴、7 个分句、4.9 万条候选
# = 39.6 万次比对）。所以给这块工作设一个总量预算：超预算就先按全局分粗排，
# 只给靠前的那批补算分句，其余的保持全局分。
CLAUSE_WORK_BUDGET = 40_000
SENT_RE = re.compile(r"[。；！？!?]")

BOILERPLATE_PENALTY = 0.55
TOP_FOR_LCS = 300
# LCS 是纯 Python 的 O(len_a × len_b) 动态规划，长 query 上很贵
# （435 字 × 435 字一次约 9ms）。按字符预算缩候选数，别让一条长粘贴把服务卡死。
LCS_CHAR_BUDGET = 1_500_000
# 粗筛最多用多少个 4-gram。短 query 全部用上；长 query 只取最稀有的那些，
# 倒排表短、定位准，实测候选池从 2.3 万降到 1 千以内。
MAX_FTS_TERMS = 150
# OR 粗筛的召回很足，但长 query 会把半个库都捞出来（实测 394 字 -> 4.9 万条，
# 占全库 55%），于是"越长的输入越慢"而且几乎是线性劣化。
# 所以给粗筛结果加一道 FTS5 bm25 排序 + 截断：bm25 会奖励命中更多稀有 gram 的文档，
# 正是我们要的信号，留 top 6000 再进精确打分。0 表示不截断。
FTS_POOL_CAP = 6000


# ---------------------------------------------------------------- IDF

class Idf:
    """n-gram -> 权重。库里没有的 gram 给默认（最高）权重。"""

    __slots__ = ("table", "default")

    def __init__(self, table: dict[str, float], default: float):
        self.table = table
        self.default = default

    def __call__(self, gram: str) -> float:
        return self.table.get(gram, self.default)


_IDF_CACHE: dict[str, tuple[float, Idf]] = {}


def _db_path(conn: sqlite3.Connection) -> str:
    row = conn.execute("PRAGMA database_list").fetchone()
    return row[2] if row and row[2] else ":memory:"


def load_idf(conn: sqlite3.Connection) -> Idf:
    path = _db_path(conn)
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        mtime = 0.0
    cached = _IDF_CACHE.get(path)
    if cached and cached[0] == mtime:
        return cached[1]

    n = conn.execute("SELECT COUNT(*) FROM query WHERE norm_core <> ''").fetchone()[0] or 1
    rows = conn.execute("SELECT gram, df FROM gram_df").fetchall()
    if not rows:
        # 老库没建过 gram_df，现场统计一遍
        counts: dict[str, int] = {}
        for (s,) in conn.execute("SELECT norm_core FROM query WHERE norm_core <> ''"):
            for gram in {s[i:i + GRAM_N] for i in range(len(s) - GRAM_N + 1)}:
                counts[gram] = counts.get(gram, 0) + 1
        rows = list(counts.items())

    table = {g: math.log(1.0 + n / (1.0 + df)) for g, df in rows}
    idf = Idf(table, math.log(1.0 + n))
    _IDF_CACHE[path] = (mtime, idf)
    return idf


# ---------------------------------------------------------------- 相似度

# 打分用的 n-gram 长度。改这个值必须同步重建 gram_df（src.build_db）。
GRAM_N = 4


def ngrams(s: str, n: int = GRAM_N) -> collections.Counter:
    if len(s) < n:
        return collections.Counter({s: 1}) if s else collections.Counter()
    return collections.Counter(s[i:i + n] for i in range(len(s) - n + 1))


def bigrams(s: str) -> collections.Counter:
    """早期 probe 脚本用过的 2-gram，保留以免那些脚本失效。"""
    return ngrams(s, 2)


def similarity_pair(A: collections.Counter, B: collections.Counter,
                    weight: Idf) -> tuple[float, float]:
    """返回 (对称余弦, query 覆盖度)。

    覆盖度 = dot / |A|²，只跟"query 有多少被覆盖"有关，不除以 |B|。
    """
    if not A or not B:
        return 0.0, 0.0
    na = nb = num = 0.0
    for g, ca in A.items():
        w = weight(g)
        va = ca * w
        na += va * va
        cb = B.get(g, 0)
        if cb:
            num += va * cb * w
    for g, cb in B.items():
        w = weight(g)
        vb = cb * w
        nb += vb * vb
    if not na or not nb:
        return 0.0, 0.0
    return num / math.sqrt(na * nb), num / na


def similarity(A: collections.Counter, B: collections.Counter, weight: Idf) -> float:
    cos, cov = similarity_pair(A, B, weight)
    return W_COS * cos + W_COV * cov


class QuerySide:
    """query（或它的一个分句）的预加权形式。

    similarity_pair 每调用一次都要重新给 A 的所有 gram 查一遍权重、算一遍 |A|，
    而 A 在整个检索过程中是不变的。库大到九万条、一次粗筛捞出几万条候选时，
    这部分占了总耗时的一大半（probe/perf4.py 实测：39.6 万次调用里 54M 次查表
    全花在重复算 A 上）。所以把 A 侧预先算好，每条候选只算自己那一半。
    """

    __slots__ = ("w", "na", "keys")

    def __init__(self, counter: collections.Counter, weight: Idf):
        self.w = {g: c * weight(g) for g, c in counter.items()}
        self.na = sum(v * v for v in self.w.values())
        self.keys = self.w.keys()


def gram_norm(B: collections.Counter, weight: Idf) -> float:
    """候选侧的模长 |B|。跟 query 无关，所以一条候选只算一次，不按分句重复算。"""
    return sum((c * weight(g)) ** 2 for g, c in B.items())


def similarity_side(A: QuerySide, B: collections.Counter, weight: Idf, nb: float) -> float:
    """sim = W_COS × 对称余弦 + W_COV × query 覆盖度，A 侧已预加权。

    数值跟 similarity() 完全一致，只是把 A 侧的工作挪到循环外、把公共 gram
    的求交交给 C 层的集合运算。
    """
    if not A.na or not nb:
        return 0.0
    common = A.keys & B.keys()
    if not common:
        return 0.0
    num = sum(A.w[g] * B[g] * weight(g) for g in common)
    if not num:
        return 0.0
    return W_COS * (num / math.sqrt(A.na * nb)) + W_COV * (num / A.na)


def cosine_counters(A: collections.Counter, B: collections.Counter, weight: Idf) -> float:
    """IDF 加权的 n-gram 余弦相似度（传入已算好的 gram 计数）。

    单独拆出来是为了让调用方把 query 的 gram 只算一次：一条长 query 有 400 多个
    gram，对每个候选都重算一遍会让检索退化成十几秒。
    """
    if not A or not B:
        return 0.0
    num = na = nb = 0.0
    for g, ca in A.items():
        w = weight(g)
        va = ca * w
        na += va * va
        cb = B.get(g, 0)
        if cb:
            num += va * cb * w
    for g, cb in B.items():
        w = weight(g)
        vb = cb * w
        nb += vb * vb
    return (num / math.sqrt(na * nb)) if na and nb else 0.0


def cosine_weighted(a: str, b: str, weight: Idf) -> float:
    """IDF 加权的 n-gram 余弦相似度。

    余弦优于 Dice（probe/ab_test.py 实测）：
    等权 Dice 会被"是否符合《企业会计准则》的规定"这种通用表达带偏，
    IDF-Dice 又会因为分母把不匹配的稀有词也算进去而把分数压扁；
    余弦在"运保费收入确认"这种库里没有原题、只能找近邻的场景下，
    能把真正相关的"总额法确认收入""单项履约义务"排到前面。
    """
    return cosine_counters(ngrams(a), ngrams(b), weight)


def lcs_span(a: str, b: str) -> tuple[int, int]:
    """返回 b 中与 a 的最长公共子串的 (起点, 长度)。"""
    if not a or not b:
        return (0, 0)
    prev = [0] * (len(b) + 1)
    best_start, best_len = 0, 0
    for i in range(1, len(a) + 1):
        cur = [0] * (len(b) + 1)
        ai = a[i - 1]
        for j in range(1, len(b) + 1):
            if ai == b[j - 1]:
                v = prev[j - 1] + 1
                cur[j] = v
                if v > best_len:
                    best_start, best_len = j - v, v
        prev = cur
    return (best_start, best_len)


def query_clauses(text: str) -> list[str]:
    """把一条 query 按句号/分号切成若干分句（各自归一化）。"""
    out = []
    for part in SENT_RE.split(text):
        part = part.strip()
        if not part:
            continue
        core = norm_core(part)
        if len(core) >= CLAUSE_MIN_LEN:
            out.append(core)
    if not out:
        core = norm_core(text)
        if core:
            out.append(core)
    return out


# ---------------------------------------------------------------- 粗筛

def _gram_df(conn: sqlite3.Connection, grams: list[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for i in range(0, len(grams), 400):
        chunk = grams[i:i + 400]
        ph = ",".join("?" * len(chunk))
        for g, d in conn.execute("SELECT gram, df FROM gram_df WHERE gram IN (%s)" % ph, chunk):
            out[g] = d
    return out


def fts_terms(conn: sqlite3.Connection, q_loose: str,
              max_grams: int = MAX_FTS_TERMS) -> str | None:
    """把 query 拆成一堆 4-gram，拼成 FTS5 的 OR 查询串。

    长 query 只留【最稀有】的 max_grams 个，不是前 N 个：
    稀有 4-gram 的倒排表短（SQL 快），而且能定位到同一要求，
    实测一条 435 字的 query 候选池从 22966 降到 957，排序结果不变。
    短 query（p50 是 74 字）本来就不足 150 个 gram，全部用上。

    注意：不要把它当成 q_id 列表再塞进 IN (...)。库里到几万条时，
    命中的 q_id 会几万个，超过 SQLite 的绑定参数上限。
    所以这里只返回查询串，由 SQL 把它当成子查询用。
    """
    grams = list(dict.fromkeys(q_loose[i:i + 4] for i in range(len(q_loose) - 3)))
    if not grams:
        return None
    if len(grams) > max_grams:
        df = _gram_df(conn, grams)
        grams.sort(key=lambda g: (df.get(g, 0), g))
        grams = grams[:max_grams]
    return " OR ".join('"' + g.replace('"', '""') + '"' for g in grams)


# ---------------------------------------------------------------- 检索

def search_one(conn: sqlite3.Connection, question: str, *, board=None, chapter=None,
               question_no=None, round_no=None, expand=False, top=10,
               include_boilerplate=False, idf=None) -> list[dict]:
    q_core = norm_core(question)
    q_loose = norm_loose(question)
    q_strict = norm_strict(question)
    q_clauses = query_clauses(question)
    idf = idf or load_idf(conn)

    where, params = [], []
    if board and not expand:
        where.append("board = ?")
        params.append(board)
    if round_no is not None:
        where.append("round = ?")
        params.append(round_no)
    if chapter and not expand:
        # 章节只对"问询函体裁"的板块（北交所/新三板）可信。创业板/深主板是回复体裁，
        # 每道题答案内部的"一、核查程序 / 二、核查意见"会被规则 1 当成章节
        # （实测创业板 2676 个取值、深主板 207 个，全是这类小标题），
        # 既不能跨公司对齐，也不能当过滤条件——前端已下线章节筛选，参数留给命令行。
        # 另外 2814 份、77262 条 query（37%）根本无章节（r2 及以后 + 新三板），
        # 所以过滤必须放行 chapter_std IS NULL，否则会静默漏掉三成七的库。
        where.append("(chapter_std = ? OR chapter = ? OR chapter_std IS NULL)")
        params.extend([chapter, chapter])
    # 禁用是治理层决定，没有"放行"开关（区别于 is_boilerplate，那个可 include）
    where.append("disabled = 0")
    if not include_boilerplate:
        where.append("is_boilerplate = 0")

    terms = fts_terms(conn, q_loose)
    if terms:
        sub = "SELECT q_id FROM query_fts WHERE query_fts MATCH ?"
        if FTS_POOL_CAP:
            sub += " ORDER BY bm25(query_fts) LIMIT " + str(FTS_POOL_CAP)
        where.append("q_id IN (" + sub + ")")
        params.append(terms)

    clause = ("WHERE " + " AND ".join(where)) if where else ""
    rows = conn.execute("SELECT * FROM query " + clause, params).fetchall()

    # query 的 gram 只算一次，循环里只算候选的
    q_grams = ngrams(q_core)
    q_clause_grams = [ngrams(c) for c in q_clauses]
    # query 侧（含每个分句）预加权；候选侧每条只算一次模长
    q_side = QuerySide(q_grams, idf)
    clause_sides = [QuerySide(c, idf) for c in q_clause_grams]
    use_clauses = len(clause_sides) > 1
    clause_pool = max(top * 4, CLAUSE_WORK_BUDGET // max(1, len(clause_sides)))
    defer_clauses = use_clauses and len(rows) * len(clause_sides) > CLAUSE_WORK_BUDGET

    scored = []
    for r in rows:
        target = r["norm_core"] or r["norm_loose"]
        t_grams = ngrams(target)
        nb = gram_norm(t_grams, idf)
        sim = similarity_side(q_side, t_grams, idf, nb)
        if use_clauses and not defer_clauses:
            best = max(similarity_side(cs, t_grams, idf, nb) for cs in clause_sides)
            best *= CLAUSE_DISCOUNT
            if best > sim:
                sim = best
        exact = bool(q_strict) and q_strict in r["norm_strict"]
        scored.append([sim, exact, r])

    if defer_clauses:
        for i in sorted(range(len(scored)),
                        key=lambda i: (-scored[i][0], scored[i][2]["q_id"]))[:clause_pool]:
            r = scored[i][2]
            target = r["norm_core"] or r["norm_loose"]
            t_grams = ngrams(target)
            nb = gram_norm(t_grams, idf)
            best = max(similarity_side(cs, t_grams, idf, nb)
                       for cs in clause_sides) * CLAUSE_DISCOUNT
            if best > scored[i][0]:
                scored[i][0] = best

    # 同分并列按 q_id 兜底：同公司 r1/r2 常逐字重复同一题，sim 严格相等，
    # 不兜底的话谁在前取决于枚举顺序，"再搜一次"的结果会变。
    scored.sort(key=lambda x: (-x[0], -x[1], x[2]["q_id"]))
    scored = scored[:max(40, min(TOP_FOR_LCS, LCS_CHAR_BUDGET // max(1, len(q_core) ** 2)))]

    out = []
    for sim, exact, r in scored:
        target = r["norm_core"] or r["norm_loose"]
        _p, ln = lcs_span(q_core, target)
        lcs_ratio = ln / max(1, min(len(q_core), len(target) or 1))
        s = W_GRAM * sim + W_LCS * lcs_ratio
        if question_no is not None and r["question_no"] == question_no:
            s += W_QUESTION
        if chapter and chapter in (r["chapter"], r["chapter_std"]):
            s += W_CHAPTER
        if r["is_boilerplate"]:
            s *= BOILERPLATE_PENALTY
        out.append({
            "score": round(s, 4), "sim": round(sim, 3), "lcs": ln, "exact": exact,
            "q_id": r["q_id"], "question_id": r["question_id"],
            "company": r["company"], "board": r["board"], "round": r["round"],
            "chapter": r["chapter"], "chapter_std": r["chapter_std"],
            "question_no": r["question_no"], "question": r["question"],
            "sub_question": r["sub_question"], "asker": r["asker"],
            "pages": str(r["page_start"]) + "-" + str(r["page_end"]),
            "raw_text": r["raw_text"], "char_len": r["char_len"],
            "is_boilerplate": bool(r["is_boilerplate"]),
        })
    out.sort(key=lambda x: (-x["score"], -x["exact"], x["q_id"]))

    # 只给最终要展示的这几条算高亮片段，省时间
    for h in out[:top]:
        core, idx = norm_core_map(h["raw_text"])
        h["hl_spans"] = _runs_to_spans(common_runs(q_core, core), idx, 1) if core else []
        if h["hl_spans"]:
            h["hl_start"], h["hl_end"] = max(h["hl_spans"], key=lambda s: s[1] - s[0])
        else:
            h["hl_start"], h["hl_end"] = 0, 0
    return out[:top]


# ---------------------------------------------------------------- 重合片段

# 候选侧高亮：至少 3 个连续字算一段命中
MIN_RUN = 3
# 覆盖率统计只认全库出现率 <= 5% 的 2-gram（太通用的"说明""是否"不算证据）
ATTN_DF_RATIO = 0.05


def common_runs(a: str, b: str, min_len: int = MIN_RUN) -> list[tuple[int, int, int]]:
    """a 与 b 的所有【极大】公共子串（长度 >= min_len），返回 (a起点, b起点, 长度)。

    极大 = 左右都不能再延伸。一次 O(len_a × len_b) 的 DP 同时拿到全部片段。
    以前只标最长的那一段，会让人误以为算法只看了那一块 —— 其实另外几段
    也是算进分数的，只是没画出来。
    """
    if not a or not b:
        return []
    la, lb = len(a), len(b)
    prev = [0] * (lb + 1)
    out: list[tuple[int, int, int]] = []
    for i in range(1, la + 1):
        cur = [0] * (lb + 1)
        ai = a[i - 1]
        for j in range(1, lb + 1):
            if ai == b[j - 1]:
                v = prev[j - 1] + 1
                cur[j] = v
                if v >= min_len and (i == la or j == lb or a[i] != b[j]):
                    out.append((i - v, j - v, v))
        prev = cur
    return out


def _merge_spans(spans: list[tuple[int, int]]) -> list[list[int]]:
    out: list[list[int]] = []
    for s, e in sorted(spans):
        if out and s <= out[-1][1]:
            if e > out[-1][1]:
                out[-1][1] = e
        else:
            out.append([s, e])
    return out


def _runs_to_spans(runs: list[tuple[int, int, int]], idx: list[int],
                   side: int) -> list[list[int]]:
    """把 (a起点, b起点, 长度) 映射回原文坐标；side=0 取 a 侧，side=1 取 b 侧。"""
    n = len(idx)
    spans = []
    for a0, b0, ln in runs:
        k = a0 if side == 0 else b0
        spans.append((idx[k], idx[min(k + ln - 1, n - 1)] + 1))
    return _merge_spans(spans)


def _flags_to_spans(flags: list[bool], q_idx: list[int]) -> list[list[int]]:
    spans, start = [], None
    for k, f in enumerate(flags + [False]):
        if f and start is None:
            start = k
        elif not f and start is not None:
            spans.append((start, k))
            start = None
    last = len(q_idx) - 1
    return _merge_spans([(q_idx[s], q_idx[min(e - 1, last)] + 1) for s, e in spans])


def covered_spans(question: str, targets: list[str],
                  idf: Idf) -> tuple[list[list[int]], list[list[int]]]:
    """question 有哪几段在 targets 里找到落点，分强弱两档 —— 前端「注意力」展示用。

    强（逐字）：与某个 target 有 >= MIN_RUN 个连续字逐字相同，4-gram 打分能吃到。
    弱（仅 2 字级）：只在一个不那么通用的 2-gram 上对得上。跨公司换措辞时，
    最短的破绽往往就落在 2 字上：query 写「依据及充分性」、库里写「计提充分性」，
    4-gram 全对不上，但「充分」对得上 —— 这正是用户觉得"算法没关注充分性"的地方。
    弱证据不参与排序（实测 cov2 进打分反而掉分），只用来解释分数为什么偏低。
    只用出现率 <= 5% 的 2-gram，避免"说明""是否"这种满库都是的词把整段点亮。
    """
    q_core, q_idx = norm_core_map(question)
    if not q_core or not q_idx:
        return [], []
    cores = [norm_core(t) for t in targets]

    strong = [False] * len(q_core)
    for c in cores:
        for a0, _b0, ln in common_runs(q_core, c, MIN_RUN):
            for k in range(a0, min(a0 + ln, len(q_core))):
                strong[k] = True

    total = math.exp(idf.default) - 1.0
    cut = math.log(1.0 + total / (1.0 + ATTN_DF_RATIO * total))
    weak = list(strong)
    for p in range(len(q_core) - 1):
        bg = q_core[p:p + 2]
        if idf(bg) < cut:
            continue
        if any(bg in c for c in cores):
            weak[p] = True
            weak[p + 1] = True

    weak_only = [w and not s for w, s in zip(weak, strong)]
    return _flags_to_spans(strong, q_idx), _flags_to_spans(weak_only, q_idx)


def _useful_parts(text: str) -> list[str]:
    parts = split_user_input(text)
    useful = [p for p in parts if not is_boilerplate(p)]
    return useful or parts


def search(conn: sqlite3.Connection, text: str, **kw) -> list[dict]:  # noqa: D401
    parts = _useful_parts(text)
    idf = load_idf(conn)
    merged: dict[str, dict] = {}
    for p in parts:
        for hit in search_one(conn, p, idf=idf, **kw):
            hit["from_input"] = p
            old = merged.get(hit["q_id"])
            if old is None or hit["score"] > old["score"]:
                merged[hit["q_id"]] = hit
    return sorted(merged.values(), key=lambda x: (-x["score"], x["q_id"]))[:kw.get("top", 10)]


def _grouped_python(conn: sqlite3.Connection, text: str, **kw) -> dict:
    """按输入的每个最小 query 分组返回，方便看"我的第①问匹配到了什么"。"""
    parts = _useful_parts(text)
    idf = load_idf(conn)
    groups = []
    merged: dict[str, dict] = {}
    for p in parts:
        hits = search_one(conn, p, idf=idf, **kw)
        for h in hits:
            h["from_input"] = p
            old = merged.get(h["q_id"])
            if old is None or h["score"] > old["score"]:
                merged[h["q_id"]] = dict(h)
        # 这一段问题里，哪些部分在返回的这几条里有落点（前端画"注意力"用）
        strong, weak = covered_spans(p, [h["raw_text"] for h in hits], idf)
        total = sum(e - s for s, e in strong) + sum(e - s for s, e in weak)
        groups.append({"text": p, "hits": hits,
                       "cover": {"strong": strong, "weak": weak,
                                 "ratio": round(total / max(1, len(p)), 3)}})
    flat = sorted(merged.values(), key=lambda x: (-x["score"], x["q_id"]))[:kw.get("top", 10)]
    return {"parts": parts, "groups": groups, "flat": flat}


# ---------------------------------------------------------------- Rust 内核

def _grouped_via_rust(conn: sqlite3.Connection, eng, text: str, **kw) -> dict:
    """走 Rust 索引引擎。归一化仍在这里算，Rust 只负责检索与打分。"""
    board = kw.get("board")
    expand = kw.get("expand", False)
    top = kw.get("top", 10)
    round_no = kw.get("round_no")

    parts = _useful_parts(text)
    metas = [{
        "text": p,
        "core": norm_core(p), "loose": norm_loose(p),
        "strict": norm_strict(p), "clauses": query_clauses(p),
    } for p in parts]
    hits_by_part = eng.search(metas, board=None if expand else board,
                              round_no=round_no, top=top)

    # 只把要展示的这几百行取回来（Rust 侧只给 q_id 和分数，不传正文）
    want = [h["q_id"] for hs in hits_by_part for h in hs]
    rows: dict = {}
    for i in range(0, len(want), 800):
        chunk = want[i:i + 800]
        ph = ",".join("?" * len(chunk))
        for r in conn.execute(
                "SELECT * FROM query WHERE q_id IN (" + ph + ") AND disabled = 0", chunk):
            rows[r["q_id"]] = r

    idf = load_idf(conn)
    groups, merged = [], {}
    for p, hits in zip(parts, hits_by_part):
        q_core = norm_core(p)
        out = []
        for h in hits:
            r = rows.get(h["q_id"])
            if r is None:
                continue
            core, idx = norm_core_map(r["raw_text"])
            spans = _runs_to_spans(common_runs(q_core, core), idx, 1) if core else []
            if spans:
                hl_s, hl_e = max(spans, key=lambda s: s[1] - s[0])
            else:
                hl_s, hl_e = 0, 0
            d = {
                "score": h["score"], "sim": h["sim"], "lcs": h["lcs"],
                "exact": h["exact"], "q_id": h["q_id"],
                "question_id": r["question_id"], "company": r["company"],
                "board": r["board"], "round": r["round"],
                "chapter": r["chapter"], "chapter_std": r["chapter_std"],
                "question_no": r["question_no"], "question": r["question"],
                "sub_question": r["sub_question"], "asker": r["asker"],
                "pages": str(r["page_start"]) + "-" + str(r["page_end"]),
                "raw_text": r["raw_text"], "char_len": r["char_len"],
                "is_boilerplate": bool(r["is_boilerplate"]),
                "hl_spans": spans, "hl_start": hl_s, "hl_end": hl_e,
                "from_input": p,
            }
            out.append(d)
            old = merged.get(d["q_id"])
            if old is None or d["score"] > old["score"]:
                merged[d["q_id"]] = d
        strong, weak = covered_spans(p, [x["raw_text"] for x in out], idf)
        total = sum(e - s for s, e in strong) + sum(e - s for s, e in weak)
        groups.append({"text": p, "hits": out,
                       "cover": {"strong": strong, "weak": weak,
                                 "ratio": round(total / max(1, len(p)), 3)}})
    flat = sorted(merged.values(), key=lambda x: (-x["score"], x["q_id"]))[:top]
    return {"parts": parts, "groups": groups, "flat": flat}


def search_grouped(conn: sqlite3.Connection, text: str, **kw) -> dict:
    """按输入的每个最小 query 分组返回。默认走 Rust 索引引擎，不可用时回退 Python。

    开关（环境变量 QB_ENGINE）：
        auto（默认）  能用 Rust 就用；启动失败或运行异常自动回退，无感
        py           强制 Python（对照、排障用）
    Rust 路径覆盖不了的过滤条件（章节 / 指定小问 / 放行套话）直接交回 Python，
    避免"某几个参数被悄悄忽略"。
    """
    unsupported = (bool(kw.get("chapter")) or kw.get("question_no") is not None
                   or kw.get("include_boilerplate"))
    if not unsupported:
        from .engine_rust import get_engine
        eng = get_engine()
        if eng is not None:
            try:
                return _grouped_via_rust(conn, eng, text, **kw)
            except Exception as e:
                print("[engine] Rust 检索失败，本次回退 Python：" + str(e))
    return _grouped_python(conn, text, **kw)


def siblings(conn: sqlite3.Connection, question_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT seq_in_question, asker, sub_question, raw_text, char_len "
        "FROM query WHERE question_id = ? AND disabled = 0 ORDER BY seq_in_question",
        (question_id,)
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------- 库统计

def stats(conn: sqlite3.Connection) -> dict:
    return {
        "letters": conn.execute("SELECT COUNT(*) FROM letter").fetchone()[0],
        "company_count": conn.execute(
            "SELECT COUNT(DISTINCT company) FROM letter").fetchone()[0],
        "pages": conn.execute("SELECT COALESCE(SUM(pages),0) FROM letter").fetchone()[0],
        "letters_by_round": [[r[0], r[1]] for r in conn.execute(
            "SELECT round, COUNT(*) FROM letter GROUP BY round ORDER BY round")],
        "questions": conn.execute("SELECT COUNT(*) FROM question").fetchone()[0],
        "queries": conn.execute("SELECT COUNT(*) FROM query").fetchone()[0],
        "banned": conn.execute(
            "SELECT COUNT(*) FROM query WHERE disabled = 1").fetchone()[0],
        "grams": conn.execute("SELECT COUNT(*) FROM gram_df").fetchone()[0],
        "gram_n": GRAM_N,
        "companies": [r[0] for r in conn.execute(
            "SELECT company, COUNT(*) c FROM query GROUP BY company ORDER BY c DESC")],
        # 分板块明细：北交所和新三板分开看，别混成一个总数
        "by_board": [
            {"board": b, "letters": nl, "companies": nc,
             "queries": conn.execute("SELECT COUNT(*) FROM query WHERE board = ?",
                                     (b,)).fetchone()[0]}
            for b, nl, nc in conn.execute(
                "SELECT board, COUNT(*), COUNT(DISTINCT company) FROM letter "
                "GROUP BY board ORDER BY board")
        ],
        "boards": [r[0] for r in conn.execute(
            "SELECT DISTINCT board FROM query ORDER BY board")],
        "rounds": [r[0] for r in conn.execute("SELECT DISTINCT round FROM query ORDER BY round")],
    }


def top_grams(conn: sqlite3.Connection, limit: int = 12) -> list[tuple[str, int]]:
    """出现最频繁的 n-gram（权重最低的那些），用来佐证 IDF 在干活。"""
    return [(r[0], r[1]) for r in conn.execute(
        "SELECT gram, df FROM gram_df ORDER BY df DESC LIMIT ?", (limit,))]


# ---------------------------------------------------------------- 命令行

def _main(argv: list[str] | None = None) -> int:
    """命令行检索入口（跟前端走的是同一套 search_grouped）。

    例：
        python -m src.search "说明研发人员认定标准及划分依据"
        python -m src.search "收入确认" --board 新三板 --chapter 财务会计信息与管理层分析
        python -m src.search --file 我的问询函片段.txt --expand --full
    """
    import argparse
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    ap = argparse.ArgumentParser(description="在问询函库里检索最小 query")
    ap.add_argument("text", nargs="*", help="问题片段；也可以只用 --file")
    ap.add_argument("--file", default="", help="从文本文件读问题片段")
    ap.add_argument("--db", default=str(root / "data" / "queries.db"))
    ap.add_argument("--board", default=None, help="限定板块：北交所 / 新三板")
    ap.add_argument("--chapter", default=None, help="限定章节（标准化后的名字）")
    ap.add_argument("--round", dest="round_no", type=int, default=None)
    ap.add_argument("--question-no", dest="question_no", type=int, default=None)
    ap.add_argument("--expand", action="store_true", help="扩大范围：跨板块、跨章节")
    ap.add_argument("--all", dest="include_boilerplate", action="store_true",
                    help="把套话也放出来（默认不发套话）")
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--full", action="store_true", help="打印候选全文而不是前 220 字")
    args = ap.parse_args(argv)

    text = " ".join(args.text)
    if args.file:
        text = Path(args.file).read_text(encoding="utf-8")
    if not text.strip():
        ap.error("要么给一段问题文字，要么用 --file 指定文件")
    if not Path(args.db).exists():
        ap.error("找不到数据库 " + args.db + "，先跑 src.build_db")

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    res = search_grouped(conn, text, board=args.board, chapter=args.chapter,
                         question_no=args.question_no, round_no=args.round_no,
                         expand=args.expand, top=args.top,
                         include_boilerplate=args.include_boilerplate)

    print("输入拆成 " + str(len(res["groups"])) + " 个最小 query，"
          + ("扩大范围" if args.expand else (args.board or "全部板块")))
    for g in res["groups"]:
        print()
        print("● " + g["text"])
        print("  落点覆盖率 " + str(g["cover"]["ratio"]) + "，命中 " + str(len(g["hits"])) + " 条")
        if not g["hits"]:
            print("  （库里没有对应的）")
        for h in g["hits"]:
            body = h["raw_text"] if args.full else h["raw_text"][:220]
            print("  %-6s %-6s %-6s %s" % ("%.3f" % h["score"], h["board"], h["company"],
                                           h["chapter_std"] or "（无章节）"))
            print("         " + body.replace("\n", " "))
    print()
    print("全局 top " + str(len(res["flat"])) + "：")
    for h in res["flat"]:
        print("  %.3f  %s / %s / %s（%s 字）"
              % (h["score"], h["board"], h["company"], h["question_no"],
                 h["char_len"]))
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(_main())
