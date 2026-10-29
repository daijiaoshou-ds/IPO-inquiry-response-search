# -*- coding: utf-8 -*-
"""「参考答案指引」的公共逻辑：聚合命中 + 生成 xlsx。

原来这些都长在 scripts/make_guide.py 里，只有命令行能用。网页上传整份函之后
也要给人一个 Excel，两边必须是同一份代码——同一份函，命令行跑出来的表和网页
点「导出」下下来的表若不一样，人第一反应是"哪个是错的"，而不是"哪边配置不同"。
所以把纯逻辑搬到这里，脚本只管命令行，服务端只管 HTTP，表由同一个函数出。

对外三件事：
    aggregate(conn, ask, top, min_score, exclude)  -> (命中的完整行, 最高分, 落点)
    build_workbook(rows, meta, top)                -> openpyxl Workbook
    write_xlsx(path, rows, meta, top)              -> 落盘
"""
from __future__ import annotations

import math
import re
from collections import OrderedDict, defaultdict
from datetime import datetime

# 分档：实测"库里没有的主题" top1 落在 0.22~0.37，真相近的在 0.42~0.72，
# 语料里有逐字相同提问的会顶到 0.9 以上（分句会被 0.9 折扣压一下）。
# 表里只出这三档，不出数值——数值对使用者是噪声，他也不需要按 0.631 和 0.628
# 做区分；真要排序，明细行的顺序本来就是按分数排好的。
BANDS = ((0.60, "高"), (0.42, "中"), (0.30, "低"))
# 门槛默认 0：**不按分数丢弃任何候选**。
# 原来默认 0.30，低于它的直接不给，表里就写成"没找到"——但那是假话：
# 库里明明有 0.155、0.148 这样的结果，只是不够像。使用者要的是"最接近的是谁"，
# 自己去判断值不值得看，不是替他判定"没有"。
DEFAULT_MIN = 0.0
# 命中排行只统计达到"低"档（≥0.30）的命中：极低档基本是 4-gram 随机撞上的，
# 计入排行会让"哪些公司的函反复撞上"变成噪声。
RANK_MIN = 0.30
# 同一家公司只留一条：r1/r2/r3 常常逐字重复同一道题，占满一整列没有信息量
MAX_PER_COMPANY = 1

CN_NUM = {1: "一", 2: "二", 3: "三", 4: "四", 5: "五", 6: "六"}

DETAIL_HEAD = ["题号", "问题", "小问索引", "小问", "参考公司",
               "检索到的问题", "相似度", "指引"]
DETAIL_WIDTH = [6, 20, 9, 44, 18, 44, 8, 36]


def clean_title(title: str) -> str:
    """标题里已经带「问题3.」这类编号，表格另有题号列，去掉避免重复。"""
    return re.sub(r"^\s*(?:问题\s*\d{1,3}|\d{1,3})\s*[\.、]\s*", "", title or "").strip()


def round_cn(r) -> str:
    """1 -> 一轮回复。"""
    try:
        n = int(r)
    except (TypeError, ValueError):
        return "第%s轮回复" % r
    return CN_NUM.get(n, str(n)) + "轮回复"


def guide_of(h: dict) -> str:
    """指引：板块/公司/轮次/那道题。例：新三板/绿霸化工/一轮回复/关于公司业务及合规性"""
    title = clean_title(h.get("question") or "") or ("第%s题" % h.get("question_no", ""))
    return "%s/%s/%s/%s" % (h["board"], h["company"], round_cn(h["round"]), title)


def band(score: float) -> str:
    """只出高/中/低三档，低于最低一档的写「极低」。

    这里刻意不再区分"逐字"：语料里有逐字相同的提问时分数本来就顶到 0.9 以上，
    自然落在"高"档，再单列一档对使用者的判断没有增量。
    """
    for cut, name in BANDS:
        if score >= cut:
            return name
    return "极低"


def guess_company(filename: str) -> str:
    """从文件名里抠出公司名，用来把"自己"从候选里排掉。

    「关于深圳市裕富照明股份有限公司公开发行股票并在北交所上市申请文件的审核问询函」
    -> 深圳市裕富照明股份有限公司
    抠不出来就返回空串；宁可不错排，也不要错排掉一家真该看的公司。
    """
    m = re.search(r"关于(.+?(?:股份有限公司|有限责任公司|有限公司))", filename or "")
    return m.group(1) if m else ""


# ---------------------------------------------------------------- 检索聚合

