# -*- coding: utf-8 -*-
"""split_clauses（按 ；。 再切一层）的回归测试。

2026-10-30 新增：编号只切到"一层小问"，一层小问里还并着几个并列问句，
监管用分号隔开。库里 27.9 万条中有 5.4 万条能切，切完总数 +26%。

这些 assert 是钉住"切得对"和"不丢内容"两条。改判据前先看这里为什么这么写。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.extract import split_clauses, split_user_input

CASES = []


def case(name):
    def deco(fn):
        CASES.append((name, fn))
        return fn
    return deco


@case("分号要切")
def _():
    t = ("发行人是否与OEM厂商间保持合作及具体情况，能否直接向OEM厂商采购维修的备件"
         "及核心零部件；报告期内公司直接或者间接向OEM厂商采购的金额、种类及占同类"
         "物品的比重、上述原材料主要发挥的作用，是否属于必须向原厂采购的原材料及原因")
    out = split_clauses(t)
    assert len(out) == 2, out
    assert out[0].startswith("发行人是否与OEM厂商")
    assert out[1].startswith("报告期内公司直接或者间接")


@case("句号要切")
def _():
    t = ("公司产品的工艺及制程情况，是否与国内主流工艺保持一致，如存在差异，请说明原因。"
         "公司是否具有第四代封装工艺技术，未采用第三、四代封装技术工艺的具体原因，"
         "是否可能影响产品性能，结合前述情况及募投项目情况，分析公司是否具有技术先进性")
    out = split_clauses(t)
    assert len(out) == 2, out
    assert "请说明原因" in out[0]
    assert out[1].startswith("公司是否具有第四代")


@case("单句不切")
def _():
    assert split_clauses("说明研发人员认定标准及划分依据") == []
    assert split_clauses("") == []


@case("短片段并入相邻段，不丢内容")
def _():
    t = "结合存货库龄结构说明跌价准备是否充分；是否存在跨期确认收入的情形；相关内部控制是否健全有效"
    out = split_clauses(t)
    assert len(out) == 3, out
    # 每个字都还在（只多了合并用的逗号）
    joined = "".join(out).replace("，", "")
    assert joined == t.replace("；", ""), (joined, t)
    # 极短的一段不会单独成条
    t2 = "说明本次发行的必要性；请说明原因"
    out2 = split_clauses(t2)
    assert len(out2) <= 1 or all(len(x) >= 8 for x in out2), out2


@case("题号标题不单独成条")
def _():
    # "2.关于毛利率"是下一题的标题，不是问句
    t = "核查并发表明确意见。2.关于毛利率"
    out = split_clauses(t)
    assert all("关于毛利率" not in x for x in out), out


@case("与编号拆分叠加：两层都切")
def _():
    t = ("1.关于收入确认。请发行人说明收入确认时点及依据；是否存在跨期确认收入的情形。"
         "请保荐机构核查并发表明确意见。2.关于毛利率。请发行人结合产品结构分析毛利率"
         "差异的原因及合理性。")
    out = split_user_input(t)
    assert len(out) == 3, out
    assert out[0] == "说明收入确认时点及依据"
    assert out[1] == "是否存在跨期确认收入的情形"
    assert out[2].startswith("结合产品结构")
    # 程序性要求不进输入侧（库侧保留但打 is_boilerplate 标记）
    assert all("核查并发表明确意见" not in x for x in out), out


@case("切完不丢内容")
def _():
    t = ("说明报告期内各期毛利率变动的原因；结合产品结构、单价及单位成本变动，"
         "量化分析毛利率与同行业可比公司的差异；说明毛利率变动趋势是否与可比公司一致。")
    out = split_clauses(t)
    assert len(out) == 3, out
    # 只允许少掉分隔标点、多出合并用的逗号，其余一个字都不能少
    assert ("".join(out).replace("，", "") ==
            t.replace("；", "").replace("。", "").replace("，", "")), out


def main() -> int:
    bad = 0
    for name, fn in CASES:
        try:
            fn()
            print("ok   " + name)
        except AssertionError as e:
            bad += 1
            print("FAIL " + name + "  " + str(e)[:160])
    print("\n%d/%d 通过" % (len(CASES) - bad, len(CASES)))
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
