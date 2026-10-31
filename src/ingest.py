# -*- coding: utf-8 -*-
"""一份问询函 -> (题, 小问)。网页上传和命令行指引共用这一层。

三种输入，两条路径：
    PDF   -> src.extract.scan_letter（已在 4306 份函上跑过，别动）
    docx  -> python-docx 取段落 -> 按题号切题 -> 每题内部复用 split_user_input
    文本  -> 同 docx（差一步读文件）

为什么 docx 不直接调 scan_letter：那是吃 PDF 行结构的，Word 段落本身就是
自然句，硬塞进去反而要重新拼行。

实测（昊辉新材料 那份 6 题 docx，7158 字）：拆题 + 拆小问 共 0.6 ms。
慢不在这，在冷启动的引擎启动与 IDF 表加载（见 docs/内部工作流.md §11）。
"""
from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

# 题标题：三种体例都见过
#   问题1. 关于…        北交所/新三板 常见
#   1.关于业务合规性。    昊辉这类（题号 + 关于）
#   一、关于…            少数老文档
HEAD_RE = re.compile(
    r"(?m)^\s*(?:问题\s*\d{1,3}\s*[\.、]?"
    r"|\d{1,3}\s*[\.、]\s*关于"
    r"|[一二三四五六七八九十]{1,3}、\s*关于)"
)

# 第四种体例：题号是 Word 的**自动编号**，`paragraph.text` 里根本读不到。
# 裕富照明那份北交所函就是这样——正文里只有「股权结构清晰稳定性」这一行标题，
# 「问题1.」只出现在目录里（目录域还带着 TAB + 页码）。
# 判据：短行、不含标点、下一行是正文。
#
# 起初只认「下一行以 根据/请/（1） 开头」，结果被库里的真实分布推翻：
# 57,670 个问题节里，"一题第一段"的开头是 补充披露 3339 / 报告期 2442 /
# 说明报告 2143 / 发行人 1673 / 补充说明 1245 / 结合发行 985 / 按照《 814…
# 按开头词判会漏掉一大半体例。改成"下一行足够长（≥30 字）"这个与用词无关的
# 信号——标题就一行短句，正文段落必然长，这条对哪个交易所都成立。
BODY_START_RE = re.compile(r"^(?:根据|依据|截至|请|报告期|[（(]\s*\d{1,2}\s*[）)])")
BODY_LONG = 30
TITLE_MAX = 30
TITLE_BAD = re.compile(r"[，。；：,;:（）()]")
# 正文里也会出现短行（"上述事项"这种），靠这几个开头词排掉，避免把段落头当题目
TITLE_STOP = re.compile(r"^(?:上述|如下|其中|因此|据此|综上|此外|同时|例如|具体|另|"
                        r"请|根据|依据|截至)")

# Word 目录域导出成文本是「标题<TAB>页码」。不剔掉的话，目录里的「问题1.」
# 会被当成正文题目，一份 11 题的函能切出 22 题（前 11 题还是空的）。
TOC_RE = re.compile(r"\t\s*\d{1,4}\s*$")

# 纯"核查并发表明确意见"这类不是要回答的问题，只是程序性要求。
# 库里也存在（scan_letter 不收它们），指引里更不该出现——检索它只会捞出一堆
# 同样的模板话。只在 docx / 文本这条路上过滤，不动 scan_letter。
#
# 最后一支是「并发表明确意见，说明核查方法、范围、依据及结论。」这种：
# 它由 split_user_input 从「请保荐机构…核查并发表明确意见」里拆出来，
# 前面挂着逗号，不是句首的「请…核查」，前两支都拦不住（裕富照明题1 的第 13 小问）。
BOILER_RE = re.compile(
    r"^(?:请[^：:]{0,12}[：:])?\s*(?:补充)?核查(?:上述|以下)?(?:事项)?[，,、]?\s*"
    r"(?:并)?(?:发表)?明确意见|^(?:补充)?核查上述事项$|^请[^：:]{0,12}核查并发表明确意见$"
    r"|^[，,、；;]?\s*(?:并)?(?:再次)?(?:审慎)?(?:发表)?明确意见")

# 函尾的程序性段落（不是问题）："除上述问题外…""请你们在20个交易日内…""特此函告"
TAIL_RE = re.compile(r"除上述问题外|个交易日内|特此函告|专此函达|请审慎|电子版材料")

# 「结合资金流水情况核查上述事项并发表明确意见」这类：以"核查…发表明确意见"
# 收尾的短句，是程序性要求而不是要回答的问题。BOILER_RE 只认句首，拦不住它
# （前面挂着"结合…"），所以单独用一条"短句 + 含该结构"的判据。
OPINION_RE = re.compile(r"核查.{0,25}(?:并)?发表明确意见")
# 「说明核查方式、核查范围、核查结论及依据」——纯模板，几乎没有公司特异性，
# 检索它只会返回一堆同样的句子。同样是短句才判。
CHECK_WAY_RE = re.compile(r"^说明核查(?:方式|程序|方法|范围|过程|结论|意见)")
OPINION_MAX = 45
CHECK_WAY_MAX = 32