def aggregate(conn, ask: str, top: int, min_score: float,
              exclude: set[str]) -> tuple[list[dict], float, dict]:
    """一个小问 -> (至多 top 条历史相似题, 全部候选里的最高分, 落点覆盖)。

    最高分单独返回：门槛以下一条不给，但要能告诉人"最像的只有 0.28"，
    这跟"工具没查到"是两件事。

    返回的仍是 search_grouped 给的完整命中行（带 hl_spans / pages / raw_text），
    不是精简过的副本——网页要拿它渲染高亮，表格要拿它写"检索到的问题"，
    两边砍成不同的字段只会让后面的人反复对照两套结构。
    """
    from .search import search_grouped

    res = search_grouped(conn, ask, top=max(top * 8, 40))
    cover = res["groups"][0]["cover"] if res["groups"] else {"strong": [], "weak": [], "ratio": 0}
    best_any = 0.0
    for grp in res["groups"]:
        for h in grp["hits"]:
            if h["company"] not in exclude and h["score"] > best_any:
                best_any = h["score"]

    best: "OrderedDict[str, dict]" = OrderedDict()
    for grp in res["groups"]:
        for h in grp["hits"]:
            if h["score"] < min_score or h["company"] in exclude:
                continue          # 太低的不给；也别自己匹配自己
            key = h["question_id"]      # 聚到「问题节」，不按小问
            cur = best.get(key)
            if cur is None or h["score"] > cur["score"]:
                best[key] = h

    ranked = sorted(best.values(), key=lambda x: (-x["score"], x["company"]))
    per_company: dict[tuple, int] = {}
    keep = []
    for x in ranked:
        ck = (x["company"], x["board"])
        if per_company.get(ck, 0) >= MAX_PER_COMPANY:
            continue
        per_company[ck] = per_company.get(ck, 0) + 1
        keep.append(x)
        if len(keep) >= top:
            break
    return keep, best_any, cover


def rows_from_letter(conn, letter, top: int, min_score: float,
                     exclude: set[str]) -> list[dict]:
    """整份函 -> 一行一个小问的中间结构（表格和网页共用同一份）。

    网页上传后先用它渲染列表，点「导出」时再把同一份 rows 交给
    build_workbook —— 屏幕上看到几条、Excel 里就是几条，不会因为两条代码路径
    各自合并去重而对不上。
    """
    qmap = {q["no"]: q for q in letter.questions}
    rows = []
    sub_idx = 0
    cur_qno = None
    for q in letter.queries:
        # 小问索引：每道题内从 1 重新计数
        if q.question_no != cur_qno:
            cur_qno, sub_idx = q.question_no, 0
        sub_idx += 1
        hits, best_any, cover = aggregate(conn, q.raw_text, top, min_score, exclude)
        rows.append({
            "qno": q.question_no,
            "qtitle": clean_title(qmap.get(q.question_no, {}).get("title", "")),
            "sub_idx": sub_idx,
            "ask": q.raw_text,
            "hits": hits,
            "best_any": best_any,
            "cover": cover,
        })
    return rows


def rank_objects(rows: list[dict]) -> list[tuple[str, int, float]]:
    """命中对象排行：哪些公司的函反复撞上。

    按"命中的小问数"计，不按参考条数——同一个小问撞上同一家公司 3 条，
    说明的是同一个问题，算 3 次会虚高。
    """
    hit_subs: dict[tuple, set] = defaultdict(set)
    for row in rows:
        for h in row["hits"]:
            if h["score"] < RANK_MIN:
                continue
            hit_subs[(h["board"], h["company"], h["round"])].add(
                (row["qno"], row["sub_idx"]))
    total = max(1, len(rows))
    out = [("%s/%s/%s" % (k[0], k[1], round_cn(k[2])), len(v), len(v) / total)
           for k, v in hit_subs.items()]
    out.sort(key=lambda x: (-x[1], x[0]))
    return out


def make_meta(source: str, n_q: int, n_sub: int, conn, top: int,
              min_score: float = DEFAULT_MIN) -> dict:
    st = conn.execute(
        "SELECT COUNT(*), (SELECT COUNT(*) FROM query) FROM letter").fetchone()
    return {
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "source": source,
        "n_q": n_q,
        "n_sub": n_sub,
        "corpus": "%d 份函 / %d 条提问 / 6 个板块（截至生成时）" % (st[0], st[1]),
        "top": top,
        "min_score": min_score,
    }


# ---------------------------------------------------------------- 写 xlsx

