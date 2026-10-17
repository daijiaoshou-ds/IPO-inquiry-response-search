# -*- coding: utf-8 -*-
"""问询函 PDF -> 最小 query。

采用设计文档 1.b 方案，但按你的要求先"定位问题片段"再拆：
  1. 章节标题   = "一、基本情况" 形态
  2. 问题节     = "问题N.标题" 形态
  3. 小问标题   = "（N）标题。" 形态
                  （判别规则：该短语不超过 30 字且以句号/冒号收尾，
                    借此与材料里的 "（1）（2）（3）" 枚举区分开）
  4. 问题片段   = 从 "请发行人 / 请保荐机构 / 请申报会计师 / 请发行人律师…" 起，
                  截断于下一个"小问标题"或下一个"根据申请文件…"
  5. 最小 query = 问题片段内再按 ① ② ③ / （1）（2） 切一层
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

from .normalize import is_boilerplate, norm_loose, norm_strict


def iter_lines(*args, **kwargs):
    """读 PDF 的每一行（延迟导入，见下）。

    pdfread 会拉起 pdfplumber —— 一整条 PDF 解析栈（几十 MB）。而 Web 服务
    只用得上 split_user_input，没必要为此把解析栈装进运行环境，所以把导入
    推迟到"真的要读某个 PDF"的那一刻。要单独用 is_hei / page_count，请直接
    from .pdfread import ...
    """
    from .pdfread import iter_lines as _iter_lines
    return _iter_lines(*args, **kwargs)

# 哪些板块的问题原文要去【回复文档】里取（因为交易所不单独公开问询函）。
#
# 深交所（创业板）只公开「问询与回复」，栏目里 100% 是回复；但回复文档把
# 问询函原文整段抄了一遍，所以可以从回复里"抄回"问询函的问题原文。
#
# 怎么分"问题原文"和"回复正文"？先试了字体（文件里通常写着
# 「黑体（加粗）问询函所列问题 / 宋体回复」），**不可靠**：
#   埃索凯 r1   ：问题基本是 SimHei，但"问题26"是 SimSun
#   毅合捷 r2   ：只有问题1是 SimHei，问题2~9 全是 SimSun（和回复同字体）
# 所以改用【结构标记】：回复正文的起点，各家写法不同但都很好认——
#     埃索凯 / 初源新材 ： "回复："
#     毅合捷            ： "【发行人披露】" / "【中介机构核查】"
# 于是规则变成：遇到分界标记就停止收集该问题的正文。
# 这样拿到的正文 = 材料段 + 请发行人段，和北交所那批问询函【同质】，
# 后面完全复用同一套抽取规则。
#
# （字体过滤的能力保留在 pdfread.want_font 里，作为备用手段，默认不启用。）
REPLY_SOURCED = {"创业板"}
# 回复正文的起点标记。各家写法不同，实测见过：
#     "回复："          （埃索凯、初源新材）
#     "回复说明："       （迪嘉药业）—— 原来只写「回复[:：]」会漏掉这个，
#                        漏掉后 answer_started 一直是假，整个回复正文被当成问题正文收进来
#     "【发行人披露】"   （毅合捷）
#     "【问题回复】"     （新研工业）
#     "[回复]"          （长联新材料科技）—— 半角方括号，原来也只认全角【】，
#                        漏掉之后这家公司 4 道题各吞了 3600~6300 字的回复正文
ANSWER_MARK_RE = re.compile(r"^\s*(回复[^。；：]{0,6}[:：]|[【\[][^】\]]{2,14}[】\]])")

# 回复正文的【结构】起点：一级小标题「一、……」。
#
# 为什么显式标记还不够：不是每家都写「回复」「【…】」。实测 港迪技术 r2 问题6、
# 山大电力 r2、永成汽车零部件 r1、新能源技术 r2 等，回复正文**直接**以
#     「一、分析报告期内管理人员、研发人员的人均薪酬变动情况及原因，与可比」
# 开头（把提问段原样复述成一级标题），于是整段回复被并进该题最后一个提问主体的
# query —— 就是"问题+回复一起拿过来"的那批超长 query（最长 14821 字）。
#
# 为什么这条不会误伤提问段：提问函原文只用「（1）（2）」和「①②」分层，一级标题
# 「一、」是回复正文独有的排版。两个例外都排掉了：
#   1. 「一、请发行人披露」—— 少数问询函确实拿一级标题写提问主体，所以加 (?!请)
#      把它排除（它由 ASKER_RE 正常识别，不需要靠这里断句）；
#   2. 回复的复述也可能写成「一、请发行人说明…」，那本来就不该被当问题起点，
#      排除掉正好。
# 回复正文里「（一）发行人说明」「（一）核查程序」这一档也是回复独有的编号——提问函
# 用小括号配【阿拉伯数字】（1）（2），回复用小括号配【汉字数字】（一）（二）。
# 实测 科通技术 r1 的提问段写成「十四、请保荐人、申报会计师发表明确意见，并说明…」，
# 收尾句里"发表明确意见"不在行尾（ASK_CLOSE_RE 认不出来），下一行就是
#     （一）核查程序
# 整段 1910 字的核查程序因此并进了提问段。
REPLY_BODY_RE = re.compile(r"^[一二三四五六七八九十]{1,3}、(?!请)|^（[一二三四五六七八九十]{1,3}）")

# 提问段的收尾句：「…发表明确意见。」。
# 这是提问段在法律意义上的最后一句——写完了就是叫中介机构表态，表态之后必然
# 是回复正文。实测 龙创汽车设计 r2 问题4：问询函原文是黑体，收尾句
#     「请保荐人、发行人律师核查并发表明确意见。」
# 下一行就是宋体的回复，而且回复的复述恰好写成「一、请发行人结合对赌协议…」——
# 因为以「请」开头，被上面 REPLY_BODY_RE 的 (?!请) 排除，于是整段对赌协议的
# 回复（1571 字）全被并进提问段。
# 这条规则把"句子完整地以发表明确意见收尾"当成硬边界，不依赖标题形态。
ASK_CLOSE_RE = re.compile(r"(发表明确意见|发表明确核查意见|发表核查意见|发表明确结论)[。\.]?$")

# 第二轮回复的"问题起点"。
# 实测第二轮常常【不用】"问题N.关于XXX"这种标题，而是直接以「请发行人」开一个自然段：
#     请发行人：                      （国科天成 r2）
#     请发行人说明晟大方霖所持…        （国科天成 r2）
#     请发行人结合原深实业转让…        （迪嘉药业 r2）
# 注意：回复正文里复述问题时是带序号的「一、请发行人…」，所以这里【只认行首的裸"请发行人"】，
# 带「一、」前缀的不算——这一条正好把"真问题"和"正文明复述"分开。
ASK_START_RE = re.compile(r"^请(?:发行人|公司)")

CN_NUM = "一二三四五六七八九十"


def cn_int(s: str) -> int | None:
    """汉字数字 -> int（一~九十九，问询函够用了）。"""
    s = s.strip()
    if not s or any(c not in CN_NUM for c in s):
        return None
    if "十" not in s:
        return CN_NUM.index(s) + 1 if len(s) == 1 else None
    a, _, b = s.partition("十")
    return (CN_NUM.index(a) + 1 if a else 1) * 10 + (CN_NUM.index(b) + 1 if b else 0)


CHAPTER_RE = re.compile(rf"^[{CN_NUM}]{{1,3}}、\s*(\S.*)$")
# 新三板的问题标题：「1.关于向员工集资。」「1、 关于代持事项。」
# 没有「问题」二字，而且标题后面常常直接跟材料/提问正文（同一行），
# 所以标题只取到第一个句号，剩下的还给正文。
# 这条规则只在 board == 新三板 时启用：北交所正文里也可能出现
# 「1.关于…」这样的枚举，不能拿它去切北交所的问题。
NSP_QUESTION_RE = re.compile(
    r"^(\d{1,3})\s*[\.、]\s*(关于[^。：，；]{2,40})(?:。\s*(.*)|\s*)$")
# 正文里偶有以"二、三…"开头的句子被误认成章节标题（实测 3 个章节名带逗号、
# 长达 25 字）。章节标题是短名词短语，不含句读，据此排掉。
CHAPTER_BAD_RE = re.compile(r"[，。；,;:：]")
QUESTION_RE = re.compile(r"^问题\s*(\d{1,3})\s*[\.、]?\s*(.*)$")
# 「问题九、关于在建工程」：少数回复文档用汉字数字编问题号。
# 实测 联纲光电科技 r2 全篇 11 道题全是这种写法，阿拉伯数字一个都没有。
# 不认它，这份文档就被判成"没有'问题N'标题"、退化成用行首裸「请发行人」合成问题；
# 而回复正文里的**复述**恰好也是一行裸「请发行人」，于是复述连同它后面的回答
# 一起被当成一个问题（实测最长 1697 字，前 6 名全是它）。
QUESTION_CN_RE = re.compile(rf"^问题\s*([{CN_NUM}]{{1,3}})\s*[\.、]?\s*(.*)$")

# 深交所的回复里写「请保荐人、发行人律师…」，北交所写「请保荐机构」。
# 「保荐人」必须补进来，否则创业板的提问主体整段识别不到。
ORG = r"(?:发行人|保荐机构|保荐人|申报会计师|会计师事务所|发行人律师|主承销商)"
# 新三板用「请公司：」「请主办券商及律师：」「请会计师核查…」。
# 「公司」太常见，必须强制跟冒号，否则「请公司补充说明」这种半句话
# 会被当成一个新的提问主体，把一句话劈成两半。
ORG_NSP = r"(?:主办券商|律师|会计师|会计师事务所|申报会计师|中介机构)"
ASKER_RE = re.compile(
    rf"请{ORG}(?:[、及和]{ORG}){{0,3}}(?:[^：:。；\n]{{0,6}}?[：:])?"
    rf"|请{ORG_NSP}(?:[、及和]{ORG_NSP}){{0,4}}(?:[^：:。；\n]{{0,6}}?[：:])?"
    rf"|请公司(?:[、及和]{ORG_NSP}){{0,4}}[：:]"
)

MATERIAL_RE = re.compile(
    r"(根据申请文件|根据招股说明书|根据申报材料|根据《|申报材料显示|招股说明书显示"
    r"|申请文件显示|发行人披露|公司披露|根据发行人|根据公开信息"
    r"|根据公开转让说明书|公开转让说明书显示|根据内核文件|内核文件显示|根据申请材料"
    # 深交所回复里的变体：材料段常写成"申报文件显示""申请文件及问询回复显示"
    r"|申报文件显示|申请文件及问询回复显示|审核问询回复显示|问询回复显示)"
)

SUBHEAD_RE = re.compile(r"（\s*(\d{1,2})\s*）")
ITEM_RE = re.compile(r"(①|②|③|④|⑤|⑥|⑦|⑧|⑨|⑩|（\s*\d{1,2}\s*）)")
ITEM_NO_RE = re.compile(r"[①②③④⑤⑥⑦⑧⑨⑩]")


@dataclass
class Query:
    seq: int
    chapter: str | None
    question_no: int | None
    question: str
    sub_question: str
    asker: str
    item_no: str
    raw_text: str
    norm_strict: str
    norm_loose: str
    is_boilerplate: bool
    page_start: int
    page_end: int


@dataclass
class Letter:
    company: str
    file_stem: str
    board: str = "北交所"
    round: int = 1
    source_url: str | None = None
    file_path: str = ""
    file_hash: str = ""
    pages: int = 0
    chapters: list[str] = field(default_factory=list)
    questions: list[dict] = field(default_factory=list)
    queries: list[Query] = field(default_factory=list)


# ---------------------------------------------------------------- 工具

def find_subheads(text: str) -> list[tuple[int, int, str, str]]:
    """找出"像小问标题"的 （N）。返回 [(start, end, no, title)]。"""
    out = []
    for m in SUBHEAD_RE.finditer(text):
        if m.start() > 0 and text[m.start() - 1] not in "。：；\n":
            continue
        start = m.end()
        stop = None
        for ch in "。：":
            i = text.find(ch, start)
            if i != -1 and (stop is None or i < stop):
                stop = i
        if stop is None:
            continue
        title = text[start:stop].strip()
        if not (0 < len(title) <= 30):
            continue
        # 关键判别：小问标题后面必须紧跟"根据申请文件…"或"请发行人…"。
        # 材料里的 "（1）（2）（3）" 枚举后面跟的是下一个"（N）"，会被这条规则排除。
        after = text[stop + 1:].lstrip()
        if not (MATERIAL_RE.match(after) or ASKER_RE.match(after)):
            continue
        out.append((m.start(), m.end(), m.group(1), title))
    return out


def split_items(seg: str) -> list[tuple[str, str]]:
    """把问题片段切成最小 query。返回 [(序号, 文本)]。"""
    parts = ITEM_RE.split(seg)
    items: list[tuple[str, str]] = []
    if parts[0].strip():
        items.append(("", parts[0].strip()))
    for i in range(1, len(parts), 2):
        no = parts[i]
        body = parts[i + 1].strip() if i + 1 < len(parts) else ""
        if body:
            items.append((ITEM_NO_RE.sub("", no).strip() or no.strip(), body))
    return items


def _clean(t: str) -> str:
    """把多余空白（含换行）折叠掉，再去掉尾部标点。

    用户从 PDF / Word 里复制粘贴时，段落里会夹带换行和缩进，
    留着它们会让前端把一句话拆成"大标题 + 小字正文"两截。
    """
    t = re.sub(r"[\s\u3000]+", "", t)
    return re.sub(r"[。；;，,]+$", "", t)


# 切分残留的接续碎片（句子的后半截被切成了独立 query）。
# 用整词前缀而不是单字：单字「以」会把「以表格形式补充说明…」这种
# 正经问题一起误杀（实测新三板 1175 条里误杀 2%），而「以及」才是真碎片。
_FRAGMENT_PREFIX = ("的", "并", "及", "同时", "其中", "此外", "以及", "、", "，", "。", "；")


def _is_fragment(t: str) -> bool:
    t = t.lstrip()
    return bool(t) and t.startswith(_FRAGMENT_PREFIX)


# ---------------------------------------------------------------- 主体

# 下载下来的文件按「公司_r轮次.pdf」命名，例如 皇裕精密_r2.pdf
ROUND_SUFFIX_RE = re.compile(r"^(?P<name>.+?)_r(?P<round>\d+)$")


def parse_stem(stem: str) -> tuple[str, int | None]:
    """从文件名解析出 (公司简称, 轮次)；没有 _rN 后缀时轮次为 None。"""
    m = ROUND_SUFFIX_RE.match(stem)
    if m:
        return m.group("name"), int(m.group("round"))
    return stem, None


def scan_letter(pdf_path: str, company: str | None = None, board: str = "北交所",
                round_no: int = 1, want_font=None) -> Letter:
    path = Path(pdf_path)
    name, file_round = parse_stem(path.stem)
    letter = Letter(
        company=company or name,
        file_stem=path.stem,
        board=board,
        round=file_round if file_round is not None else round_no,
        file_path=str(path).replace("\\", "/"),
        file_hash=hashlib.sha256(path.read_bytes()).hexdigest()[:16],
        pages=0,
    )

    chapter: str | None = None
    cur_q: dict | None = None
    questions: list[dict] = []
    pages = 0
    # 回复文档：见到分界标记之后，这个问题的剩余部分都是回复正文，不再收
    answer_started = False
    # 回复文档里已经出现过的问题编号。回复正文内部也常有「3、关于自然人的
    # 境外账户」这种小标题，和问题标题长得一模一样；但真问题每个编号只出现
    # 一次，所以"编号已出现过"就是它是正文小标题的铁证。
    seen_nos: set[int] = set()

    def close_q(q: dict | None) -> None:
        """结算一个问题。

        **必须所有"结束当前问题"的地方都走这里**，否则会漏登记编号。
        （踩过：章节标题那条分支自己写了一份结算，没登记 seen_nos，
          结果回复正文里同号的小标题被当成新问题放进来，正文一下子涨到 4000 字。）

        规则：只有真正收到过正文的才算问题——回复文档开头的【目录】会把
        全部问题标题列一遍，它们没有正文，必须丢掉，否则会把编号占住。
        """
        if q is None:
            return
        if q["body"]:
            seen_nos.add(q["no"])
            questions.append(q)
        elif board not in REPLY_SOURCED:
            questions.append(q)

    # 回复文档要先整体看一遍：判断这份文档到底有没有"问题N"标题。
    # 第二轮回复常常一个都没有，得改用「裸请发行人」当问题起点（见 ASK_START_RE）。
    # （北交所那套是流式的，但 16000 行的列表在内存里无所谓，换来的是能先判形态。）
    lines = list(iter_lines(str(path), want_font=want_font))
    pages = lines[-1].page if lines else 0

    # 回复文档每页页眉都印着「XXX股份有限公司…审核问询函回复」这类小字，它是纯
    # 噪声，却会被当成提问正文收进来（实测天元航材、天溯计量、毓恬冠佳）。
    # 识别办法不需要认识页眉的写法：**同一行文字重复出现在 ≥3 个页面上、且每次
    # 都在该页最顶端**，那就是页眉。正文里不会有一模一样的整句反复出现。
    running_headers: set[str] = set()
    if board in REPLY_SOURCED:
        top_of_page: dict[int, float] = {}
        for l in lines:
            if l.page not in top_of_page or l.top < top_of_page[l.page]:
                top_of_page[l.page] = l.top
        hits: dict[str, int] = {}
        for l in lines:
            if l.top <= top_of_page.get(l.page, 0.0) + 0.5:
                hits[l.text] = hits.get(l.text, 0) + 1
        running_headers = {t for t, n in hits.items() if n >= 3 and len(t) >= 10}

    use_ask_start = (
        board in REPLY_SOURCED
        and sum(1 for l in lines
                if QUESTION_RE.match(l.text) or QUESTION_CN_RE.match(l.text)
                or NSP_QUESTION_RE.match(l.text)) < 2)
    synth_no = 0

    for line in lines:
        # 章节标题 = 短名词短语，不含句读。
        # 注意：回复文档（创业板）里这条规则【是有用的】，虽然它认出来的名字几乎
        # 全是回复小标题而不是真章节（实测 265 个取值：二、中介机构核查情况 / 一、请
        # 发行人披露 …）。曾经顺手把它对创业板关掉，结果 中塑新材料 r2 立刻从
        # 28 条 / 最长 126 字 变成 198 条 / 最长 2905 字 —— 因为那家的回复恰好用
        # 短标题分段，关掉就再没有东西截断提问段了。所以**保留**它：宁可留一个
        # 没用的 chapter 标签，也不能让提问段失控。
        m_ch = CHAPTER_RE.match(line.text)
        if (m_ch
                and len(line.text) <= 24 and not CHAPTER_BAD_RE.search(line.text)):
            close_q(cur_q)
            cur_q = None
            chapter = line.text.strip()
            continue

        no: int | None = None
        title = ""
        rest = ""
        m_q = QUESTION_RE.match(line.text)
        if m_q:
            no, title = int(m_q.group(1)), line.text.strip()
        elif board in REPLY_SOURCED:
            m_cn = QUESTION_CN_RE.match(line.text)
            if m_cn:
                num = cn_int(m_cn.group(1))
                if num is not None:
                    no, title = num, line.text.strip()
        if no is None and board in ("新三板", "创业板"):
            # 创业板回抄的问询函问题标题有三种写法，实测都出现过：
            #     问题1关于创业板定位 / 问题1.关于技术创新性和市场空间 / 1.关于行业发展和发行人业绩波动
            m2 = NSP_QUESTION_RE.match(line.text)
            if m2:
                no = int(m2.group(1))
                title = m2.group(1) + "." + m2.group(2)
                rest = (m2.group(3) or "").strip()
        if no is not None:
            # 先结算上一个问题。回复文档开头的【目录】会把全部问题标题列一遍，
            # 它们没有任何正文：这种必须丢掉，否则会把编号占住，
            # 让后面真正的问题因为"编号已出现过"而被误判成正文小标题。
            close_q(cur_q)
            cur_q = None
            if board in REPLY_SOURCED and no in seen_nos:
                # 编号已出现过 = 回复正文里的小标题（如「3、关于自然人的境外账户」），
                # 忽略它，继续待在回复模式
                continue
            cur_q = {
                "chapter": chapter,
                "no": no,
                "title": title,
                "page_start": line.page,
                "page_end": line.page,
                "body": [rest] if rest else [],
                "has_asker": bool(ASKER_RE.search(rest)),
            }
            answer_started = False
            continue

        # 没有"问题N"标题的回复文档：行首的裸"请发行人"就是新问题的开始。
        # 只在【还没开始】或【正处在上一题的回复正文里】时才认，
        # 免得把一道题内的「请发行人：…请保荐人…」拆成两道。
        if use_ask_start and (cur_q is None or answer_started) and ASK_START_RE.match(line.text):
            close_q(cur_q)
            synth_no += 1
            cur_q = {
                "chapter": chapter,
                "no": synth_no,
                "title": line.text[:60],
                "page_start": line.page,
                "page_end": line.page,
                "body": [line.text],
                "has_asker": True,
            }
            answer_started = False
            continue

        if cur_q is not None:
            if answer_started:
                continue
            if board in REPLY_SOURCED and ANSWER_MARK_RE.match(line.text):
                answer_started = True
                continue
            # 结构起点：一级标题「一、…」。加了 has_asker 这道闸：提问段里必然已经
            # 出现过「请发行人/请保荐人…」，如果一个"请"都还没出现就见到「一、」，
            # 那说明是还没进入提问主体（或这份文档用一级标题写提问），不能当成回复起点。
            if (board in REPLY_SOURCED and cur_q.get("has_asker")
                    and REPLY_BODY_RE.match(line.text)):
                answer_started = True
                continue
            if line.text in running_headers:
                continue
            cur_q["body"].append(line.text)
            if not cur_q.get("has_asker") and ASKER_RE.search(line.text):
                cur_q["has_asker"] = True
            cur_q["page_end"] = line.page
            # 收尾句：本行是完整的「…发表明确意见。」，则提问段到此为止（见 ASK_CLOSE_RE）。
            # 注意是本行【先收进来】再封口——这句本身属于提问段。
            if (board in REPLY_SOURCED and cur_q.get("has_asker")
                    and ASK_CLOSE_RE.search(line.text)):
                answer_started = True

    close_q(cur_q)

    # 少数问询函里同一个"问题N"会出现两次（分页重复标题、或章节被拆开），
    # 直接入库会撞 question_id 唯一约束。这里按问题号合并成一条。
    merged: list[dict] = []
    by_no: dict[int, dict] = {}
    for q in questions:
        if q["no"] in by_no:
            first = by_no[q["no"]]
            # 创业板回复的【目录页】也是黑体，会先冒出一批同样的问题标题，
            # 但它们没有正文。合并时把标题和页码让给真正有正文的那一份，
            # 否则 page_start 会停在目录页、标题也可能只是目录里的简写。
            if q["body"] and not first["body"]:
                first["page_start"] = q["page_start"]
                if q["title"]:
                    first["title"] = q["title"]
            first["body"].extend(q["body"])
            first["page_end"] = q["page_end"]
        else:
            by_no[q["no"]] = q
            merged.append(q)
    questions = merged

    letter.pages = pages
    letter.chapters = list(dict.fromkeys(q["chapter"] for q in questions if q["chapter"]))
    seq = 0
    for q in questions:
        letter.questions.append({
            "chapter": q["chapter"], "no": q["no"], "title": q["title"],
            "page_start": q["page_start"], "page_end": q["page_end"],
        })
        for sub in _extract_from_body("".join(q["body"]), q):
            seq += 1
            sub.seq = seq
            letter.queries.append(sub)
    return letter


def _extract_from_body(body: str, q: dict) -> list[Query]:
    subs = find_subheads(body)
    sub_starts = [s[0] for s in subs]

    def sub_title_at(pos: int) -> str:
        best = ""
        for start, _end, _no, title in subs:
            if start <= pos:
                best = title
            else:
                break
        return best

    marks = list(ASKER_RE.finditer(body))
    out: list[Query] = []
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(body)
        nxt = next((s for s in sub_starts if s > m.start()), None)
        if nxt is not None:
            end = min(end, nxt)
        mat = MATERIAL_RE.search(body, m.end(), end)
        if mat:
            end = mat.start()

        asker = m.group(0).rstrip("：:")
        seg = body[m.end():end].strip()
        if not seg:
            continue
        for item_no, raw in split_items(seg):
            raw = _clean(raw)
            if len(raw) < 6 or _is_fragment(raw):
                continue
            out.append(Query(
                seq=0,
                chapter=q["chapter"],
                question_no=q["no"],
                question=q["title"],
                sub_question=sub_title_at(m.start()),
                asker=asker,
                item_no=item_no,
                raw_text=raw,
                norm_strict=norm_strict(raw),
                norm_loose=norm_loose(raw),
                is_boilerplate=is_boilerplate(raw),
                page_start=q["page_start"],
                page_end=q["page_end"],
            ))
    return out


def scan_dir(dir_path: str, board: str = "北交所") -> list[Letter]:
    return [scan_letter(str(p), board=board)
            for p in sorted(Path(dir_path).glob("*.pdf"))]


def split_user_input(text: str) -> list[str]:
    """用户直接把自家问询函的一整段粘进来时，用同一套规则拆成多个最小 query。

    设计文档第 5 节只写了"搜一个问题"，但实际用法是整段粘贴，
    所以输入侧必须复用同一套切分器，粒度才对得齐。
    """
    marks = list(ASKER_RE.finditer(text))
    if not marks:
        items = [_clean(t) for _no, t in split_items(text)]
        items = [t for t in items if len(t) >= 6]
        return items or [text.strip()]

    subs = find_subheads(text)
    sub_starts = [s[0] for s in subs]
    out: list[str] = []
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        nxt = next((s for s in sub_starts if s > m.start()), None)
        if nxt is not None:
            end = min(end, nxt)
        mat = MATERIAL_RE.search(text, m.end(), end)
        if mat:
            end = mat.start()
        for _no, raw in split_items(text[m.end():end].strip()):
            raw = _clean(raw)
            if len(raw) >= 6:
                out.append(raw)
    return out or [text.strip()]