MIN_SUB = 10          # 短于这个字数的碎片不要（"并发表明确意见" 这类）

ALLOWED = {".pdf", ".docx", ".txt", ".md", ".markdown"}
# .doc（97-2003 二进制）python-docx 读不了，需要 Word/LibreOffice 转换
UNSUPPORTED = {".doc": "老版 .doc 读不了，请用 Word 另存为 .docx，或直接导成 PDF"}


def read_docx_text(path: str | Path) -> str:
    """Word 转纯文本。段落按顺序拼，表格里的文字也收（有时整道题排成表格）。"""
    try:
        import docx
    except ImportError:
        raise ValueError("这个环境没装 python-docx，读不了 .docx；"
                         "可以装一下，或把文件另存为 PDF / txt 再上传")

    d = docx.Document(str(path))
    lines = [p.text.strip() for p in d.paragraphs if p.text.strip()]
    for tb in d.tables:
        for row in tb.rows:
            for cell in row.cells:
                t = cell.text.strip()
                if t:
                    lines.append(t)
    return "\n".join(lines)


# 深交所体例的审核问询函：`1．报告期各期，发行人…`，编号后面直接是正文，
# 没有「关于」二字。跟 PDF 路径同一套判据（见 extract.py 的 BARE_NUM_RE）：
# 只在"①②两条规则一个都没认出来 + 文首确实写着审核问询函"时启用，
# 且编号必须接得上 1,2,3…，否则正文里的金额、百分比会被当成题号。
BARE_HEAD_RE = re.compile(r"^(\d{1,3})\s*[.．、]\s*(?![\d%％，。；、])(\S{1,60})$")
_HEAD_NO_RE = re.compile(r"^\s*(?:(?:问询)?问题\s*(\d{1,3})|(\d{1,3})\s*[.．、]|第\s*(\d{1,3})\s*题)")


def _head_no(s: str) -> int | None:
    """从题头行里取题号（三种写法任取其一）。"""
    m = _HEAD_NO_RE.match(s)
    if not m:
        return None
    for g in m.groups():
        if g:
            return int(g)
    return None
LETTER_MARK_RE = re.compile(r"审核问询问题|问询问题|审核问询函")


def _find_heads(text: str) -> list[int]:
    """题目起始位置（字符偏移）。

    三条规则，按优先级：
      ① 带题号的（问题1. / 1.关于… / 一、关于…）—— 命中就用它
      ② 题号被 Word 自动编号吃掉的：短标题行 + 下一行是正文开头。
         **只在 ① 一个都没认出来时启用**：① 已经认到题头，再拿这条去补，
         会把正文里的折行（"中用于…"）和子题（"6.1关于…"）也当成新题。
      ③ 题号接力：深交所那种裸 `1．正文`（没有"关于"）。编号必须接上一题 +1，
         且该号没被 ① 占过——否则正文里以数字开头的句子会被当成新题。
    """
    lines = text.split("\n")
    offs, pos = [], 0
    for ln in lines:
        offs.append(pos)
        pos += len(ln) + 1

    # ① 的题号集合，给 ③ 做"这个号已经有人占了"的判据；
    # n_explicit 决定 ② 要不要启用（必须在循环外算一次，循环里 `not heads`
    # 会在加进第一个题头之后变假，裕富那份因此从 11 题退化成 1 题）
    head_nos = set()
    n_explicit = 0
    for ln in lines:
        s = ln.strip()
        if s and HEAD_RE.match(s):
            n_explicit += 1
            n = _head_no(s)
            if n is not None:
                head_nos.add(n)
    loose = n_explicit == 0

    heads = []
    last = 0
    for i, ln in enumerate(lines):
        s = ln.strip()
        if not s:
            continue
        if HEAD_RE.match(s):
            heads.append(offs[i])
            n = _head_no(s)
            if n is not None:
                last = n
            continue
        m_bare = BARE_HEAD_RE.match(s)
        if (m_bare and LETTER_MARK_RE.search(text[:4000])
                and int(m_bare.group(1)) == last + 1
                and int(m_bare.group(1)) not in head_nos):
            heads.append(offs[i])
            last += 1
            continue
        if (loose
                and 2 <= len(s) <= TITLE_MAX and not TITLE_BAD.search(s)
                and not TITLE_STOP.match(s) and i + 1 < len(lines)):
            nxt = lines[i + 1].strip()
            if BODY_START_RE.match(nxt) or len(nxt) >= BODY_LONG:
                heads.append(offs[i])
    return heads


