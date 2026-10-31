# -*- coding: utf-8 -*-
"""第四种题号体例：深交所那种裸 `1．正文`（没有「关于」二字）。

2026-10-31 新增。起因：北交所/新三板之外，创业板/科创板/沪主板/深主板的"审核问询函
本身"从来没被测过（交易所对 IPO 只公开回复，函原文抄在回复里）。花了半天找到 5 份
**再融资**审核问询函，测出两个真问题：

  ① 深交所体例 `1．报告期各期，发行人…`，编号后面直接是正文，没有「关于」。
     `NSP_QUESTION_RE` 强制要求「关于」，于是整份 0 题。
     另外句点有三种字形：半角 . / 全角 ．(U+FF0E) / 顿号 、，一开始只认半角。
  ② 上交所体例 `1.关于战略投资者` 本来就认；但第 6 题只写 `6.其他`（两个字），
     子题写 `6.1/6.2/6.3`。原来的规则要么漏掉第 6 题，要么把 6.x 当成 3 道新题。

这些 assert 钉住的是"别为了救深交所把北交所/新三板弄坏"——后者靠 `问题N` 和
`N.关于` 本来就好，不该走兜底。真实语料的回归是在 16 份北交所/新三板 PDF 上做的
逐条比对（16/16 一致），这里只留下不依赖外部文件的构造用例。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.extract import BARE_NUM_RE, NSP_QUESTION_RE
from src.ingest import _find_heads, split_letter_text

CASES = []


def case(name):
    def deco(fn):
        CASES.append((name, fn))
        return fn
    return deco


# 深交所体例：编号 + 正文，没有「关于」，句点是全角 ．
SZ_LETTER = """深圳证券交易所
关于某某股份有限公司申请向特定对象发行股票的审核问询函
审核函〔2023〕120138号
某某股份有限公司：
根据《证券法》等有关规定，本所发行上市审核机构对申请文件进行了审核，并形成如下审核问询问题。
1．报告期各期，发行人归母净利润分别为100,145.96万元、-2,535.03万元，波动较大。
请发行人补充说明：（1）业绩下滑的原因及合理性；（2）是否存在持续下滑风险。
2．本次发行拟募集资金总额不超过300,000万元，用于补充流动资金。
请发行人说明募集资金规模的合理性。
3．根据国家统计局发布的《国民经济行业分类》，发行人所属行业为电力生产。
请发行人说明行业分类的依据。
"""

# 上交所体例：1.关于XXX，最后一题只写「6.其他」，子题是 6.1/6.2
SH_LETTER = """上海证券交易所文件
关于某某股份有限公司向特定对象发行股票申请文件的审核问询函
某某股份有限公司、某某证券股份有限公司：
本所审核机构对申请文件进行了审核，并形成了首轮问询问题。
1.关于战略投资者
根据申报材料，某投资方将作为战略投资者参与本次发行。
请发行人说明：（1）战略资源情况；（2）决策程序是否完备。
2.关于本次发行方案
请发行人说明认购资金来源及合法性。
3.关于本次募投项目
请发行人说明募投项目的必要性。
4.关于融资规模及效益测算
请发行人说明融资规模的合理性。
5.关于公司业务与经营情况
请发行人说明毛利率下滑的原因。
6.其他
6.1关于财务性投资
根据申报材料，公司其他流动资产为9,577.56万元。
请发行人说明是否属于财务性投资。
6.2.关于土地房产
请发行人说明无证房产的情况及影响。
"""


@case("深交所体例：全角句点、无「关于」也要认")
def _():
    assert BARE_NUM_RE.match("1．报告期各期，发行人归母净利润为100,145.96万元")
    heads = _find_heads(SZ_LETTER)
    assert len(heads) == 3, len(heads)


@case("上交所体例：「6.其他」两个字的题名不能漏")
def _():
    heads = _find_heads(SH_LETTER)
    # 1..5 + "6.其他" = 6 题；不是 1..5 + 6.1 + 6.2 = 7 题（子题不能当新题）
    assert len(heads) == 6, len(heads)


@case("子题 6.1/6.2 不单独成题")
def _():
    assert not BARE_NUM_RE.match("6.1关于财务性投资")     # 编号后面紧跟数字，挡住
    L = split_letter_text(SH_LETTER)
    assert len(L.questions) == 6, [q["title"] for q in L.questions]
    # 6.1/6.2 的内容落在第 6 题里
    tail = "".join(q.raw_text for q in L.queries if q.question_no == 6)
    assert "财务性投资" in tail and "土地房产" in tail, tail[:120]


@case("编号接不上就不认（正文里的金额、年份）")
def _():
    # "65.40%" 不是题号（数字后面紧跟 % 或数字，被挡）
    assert BARE_NUM_RE.match("65.40%、56.60%，占比逐年增加") is None
    assert BARE_NUM_RE.match("2．本次发行拟募集资金") is not None
    # 正文里插一行"65.…"，题数不变
    text = SZ_LETTER.replace("2．本次发行", "65.40%、56.60%，占比逐年增加。\n2．本次发行")
    assert len(_find_heads(text)) == 3, len(_find_heads(text))


@case("北交所体例不走兜底（避免正文数字抢戏）")
def _():
    # 有「问题N」标题时，裸编号不参与判定
    text = ("北京证券交易所\n关于某某公司的审核问询函\n"
            "问题1.关于股权结构\n请发行人说明股权代持情况。\n"
            "问题2.关于收入\n请发行人说明收入确认政策。\n"
            "问题3.关于毛利率\n请发行人说明毛利率变动原因。\n")
    L = split_letter_text(text)
    assert len(L.questions) == 3, len(L.questions)


@case("NSP 也要认全角句点")
def _():
    assert NSP_QUESTION_RE.match("1．关于战略投资者")
    assert NSP_QUESTION_RE.match("1.关于战略投资者")


def main() -> int:
    bad = 0
    for name, fn in CASES:
        try:
            fn()
            print("ok   " + name)
        except AssertionError as e:
            bad += 1
            print("FAIL " + name + "  " + str(e)[:180])
    print("\n%d/%d 通过" % (len(CASES) - bad, len(CASES)))
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