def _units(s) -> int:
    """按显示宽度估算：中文/全角算 2，ASCII 算 1。"""
    return sum(2 if ord(ch) > 127 else 1 for ch in str(s))


def _height(values, widths) -> float:
    lines = 1
    for v, w in zip(values, widths):
        if v is None or v == "":
            continue
        lines = max(lines, math.ceil(_units(v) / max(w - 1, 4)))
    # 上限放到 240：244 字的小问按 44 宽要 11 行，原来的 160 会把它裁掉半截
    return 15.0 if lines <= 1 else min(lines * 14.5 + 4, 240)


def build_workbook(rows: list[dict], meta: dict, top: int):
    """四张表：说明（最左，先看这张）/ 命中排行 / 明细TOP2 / 明细TOP5。"""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    thin = Side(style="thin", color="DDDDDD")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    head_fill = PatternFill("solid", fgColor="EEF3FA")
    wrap = Alignment(wrap_text=True, vertical="center")
    head_align = Alignment(wrap_text=True, vertical="center", horizontal="center")

    # ---- 表1 说明（最左，先看这张）
    ws = wb.active
    ws.title = "说明"
    ws.column_dimensions["A"].width = 16
    ws.column_dimensions["B"].width = 100

    empty_subs = [r for r in rows if not r["hits"]]
    empty_txt = "；".join("题%s·小问%s（最像 %.2f）" % (
        r["qno"], r["sub_idx"], r.get("best_any", 0.0)) for r in empty_subs[:8])
    if len(empty_subs) > 8:
        empty_txt += "；…等 %d 个" % len(empty_subs)

    lines = [
        ("生成时间", meta["ts"]),
        ("待答函", meta["source"]),
        ("拆出", "%d 道题 / %d 个小问" % (meta["n_q"], meta["n_sub"])),
        ("语料覆盖", meta["corpus"]),
        ("命中情况", "%d 个小问找到参考，%d 个没到门槛" % (
            len(rows) - len(empty_subs), len(empty_subs))),
        ("检索参数", "全板块检索（不分板块）；每个小问取 %d 条；相似度低于 %.2f 丢弃；"
                 "同一家公司只留 1 条" % (top, meta["min_score"])),
        ("表怎么排", "「明细TOP2」每个小问最多 2 条，先看这份；「明细TOP5」最多 %d 条，"
                 "想深挖再看。两份内容一样，只是取的条数不同。" % top),
        ("条数", "每个小问都给满 %d 条，不按分数丢弃。所以会看到 0.1x 的极低结果——"
                 "那不是出错，是库里确实没有更近的。" % top),
        ("", ""),
        ("相似度", "只分 高/中/低/极低 四档（≥0.60 ／ 0.42–0.60 ／ 0.30–0.42 ／ <0.30）。"
                 "明细里同一个小问的多条参考已按相似度从高到低排好，不用再看分数。"),
        ("极低是什么", "库里没有相近的提问，这几条只是最接近的。参考价值有限，"
                 "但也可能是「换个说法问了同一件事」，值不值得看由你判断"
                 "——不替你判「没有」。"),
        ("怎么用", "先看「命中排行」：哪几家公司的函反复撞上，就先看那几家；"
                 "再照着「明细TOP2」按题号顺序过。挑中的题去翻原文。"
                 "（排行只统计达到「低」档以上的命中，极低档不计入。）"),
        ("为什么没答案", "本表只做定位，不做答案摘要。问询回复要严谨，答案必须人自己看。"),
        ("注意", "相似度是 4-gram 逐字比对，不是语义。"
                 "\"存货跌价准备计提是否充分\"和\"转回是否合理\"在向量空间很近，"
                 "在监管逻辑里是两回事，所以宁可给低档也不替你判断。"),
    ]
    if empty_txt:
        lines.append(("没找到的", empty_txt))

    for i, (k, v) in enumerate(lines, 1):
        c1 = ws.cell(row=i, column=1, value=k)
        c1.font = Font(bold=True, size=10)
        c1.alignment = Alignment(vertical="top")
        c2 = ws.cell(row=i, column=2, value=v)
        c2.font = Font(size=10)
        c2.alignment = Alignment(wrap_text=True, vertical="top")
        if v:
            ws.row_dimensions[i].height = _height([v], [100])

    # ---- 表2 命中排行
    # 只列命中 ≥2 次的对象：命中 1 次的对"先去翻哪几家"没有行动价值，
    # 而这样的对象往往有七八十家，全列出来表就废了。其余在脚注里交代去处。
    wb2 = wb.create_sheet("命中排行")
    ranked = [x for x in rank_objects(rows) if x[1] >= 2]
    rest = [x for x in rank_objects(rows) if x[1] < 2]
    if not ranked:                       # 整份函都很生，一个都没重复命中
        ranked = rank_objects(rows)[:10]
        rest = []

    wb2.append(["对象", "问题命中数量", "占问题比重"])
    for c in range(1, 4):
        cell = wb2.cell(row=1, column=c)
        cell.font = Font(bold=True, size=10)
        cell.fill = head_fill
        cell.border = border
        cell.alignment = head_align
    for i, (name, n, ratio) in enumerate(ranked, 2):
        wb2.cell(row=i, column=1, value=name).alignment = Alignment(vertical="center")
        c2 = wb2.cell(row=i, column=2, value=n)
        c2.alignment = Alignment(horizontal="center", vertical="center")
        c3 = wb2.cell(row=i, column=3, value=ratio)
        c3.number_format = "0.00%"
        c3.alignment = Alignment(horizontal="center", vertical="center")
        for c in range(1, 4):
            cell = wb2.cell(row=i, column=c)
            cell.border = border
            cell.font = Font(size=10)

    if rest:
        note = wb2.cell(row=len(ranked) + 3, column=1,
                        value="另有 %d 家各命中 1 个小问，未列入（见「明细」，按参考公司筛选即可）"
                              % len(rest))
        note.font = Font(size=9, italic=True, color="808080")

    for i, w in enumerate([34, 14, 12], 1):
        wb2.column_dimensions[get_column_letter(i)].width = w
    wb2.freeze_panes = "A2"

    # ---- 表3/表4 明细：一行一条参考
    # 两份，只差"每个小问取前几条"：
    #   TOP2 —— 少而精，先看这份
    #   TOP5 —— 想深挖再看这份
    # 没命中的小问也占一行（各列写"—"），否则行号会跳（1,2,3,4,5,6,9,10），
    # 看着像排错了序，其实是中间两行被整行跳过。
    def write_detail(name: str, limit: int) -> None:
        ws3 = wb.create_sheet(name)
        ws3.append(DETAIL_HEAD)
        for c in range(1, len(DETAIL_HEAD) + 1):
            cell = ws3.cell(row=1, column=c)
            cell.font = Font(bold=True, size=10)
            cell.fill = head_fill
            cell.border = border
            cell.alignment = head_align
        ws3.row_dimensions[1].height = 24

        r = 2
        for row in rows:
            hits = row["hits"][:limit]
            if not hits:
                vals = [row["qno"], row["qtitle"], row["sub_idx"], row["ask"],
                        "未找到", "—", "—", "—"]
                ws3.append(vals)
                for c in range(1, len(DETAIL_HEAD) + 1):
                    cell = ws3.cell(row=r, column=c)
                    cell.border = border
                    cell.font = Font(size=10, color="808080")
                    cell.alignment = (Alignment(horizontal="center", vertical="center")
                                      if c in (1, 3, 7) else wrap)
                ws3.row_dimensions[r].height = _height(vals, DETAIL_WIDTH)
                r += 1
                continue
            for h in hits:
                vals = [row["qno"], row["qtitle"], row["sub_idx"], row["ask"],
                        h["company"], h["raw_text"], band(h["score"]), guide_of(h)]
                ws3.append(vals)
                for c in range(1, len(DETAIL_HEAD) + 1):
                    cell = ws3.cell(row=r, column=c)
                    cell.border = border
                    cell.font = Font(size=10)
                    # 整张表垂直居中：行高按内容算过，居中后纵向看起来是齐的
                    cell.alignment = (Alignment(horizontal="center", vertical="center")
                                      if c in (1, 3, 7) else wrap)
                ws3.row_dimensions[r].height = _height(vals, DETAIL_WIDTH)
                r += 1

        for i, w in enumerate(DETAIL_WIDTH, 1):
            ws3.column_dimensions[get_column_letter(i)].width = w
        ws3.freeze_panes = "D2"
        if r > 2:
            ws3.auto_filter.ref = "A1:%s%d" % (get_column_letter(len(DETAIL_HEAD)), r - 1)

    write_detail("明细TOP2", 2)
    write_detail("明细TOP5", max(top, 2))
    return wb


def write_xlsx(path, rows: list[dict], meta: dict, top: int) -> None:
    build_workbook(rows, meta, top).save(str(path))