def split_letter_text(text: str) -> SimpleNamespace:
    """纯文本 -> {questions, queries}。先切题，再每题内部复用 split_user_input。"""
    from .extract import split_user_input

    # 先剔目录行，再切
    lines = [ln for ln in text.split("\n") if ln.strip()]
    text = "\n".join(ln for ln in lines if not TOC_RE.search(ln))

    heads = _find_heads(text)
    if len(heads) >= 2:
        chunks = [(heads[i], heads[i + 1] if i + 1 < len(heads) else len(text))
                  for i in range(len(heads))]
    else:
        # 一个题号都没认出来：退化成一整题，不再硬猜，但要让上层知道
        chunks = [(0, len(text))]

    questions, queries = [], []
    for idx, (s, e) in enumerate(chunks, 1):
        chunk = text[s:e].strip()
        if not chunk:
            continue
        first = chunk.split("\n", 1)[0]
        # 标题只留到第一个句号：`5.关于固定资产与在建工程。根据申请文件，…`
        # 整段都是首段，直接取整段会把正文也当成标题
        title = re.split(r"[。：:]", first, 1)[0]
        title = re.sub(r"^\s*(?:问题\s*\d{1,3}|\d{1,3})\s*[\.、]?\s*", "", title).strip()
        title = title[:60]
        questions.append({"no": idx, "title": title, "page_start": 0, "page_end": 0})

        subs = split_user_input(chunk)
        subs = [x for x in subs
                if len(x) >= MIN_SUB and not BOILER_RE.match(x) and not TAIL_RE.search(x)
                and not (len(x) < OPINION_MAX and OPINION_RE.search(x))
                and not (len(x) < CHECK_WAY_MAX and CHECK_WAY_RE.match(x))]
        # 一段都拆不出来（整题就一句话，或格式怪异）：整段当一个小问
        if not subs:
            body = chunk[len(first):].strip() if len(chunk) > len(first) else chunk
            body = body or chunk
            if len(body) >= MIN_SUB and not BOILER_RE.match(body) and not TAIL_RE.search(body):
                subs = [body]
        for k, s in enumerate(subs, 1):
            queries.append(SimpleNamespace(
                question_no=idx, item_no=k, raw_text=s, sub_question=s,
                question=title, page_start=0, page_end=0, is_boilerplate=False))

    return SimpleNamespace(company=None, questions=questions, queries=queries,
                           pages=0, kind="text")


def load_file(path: str | Path, board: str = "") -> SimpleNamespace:
    """按扩展名分派。PDF 走 scan_letter，其余走文本拆分。"""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(str(p))
    suf = p.suffix.lower()
    if suf in UNSUPPORTED:
        raise ValueError(UNSUPPORTED[suf])
    if suf not in ALLOWED:
        raise ValueError("不支持的格式 %s（支持 %s）" % (
            suf or "无扩展名", "、".join(sorted(ALLOWED))))

    if suf == ".pdf":
        from .extract import scan_letter

        # 默认按「问询函原文」解析。用户上传的永远是自己刚收到、还没回复的函，
        # 不是别人的回复——所以不需要去猜体裁。
        #
        # 但题号写法有两套，抽取器按 board 分支：
        #   「问题1.关于…」   北交所等，board=问询函 就能认
        #   「1.关于销售收入。」NSP_QUESTION_RE，原代码只在 board=新三板 时启用
        # 所以先按问询函解析，一条都没拆出来时再用新三板那套试一次。只重试一次，
        # 不动 extract.py 里的核心分支（那会牵动 4306 份已被验证的函）。
        letter = scan_letter(str(p), board=board or "问询函")
        if board or letter.queries:
            letter.kind = "pdf"
            return letter
        retry = scan_letter(str(p), board="新三板")
        if retry.queries:
            retry.kind = "pdf"
            return retry
        letter.kind = "pdf"
        return letter

    text = read_docx_text(p) if suf == ".docx" else p.read_text(encoding="utf-8")
    letter = split_letter_text(text)
    letter.kind = "docx" if suf == ".docx" else "text"
    return letter


def load_bytes(data: bytes, filename: str, board: str = "") -> SimpleNamespace:
    """网页上传用：内存里的字节 -> letter。落一次临时文件，省得再写一套 PDF 入口。"""
    import os
    import tempfile

    suf = Path(filename).suffix.lower()
    if suf in UNSUPPORTED:
        raise ValueError(UNSUPPORTED[suf])
    if suf not in ALLOWED:
        raise ValueError("不支持的格式 %s（支持 %s）" % (
            suf or "无扩展名", "、".join(sorted(ALLOWED))))
    fd, tmp = tempfile.mkstemp(suffix=suf)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        return load_file(tmp, board=board)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
