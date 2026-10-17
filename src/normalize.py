# -*- coding: utf-8 -*-
"""query 归一化与通用短语清洗。

三层：
  norm_strict —— 统一全半角、去空白、去标点，**保留数字**（精确召回 + 高亮定位）
  norm_loose  —— 在 strict 基础上再去数字（宽召回）
  norm_core   —— 在 loose 基础上再清掉"通用法规/准则引用"（**打分用**）

为什么要有 norm_core：
  "是否符合《企业会计准则》的规定" 这类表达对"找同款问题"没有任何区分度，
  却会实实在在推高两条无关问题的相似度。清掉之后，剩下的才是真正的问题特征。

另外，高频通用词由 IDF 自动降权（见 search.py），不必手工穷举。
"""
from __future__ import annotations

import re
import unicodedata

_PUNCT = "，。、；：？！“”‘’（）《》,.;:?!()[]【】<>}{·—–-…／/\\|~@#$^&*_+=\"'"
_PUNCT_SET = set(_PUNCT)
_DIGIT_RE = re.compile(r"[0-9０-９]")


def to_halfwidth(text: str) -> str:
    """全角转半角（保留中文）。"""
    out = []
    for ch in text:
        code = ord(ch)
        if code == 0x3000:
            out.append(" ")
        elif 0xFF01 <= code <= 0xFF5E:
            out.append(chr(code - 0xFEE0))
        else:
            out.append(ch)
    return "".join(out)


# ---------------------------------------------------------------- 基础归一化

def _tokenize(text: str, keep_digits: bool) -> tuple[str, list[int]]:
    """归一化，并保留"归一化后每个字符来自原文哪个下标"的映射（高亮要用）。"""
    text = unicodedata.normalize("NFKC", text)
    text = to_halfwidth(text)
    chars: list[str] = []
    idx: list[int] = []
    for i, ch in enumerate(text):
        if ch.isspace() or ch in _PUNCT_SET:
            continue
        if not keep_digits and _DIGIT_RE.match(ch):
            continue
        chars.append(ch)
        idx.append(i)
    return "".join(chars), idx


def norm_strict(text: str) -> str:
    return _tokenize(text, keep_digits=True)[0]


def norm_loose(text: str) -> str:
    return _tokenize(text, keep_digits=False)[0]


# ---------------------------------------------------------------- 通用短语清洗
# 注意：这些正则作用在 **loose 文本**上，标点已经被清掉了，
# 所以模式里不要写《》、逗号之类。

GENERIC_RE = re.compile(
    r"(?:"
    r"企业会计准则(?:第[一二三四五六七八九十百零]+号)?"
    r"|中华人民共和国(?:公司法|证券法|会计法|民法典|注册会计师法)"
    r"|公开发行证券的公司信息披露内容与格式准则第[一二三四五六七八九十百零]+号"
    r"|北京证券交易所向不特定合格投资者公开发行股票(?:并上市)?"
    r"(?:注册管理办法|业务规则适用指引第[一二三四五六七八九十百零]+号)?"
    r"|公司法|证券法"
    r")"
)

GENERIC_PHRASES = (
    "是否符合相关法律法规的规定", "是否符合相关法律法规规定",
    "是否有违相关法律法规的规定",
    "是否符合相关规定", "是否符合规定", "是否符合的规定",
    "符合相关法律法规的规定", "符合相关规定", "符合规定",
)


def _strip_generic(s: str, idx: list[int]) -> tuple[str, list[int]]:
    if not s:
        return s, idx
    mask = [True] * len(s)
    for m in GENERIC_RE.finditer(s):
        for i in range(m.start(), m.end()):
            mask[i] = False
    for p in GENERIC_PHRASES:
        start = s.find(p)
        while start != -1:
            for i in range(start, start + len(p)):
                mask[i] = False
            start = s.find(p, start + 1)
    if all(mask):
        return s, idx
    return ("".join(c for c, k in zip(s, mask) if k),
            [i for i, k in zip(idx, mask) if k])


def norm_core_map(text: str) -> tuple[str, list[int]]:
    """打分用的归一化文本 + 原文下标映射。"""
    s, idx = _tokenize(text, keep_digits=False)
    return _strip_generic(s, idx)


def norm_core(text: str) -> str:
    return norm_core_map(text)[0]


# ---------------------------------------------------------------- 套话判定

_BOILERPLATE_CORES = [
    "核查上述事项并发表明确意见",
    "核查并发表明确意见",
    "发表明确意见",
    "完善招股说明书信息披露",
    "按照相关规定补充披露",
]


def is_boilerplate(raw: str, slack: int = 20) -> bool:
    """跨公司会 100% 命中、几乎没有信息量的提问。

    判定：正文较短（不超过套话本身 + slack 字）且套话出现在其中。
    这样 "律师核查上述事项并发表明确意见" 也能被识别，
    而 "结合……说明……并发表明确意见" 这种长问题不会被误杀。
    """
    t = norm_strict(raw)
    if len(t) < 12:
        return True
    for core in _BOILERPLATE_CORES:
        c = norm_strict(core)
        if c in t and len(t) <= len(c) + slack:
            return True
    return False
