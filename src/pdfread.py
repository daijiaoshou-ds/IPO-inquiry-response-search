# -*- coding: utf-8 -*-
"""把 PDF 还原成"行"序列。

不同公司的问询函字体完全不一样（实测：前景无忧用 WenQuanYiZenHeiSharp，
昌誉密封用 SimHei，济南澳图 / 玄机科技只有 FangSong），所以**不能依赖字体名**。
这里只负责把字符按坐标还原成行，结构判断交给 extract.py。
"""
from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from typing import Callable, Iterator

import pdfplumber

# 页脚页码：纯数字/带横线的短行
PAGE_NUM_RE = re.compile(r"^[\d\s\-—]{1,10}$")
# 目录行的点线引导符
TOC_RE = re.compile(r"\.{5,}")

# ---------------------------------------------------------------- 字体名归一化
# 背景：创业板要从【回复】里把问询函的提问段抄回来（深交所不单独公开问询函，
# 见 extract.REPLY_SOURCED）。一开始想用字体切——回复文档里通常写着
# 「黑体（加粗）问询函所列问题 / 宋体回复」。
#
# **结论是字体不可靠，最后没用它**：实测埃索凯 r1 的问题基本是 SimHei 但
# "问题26"是 SimSun；毅合捷 r2 更极端，只有问题1是 SimHei、问题2~9 全是
# SimSun（和回复正文同字体）。改成按【回复正文的起点标记】切（extract.ANSWER_MARK_RE）。
#
# 这套归一化逻辑保留下来给 want_font 用（备用手段），顺带也是个记录：
# 字体【名】本身就不可靠，同一个约定至少三种写法：
#     SimHei / SimSun
#     黑体 / 宋体          <- 而且 pdfplumber 有时把它们给成 bytes 或 bytes 的 str repr
# 所以必须先归一化再比。
_SUBSET_RE = re.compile(r"^[A-Z]{6}\+")
_HEI_RE = re.compile(r"(?i)hei|黑")


def norm_font(name) -> str:
    """字体名归一化：剥掉子集前缀、把 bytes / bytes 的 str-repr 解成中文名。

    实测三种形态都要处理：
        "BCDFEE+SimHei"            -> "SimHei"
        b"ABCDEE+\xba\xda\xcc\xe5" -> "黑体"      （GBK 字节）
        "b'ABCDEE+\\xba\\xda\\xcc\\xe5'" -> "黑体"  （bytes 的字符串表示）
    """
    raw = name
    if isinstance(name, str) and len(name) > 3 and name[0] == "b" and name[1] in "'\"":
        try:
            name = ast.literal_eval(name)
        except (ValueError, SyntaxError):
            pass
    if isinstance(name, (bytes, bytearray)):
        for enc in ("gbk", "utf-8", "latin-1"):
            try:
                name = bytes(name).decode(enc)
                break
            except UnicodeDecodeError:
                continue
        else:
            name = str(raw)
    return _SUBSET_RE.sub("", str(name or ""))


def is_hei(name) -> bool:
    """是不是"黑体"系（= 问询函原文那一档）。"""
    return bool(_HEI_RE.search(norm_font(name)))


@dataclass
class Line:
    page: int
    top: float
    text: str
    font: str

    @property
    def is_junk(self) -> bool:
        return bool(PAGE_NUM_RE.match(self.text) or TOC_RE.search(self.text))


def _page_lines(page_no: int, page, tol: float = 3.0) -> list[Line]:
    """按 top 容差把字符聚成行。"""
    rows: list[dict] = []
    for ch in sorted(page.chars, key=lambda c: (c["top"], c["x0"])):
        if not ch.get("text"):
            continue
        for row in rows:
            if abs(row["top"] - ch["top"]) <= tol:
                row["chars"].append(ch)
                row["top"] = min(row["top"], ch["top"])
                break
        else:
            rows.append({"top": ch["top"], "chars": [ch]})

    out: list[Line] = []
    for row in rows:
        cs = sorted(row["chars"], key=lambda c: c["x0"])
        text = "".join(c["text"] for c in cs).strip()
        if not text:
            continue
        fonts: dict[str, int] = {}
        for c in cs:
            fn = norm_font(c["fontname"])
            fonts[fn] = fonts.get(fn, 0) + 1
        out.append(Line(page_no, row["top"], text, max(fonts, key=fonts.get)))
    return sorted(out, key=lambda l: l.top)


def iter_lines(pdf_path: str, skip_junk: bool = True,
               want_font: Callable[[str], bool] | None = None) -> Iterator[Line]:
    """按行遍历。want_font 给定时只产出"该行主字体满足条件"的行。

    want_font 目前没人默认启用（字体方案被实测否决，见文件开头的说明），
    留着是因为它在个别文档上确实能把问题段切得更干净，可以当兜底手段。

    **每页处理完必须 page.close()。** pdfplumber 的 `pdf.pages` 会把每一页的
    Page 对象（以及它缓存下来的全部字符对象）一直留在内存里，内存随页数线性涨：
    实测 919 页 / 16 MB 的一份沪主板回复，解析峰值 2.56 GB（约 2.8 MB/页），
    而且结束后还不释放。10 个进程一起跑就能把机器内存吃光（2026-10 实际发生过，
    机器卡死后强制重启）。close() 会把该页的 _objects / textmap 缓存清掉，
    峰值降到跟单页一个量级。
    """
    with pdfplumber.open(pdf_path) as pdf:
        pages = pdf.pages
        for pno, page in enumerate(pages, 1):
            try:
                lines = _page_lines(pno, page)
            finally:
                # 无论这一页解析成功与否都要清缓存，否则一份坏页就把整份文档钉在内存里
                page.close()
            for line in lines:
                if skip_junk and line.is_junk:
                    continue
                if want_font is not None and not want_font(line.font):
                    continue
                yield line


def page_count(pdf_path: str) -> int:
    with pdfplumber.open(pdf_path) as pdf:
        return len(pdf.pages)
