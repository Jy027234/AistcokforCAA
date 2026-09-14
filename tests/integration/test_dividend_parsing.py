"""用**真实公告正文**验证分红解析（§12.7）。

为什么不用合成正文
------------------
分红公告的写法千变万化，而且错法都很隐蔽：
把"每 10 股 5.96 元"当成"每股 5.96 元"，金额差 10 倍，账面却完全自洽；
把"利润分配方案"当成已确定的实施公告，就会按一个还没经股东会审议的
方案提前记账。合成正文只会用我自己想得到的写法，测不出这些。

因此这里离线跑：正文固化在 `tests/fixtures/cninfo/*.txt`，
它们是从巨潮公告 PDF 用 pypdf 抽出来的真实文本。
**离线是可复现的前提**：依赖网络的测试会在网络抖动时给出
与代码无关的结论，而这类测试的结论恰恰是"能不能记账"。

重新生成 fixture：
    python tools/refresh_cninfo_fixtures.py
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from aquant.adapters.providers.cninfo import parse_cash_dividend  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "cninfo"


def load(name: str) -> tuple[str, str]:
    """返回 (正文, 标题)。文件首行是标题。"""

    path = FIXTURES / (name + ".txt")
    if not path.exists():
        pytest.skip("缺少 fixture " + str(path) + "；先运行 tools/refresh_cninfo_fixtures.py")
    lines = path.read_text(encoding="utf-8").splitlines()
    title = lines[0].removeprefix("# title: ").strip()
    return "\n".join(lines[1:]), title


def parse(name: str):
    text, title = load(name)
    announced = date.fromisoformat(name[:10])
    return parse_cash_dividend(text=text, title=title,
                               announcement_id=name.split("-")[-1],
                               announced_on=announced)


# =============================== 表格形态 + 非整数分金额（贵州茅台 2025 年度）
def test_maotai_table_form_and_sub_cent_amount():
    """茅台：金额是 28.02423 元（非整数分），日期在表格里。

    这条同时钉住两件事：
      1. 表格形态（表头给列名、下一段给值）必须能解析；
      2. 金额必须是**微元精确**的 28024230，而不是被舍成 2802423 分。
    """

    result = parse("2026-06-22-1225379934")

    assert result.status == "OK", result.notes
    assert result.cash_per_share_micros == 28_024_230, result.cash_per_share_micros
    # 28.02423 元 = 2802.423 分：**不能**整除到分，所以分值必须为 None，
    # 而不是四舍五入出来的 2802。
    assert result.per_share_cents_exact is None, result.per_share_cents_exact
    assert result.record_date == date(2026, 6, 25)
    assert result.ex_date == date(2026, 6, 26)
    assert result.pay_date == date(2026, 6, 26)
    # 每个值都要能回到原文。表格形态下三个日期共用一条证据，
    # 键名是 table_dates；其它形态按字段名给证据。
    assert result.evidence.get("per_share_amount")
    assert result.evidence.get("table_dates")
    assert result.evidence["table_dates"]


# ======================================== 「每 10 股」写法（平安银行 2025 年度）
def test_pingan_per_ten_shares_wording_is_converted():
    """平安银行：正文写「每 10 股派发现金股利人民币 5.96 元」。

    必须换算成每股 0.596 元 = 596000 微元。写成 5.96 元就是 10 倍错误，
    而这种错误在账面上完全自洽。
    """

    result = parse("2026-06-05-1225352449")

    assert result.cash_per_share_micros == 596_000, result.cash_per_share_micros
    # 0.596 元 = 59.6 分，**仍然不是整数分**，所以分值必须为 None。
    # 我最初把这条写成 == 59，是把"59.6 分的整数部分"当成了精确值；
    # 微元 596000 才是精确的。
    assert result.per_share_cents_exact is None, result.per_share_cents_exact
    # 证据片段保留公告原文的写法（正文里是"每10股"，无空格）；
    # 断言按去空白后的形式比对，避免拿空格去卡原文。
    compact = result.evidence["per_share_amount"].replace(" ", "")
    assert "每10股" in compact, compact
    assert "5.96元" in compact, compact
    assert any("换算" in n for n in result.notes), result.notes


def test_pingan_announcement_has_no_pay_date_so_it_is_incomplete():
    """这份平安银行公告**没有写**现金红利发放日，因此必须判不完整。

    我最初把这条写成"应当 OK"，是错的：正文只有「股权登记日为…
    除权除息日为…」，发放日根本没有出现。很多 A 股公告的发放日与
    除权日同日，但那是**事实**而不是**规则**——把缺失当成同日，
    会让到账时点凭空提前，应收转现金的日期随之错误。

    这条用例的价值在于：它记录了一个"看起来应该能解析、实际缺字段"
    的真实样本，防止以后有人为了方便把缺失默认成同日。
    """

    result = parse("2026-06-05-1225352449")

    assert result.status == "INCOMPLETE", result.notes
    assert result.pay_date is None
    assert result.record_date == date(2026, 6, 11)
    assert result.ex_date == date(2026, 6, 12)
    assert any("发放日" in n for n in result.notes), result.notes


# ==================================================== 三个日期的先后必须成立
def test_dates_are_ordered():
    """解析出来的日期必须递增。缺字段时不适用（那是另一条用例的事）。"""

    for name in ("2026-06-22-1225379934", "2026-06-05-1225352449"):
        result = parse(name)
        if not (result.record_date and result.ex_date and result.pay_date):
            continue
        assert result.record_date <= result.ex_date <= result.pay_date, name


# ============================================ 方案/预案类不得当作实施公告
def test_proposal_title_is_not_executable():
    """标题是"利润分配方案"的，日期还没经股东会审议，不能执行。"""

    result = parse_cash_dividend(
        text="一、利润分配方案 每 10 股派发现金红利 1.00 元（含税）。",
        title="2026年中期利润分配方案",
        announcement_id="x1", announced_on=date(2026, 8, 15))

    assert result.status == "NOT_APPLICABLE"
    assert result.cash_per_share_micros is None
    assert any("股东会" in n for n in result.notes), result.notes


# ==================================================== 无关公告直接判不适用
def test_unrelated_announcement_is_not_applicable():
    result = parse_cash_dividend(
        text="本公司董事会保证公告内容真实、准确、完整。",
        title="股票交易异常波动公告",
        announcement_id="x2", announced_on=date(2026, 8, 11))

    assert result.status == "NOT_APPLICABLE"
    assert result.notes == []


# ==================================== 缺发放日必须报不完整，不得猜成同日
def test_missing_pay_date_is_incomplete_not_guessed():
    """只写登记日与除权日时，**不得**假设发放日同日。

    很多公告确实同日，但那是事实而非规则；把"缺失"当成"同日"，
    会让到账日凭空提前，应收转现金的时点随之错误。
    """

    result = parse_cash_dividend(
        text=("本次权益分派股权登记日为：2026 年 6 月 11 日，"
              "除权除息日为：2026 年 6 月 12 日。每股现金红利 0.50 元（含税）。"),
        title="某某2025年年度权益分派实施公告",
        announcement_id="x3", announced_on=date(2026, 6, 5))

    assert result.status == "INCOMPLETE"
    assert result.pay_date is None
    assert result.record_date == date(2026, 6, 11)
    assert any("发放日" in n for n in result.notes), result.notes


# ==================================================== 日期倒挂必须报不完整
def test_reversed_dates_are_incomplete():
    result = parse_cash_dividend(
        text=("股权登记日为：2026 年 6 月 20 日，除权除息日为：2026 年 6 月 12 日，"
              "现金红利发放日为：2026 年 6 月 25 日。每股现金红利 0.50 元。"),
        title="某某2025年年度权益分派实施公告",
        announcement_id="x4", announced_on=date(2026, 6, 5))

    assert result.status == "INCOMPLETE"
    assert any("顺序异常" in n for n in result.notes), result.notes
