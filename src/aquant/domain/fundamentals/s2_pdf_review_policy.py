"""Frozen CNINFO PDF review scope for the 2026-H1 S2 industrial pilot.

This scope is deliberately narrower than all fields printed in every report.
It is one immutable admission cohort per security/report period, not permission
to append individual fields to an already reviewed report later.
"""

from __future__ import annotations

from datetime import date
from types import MappingProxyType


S2_PDF_SUBSET_POLICY = "s2-2026h1-required-fields-v1"
S2_PDF_PILOT_COMPANIES = MappingProxyType({
    "000651": "珠海格力电器股份有限公司",
    "600276": "江苏恒瑞医药股份有限公司",
    "002415": "杭州海康威视数字技术股份有限公司",
    "300750": "宁德时代新能源科技股份有限公司",
    "688981": "中芯国际集成电路制造有限公司",
})

_ALL_FIELDS = frozenset({
    "parent_equity", "revenue", "net_profit_consolidated",
    "net_profit_attributable", "operating_cashflow",
})
S2_PDF_REQUIRED_FIELDS = MappingProxyType({
    date(2024, 6, 30): frozenset({"revenue"}),
    date(2024, 12, 31): frozenset({"revenue"}),
    date(2025, 6, 30): _ALL_FIELDS,
    date(2025, 12, 31): _ALL_FIELDS - {"parent_equity"},
    date(2026, 6, 30): _ALL_FIELDS,
})


__all__ = [
    "S2_PDF_PILOT_COMPANIES", "S2_PDF_REQUIRED_FIELDS", "S2_PDF_SUBSET_POLICY",
]
