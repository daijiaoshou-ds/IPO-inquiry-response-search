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

# 纯"核查并发表明确意见"这类不是要回答的问题，只是程序性要求。
# 库里也存在（scan_letter 不收它们），指引里更不该出现——检索它只会捞出一堆
# 同样的模板话。只在 docx / 文本这条路上过滤，不动 scan_letter。
BOILER_RE = re.compile(
    r"^(?:请[^：:]{0,12}[：:])?\s*(?:补充)?核查(?:上述|以下)?(?:事项)?[，,、]?\s*"
    r"(?:并)?(?:发表)?明确意见|^(?:补充)?核查上述事项$|^请[^：:]{0,12}核查并发表明确意见$"
    r"|^(?:并)?(?:再次)?(?:审慎)?(?:发表)?明确意见[。；]?$")

# 函尾的程序性段落（不是问题）："除上述问题外…""请你们在20个交易日内…""特此函告"
TAIL_RE = re.compile(r"除上述问题外|个交易日内|特此函告|专此函达|请审慎|电子版材料")

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


def split_letter_text(text: str) -> SimpleNamespace:
    """纯文本 -> {questions, queries}。先切题，再每题内部复用 split_user_input。"""
    from .extract import split_user_input

    heads = [m.start() for m in HEAD_RE.finditer(text)]
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
                if len(x) >= MIN_SUB and not BOILER_RE.match(x) and not TAIL_RE.search(x)]
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
