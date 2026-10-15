"""结构化源接入时踩过的坑，逐条锁住。

这里每个测试都对应一次**真实发生过的静默失效**——它们当时都不报错，只是
安静地少取数据或写错口径。这类缺陷靠人眼复核很难发现，因此必须有测试钉住。
"""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))

from aquant.adapters.providers.cninfo_probe import market_for_code  # noqa: E402
from aquant.adapters.providers.eastmoney_structured import secucode  # noqa: E402
from aquant.domain.fundamentals.versioned import ProfitScope  # noqa: E402


# --------------------------------------------------------------------------
# 交易所后缀：两段北交所代码用的是**相反**的后缀
# --------------------------------------------------------------------------

def test_eastmoney_suffix_for_beijing_segments() -> None:
    """43/83/87 段用 ``.NQ``，92 段用 ``.BJ``——实测相反，写反了取不到数。"""

    assert secucode("430017") == "430017.NQ"
    assert secucode("830799") == "830799.NQ"
    assert secucode("870199") == "870199.NQ"
    assert secucode("920002") == "920002.BJ"


def test_eastmoney_suffix_for_main_boards() -> None:
    assert secucode("000651") == "000651.SZ"
    assert secucode("300750") == "300750.SZ"
    assert secucode("600519") == "600519.SH"
    assert secucode("688981") == "688981.SH"


def test_eastmoney_suffix_rejects_non_six_digit_ids() -> None:
    from aquant.adapters.providers.eastmoney_structured import EastmoneyError

    with pytest.raises(EastmoneyError):
        secucode("651")
    with pytest.raises(EastmoneyError):
        secucode("abcdef")


# --------------------------------------------------------------------------
# 巨潮市场前缀：早期完全没有北交所
# --------------------------------------------------------------------------

def test_cninfo_market_includes_beijing_exchange() -> None:
    """早期只列深沪两市，600 只北交所证券绑定公告时全报
    ``unsupported A-share market prefix``——结构化财报取得到，但"何时披露"
    过不去，因此拿不到 PIT 时点。"""

    for code in ("430017", "830799", "870199", "920002"):
        assert market_for_code(code) == "bj"


def test_cninfo_market_keeps_main_boards() -> None:
    assert market_for_code("000651") == "szse"
    # 302 是深市较新的段，实测全市场有 1 只
    assert market_for_code("302132") == "szse"
    assert market_for_code("600519") == "sse"
    assert market_for_code("688981") == "sse"


# --------------------------------------------------------------------------
# profit_scope：两个利润指标必须区分归属口径
# --------------------------------------------------------------------------

def test_reconstructed_profit_scopes_match_the_pdf_convention() -> None:
    """全写 ``None`` 会让"归母"与"合并"失去区分度，下游按 ``profit_scope``
    排序时直接抛 ``'<' not supported between instances of 'NoneType' and
    'ProfitScope'``。取值必须与官方 PDF 路径一致。"""

    from ingest_reconstructed_facts import PROFIT_SCOPES

    assert PROFIT_SCOPES["net_profit_attributable"] is ProfitScope.ATTRIBUTABLE
    assert PROFIT_SCOPES["net_profit_consolidated"] is ProfitScope.CONSOLIDATED
    for metric in ("revenue", "parent_equity", "operating_cashflow"):
        assert PROFIT_SCOPES[metric] is None


# --------------------------------------------------------------------------
# 语言版本不是修订版
# --------------------------------------------------------------------------

def test_language_variants_are_not_treated_as_revisions() -> None:
    """同一期间常有中文正报 + 英文译本两份全文。早期用 ``len(full) > 1``
    一刀切判成"存在更正/修订"，而且按公告时间倒序会把**英文版**当成正报
    （实测 000651 2025 年报选中的是"2025年年度报告（英文版）"）。"""

    from bind_structured_to_announcements import _is_translation

    assert _is_translation("2025年年度报告（英文版）")
    assert _is_translation("2025 Annual Report (English)")
    assert not _is_translation("2025年年度报告")
    assert not _is_translation("贵州茅台2025年年度报告")


# --------------------------------------------------------------------------
# 修订链：最新版就是当期值，且可得时点必须取自最新版
# --------------------------------------------------------------------------

def _version(announcement_id: str, stamp: int):
    from types import SimpleNamespace

    return SimpleNamespace(announcement_id=announcement_id,
                           announcement_time_ms=stamp)


def test_original_plus_correction_selects_the_correction() -> None:
    """原版与更正版并存时，当期值是**更正版**。

    关键是可得时点要取自更正版：用原版公告日配更正后的数字是**前视偏差**，
    那组数字在更正日之前并不存在。
    """

    from bind_structured_to_announcements import current_report_version

    head, tied = current_report_version([
        _version("original", 1_700_000_000_000),
        _version("corrected", 1_750_000_000_000),
    ])
    assert head.announcement_id == "corrected"
    assert len(tied) == 1  # 时间戳不同 → 无歧义，不需要人工复核


def test_version_choice_does_not_depend_on_input_order() -> None:
    from bind_structured_to_announcements import current_report_version

    head, _ = current_report_version([
        _version("early", 100), _version("latest", 900), _version("middle", 500),
    ])
    assert head.announcement_id == "latest"


def test_same_timestamp_versions_are_reported_as_ambiguous() -> None:
    """同一时间戳上有多份不同文件时，仅凭索引无法判断哪份是当期值——
    这种才是真正需要人工复核的情形，不能猜。"""

    from bind_structured_to_announcements import current_report_version

    head, tied = current_report_version([
        _version("a", 5_000), _version("b", 5_000),
    ])
    assert len(tied) == 2
    assert head.announcement_id in tied


# --------------------------------------------------------------------------
# 市值来源：F10 只认决策日市值，且不允许用财报期股本×价格顶替
# --------------------------------------------------------------------------

def test_tencent_quote_symbol_covers_all_three_exchanges() -> None:
    from collect_market_caps_tencent import quote_symbol

    assert quote_symbol("000651") == "sz000651"
    assert quote_symbol("600519") == "sh600519"
    # 腾讯用 bj 前缀，而东财 43/83/87 段要 .NQ、92 段要 .BJ——两个数据源
    # 的北交所写法互不相同，不能互相套用
    assert quote_symbol("430017") == "bj430017"
    assert quote_symbol("920002") == "bj920002"


def test_market_cap_parser_keeps_the_total_market_cap_field() -> None:
    """腾讯行情以 ``~`` 分隔，第 45 项是总市值（亿元）。字段错位会静默取到
    别的数，因此用一个真实响应片段钉住位置。"""

    from collect_market_caps_tencent import parse_market_caps

    fields = [""] * 60
    fields[1], fields[2], fields[3], fields[45] = "格力电器", "000651", "38.28", "2144.22"
    payload = ('v_sz000651="' + "~".join(fields) + '";').encode("gbk")
    caps = parse_market_caps(payload)
    assert caps == {"000651": 214_422_000_000}


def test_market_cap_parser_skips_missing_fields_instead_of_zero_filling() -> None:
    from collect_market_caps_tencent import parse_market_caps

    empty = ('v_sz000651="' + "~".join([""] * 60) + '";').encode("gbk")
    assert parse_market_caps(empty) == {}
