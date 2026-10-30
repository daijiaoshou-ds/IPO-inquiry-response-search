"""把 extract.split_items 的切分行为钉住。

起因（2026-10-30）：库里有 200~900 字的长 query，是若干小问被并成了一条。
根因是 ITEM_RE 只认全角 `（1）`，不认半角 `(1)`——泰凌微电子、贝克制药、
华曙高科技这些用英文括号写编号的函，整段都没被切开（实测 213 条）。
这批已经用 scripts/split_merged.py 原地拆了，但规则漏了还会再犯，所以加这个测试。

跑：python tests/test_split_items.py
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.extract import split_items  # noqa: E402


def _nos(seg: str) -> list[str]:
    return [no for no, _t in split_items(seg)]


def _texts(seg: str) -> list[str]:
    return [t for _no, t in split_items(seg)]


# ---------------------------------------------------------------- 要拆的体例

def test_半角括号要拆() -> None:
    """英文括号 (1)(2)——本次修的就是它。"""
    seg = "(1)经销商收入确认政策；(2)前五大经销商毛利率差异原因；(3)经销商回款情况"
    assert len(split_items(seg)) == 3, split_items(seg)
    assert _texts(seg)[0].startswith("经销商收入确认政策")


def test_全角括号要拆() -> None:
    """老版本就支持的体例，不能被新规则改坏。"""
    seg = "（1）说明收入确认政策；（2）说明毛利率变动原因；（3）说明回款情况"
    assert len(split_items(seg)) == 3, split_items(seg)


def test_圆圈数字要拆() -> None:
    seg = "①说明收入确认政策；②说明毛利率变动原因"
    assert len(split_items(seg)) == 2, split_items(seg)


def test_混用括号也要拆() -> None:
    """一份函里全角半角混着写的情况确实存在。"""
    seg = "（1）说明收入确认政策；(2)说明毛利率变动原因"
    assert len(split_items(seg)) == 2, split_items(seg)


# ---------------------------------------------------------------- 不能拆的

def test_中文括号不拆() -> None:
    """（一）（二）是回复正文的编号，靠它区分问与答，绝不能当小问切。

    误拆的后果不是"多几条"，而是把回复内容当成提问塞进库里。
    """
    seg = "（一）核查程序；（二）核查意见"
    assert split_items(seg) == [("", seg)], split_items(seg)


def test_金额不拆() -> None:
    """括号里带单位、带千分位、带小数的都不是编号。"""
    for seg in ["发行人2020年营业收入(万元)为11,145.44元，较上年增长(5.5)%",
                "报告期内毛利率分别为(1,234.56)万元和(2.5)%",
                "公司注册资本为5,500,000,000.00美元"]:
        assert len(split_items(seg)) == 1, (seg, split_items(seg))


def test_年份括号不拆() -> None:
    assert len(split_items("公司于2019年(2020年)完成改制")) == 1


def test_单个编号不拆() -> None:
    """只有一个 (1)，没有 (2)，那是引用而不是分项。"""
    seg = "具体内容详见(1)招股说明书之“业务与技术”章节"
    assert len(split_items(seg)) == 1, split_items(seg)


def test_没有编号不拆() -> None:
    seg = "请发行人说明报告期内毛利率变动的原因及合理性"
    assert len(split_items(seg)) == 1


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("ok   %s" % name[5:])
            except AssertionError as e:
                fails += 1
                print("FAIL %s  %s" % (name[5:], e))
    print("\n%s" % ("全部通过" if not fails else "失败 %d 项" % fails))
    sys.exit(1 if fails else 0)
