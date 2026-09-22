"""把新浪三表候选行转换成版本化财务事实。

新浪页面只给当前展示的三张表，不能单独证明报告的公告时点或修订版本。
本适配层因此只做规范化和留痕：没有 CNINFO 公告关联的行仍然可以进入
``VersionedFinancialFactStore`` 做覆盖/缺失诊断，但会使用
``AvailabilityBasis.UNKNOWN``，从而被正式 PIT 读取拒绝。

只有上游已经把结构化值与一份具体的 CNINFO 公告原文版本核验完成、并在
``FinancialDisclosureLink.pit_eligible`` 上明确放行时，事实才会使用
``RECONSTRUCTED`` basis。这里不联网、不下载公告，也不把标题匹配本身当成
数值核验。
"""

from __future__ import annotations

import hashlib
from datetime import date, datetime
from typing import Sequence

from ...domain.data.pit import (
    AvailabilityBasis,
    PitMode,
    TimestampPrecision,
    date_only_available_at,
)
from ...domain.fundamentals.disclosure_link import (
    FinancialDisclosureLink,
    FinancialDisclosureVersion,
)
from ...domain.fundamentals.versioned import (
    FinancialFact,
    ProfitScope,
    StatementScope,
)
from .sina_financial import SinaS2Row


UNLINKED_DOCUMENT_PREFIX = "sina-observation:"

# These names are deliberately explicit about the accounting scope.  They are
# normalized domain metrics, not provider column names.
METRIC_NET_PROFIT_ATTRIBUTABLE = "net_profit_attributable"
METRIC_NET_PROFIT_CONSOLIDATED = "net_profit_consolidated"
METRIC_PARENT_EQUITY = "parent_equity"
METRIC_OPERATING_CASHFLOW = "operating_cashflow"
METRIC_REVENUE = "revenue"

_EXCHANGE_PREFIXES = {
    "SH": ("60", "68", "90"),
    "SZ": ("00", "30", "20"),
}


def _validate_utc(value: datetime, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value


def _combined_content_hash(row: SinaS2Row) -> str:
    """Return a stable digest for the three source pages on ``row``."""

    hashes = tuple(row.content_hashes)
    if len(hashes) != 3 or any(
        not value.startswith("sha256:") for value in hashes
    ):
        raise ValueError(
            "Sina S2 row must retain exactly three sha256 page content hashes"
        )
    payload = "|".join(hashes).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def normalize_sina_instrument_id(value: object) -> str:
    """Normalize Sina's code into the product's ``SH./SZ.`` identifier.

    Sina commonly supplies a bare six-digit code.  Explicit ``sh.``/``sz.``
    forms are accepted too.  The 90/20 prefixes are Shanghai/Shenzhen B-share
    codes; retaining them here avoids treating a valid provider code as a
    different bare instrument.  This adapter does not broaden the product's
    executable A-share universe; that remains an upstream pool decision.
    """

    text = str(value or "").strip().upper().replace("-", ".")
    market: str | None = None
    code = text
    if "." in text:
        market, code = text.split(".", 1)
        market = {"SSE": "SH", "SZSE": "SZ"}.get(market, market)
    elif text.startswith(("SH", "SZ")) and text[2:].isdigit():
        market, code = text[:2], text[2:]
    if not code.isdigit() or len(code) != 6:
        raise ValueError(f"invalid Sina stock code: {value!r}")

    inferred = next(
        (exchange for exchange, prefixes in _EXCHANGE_PREFIXES.items()
         if code.startswith(prefixes)),
        None,
    )
    if inferred is None:
        raise ValueError(f"unsupported Sina stock-code prefix: {value!r}")
    if market is not None and market != inferred:
        raise ValueError(f"Sina stock code market does not match prefix: {value!r}")
    return f"{inferred}.{code}"


def _observation_document_id(
    row: SinaS2Row,
    content_hash: str,
    instrument_id: str,
) -> str:
    """Give an unlinked observation an explicit, non-CNINFO identity."""

    return (
        f"{UNLINKED_DOCUMENT_PREFIX}{instrument_id}:"
        f"{row.end_date.isoformat()}:{content_hash.removeprefix('sha256:')}"
    )


def _version_id(
    instrument_id: str,
    metric: str,
    source_document_id: str,
    content_hash: str,
) -> str:
    payload = "|".join((
        instrument_id,
        metric,
        source_document_id,
        content_hash,
    )).encode("utf-8")
    return "sina-financial:" + hashlib.sha256(payload).hexdigest()


def _active_disclosure(
    row: SinaS2Row,
    disclosure: FinancialDisclosureLink | None,
) -> FinancialDisclosureVersion | None:
    if disclosure is None:
        return None
    if (normalize_sina_instrument_id(disclosure.stock_code)
            != normalize_sina_instrument_id(row.stock_code)):
        raise ValueError("CNINFO disclosure link belongs to another stock")
    if disclosure.period_end != row.end_date:
        raise ValueError("CNINFO disclosure link belongs to another report period")
    if disclosure.value_source_id != row.source_id:
        raise ValueError("CNINFO link value source does not match Sina row")
    if row.content_hashes and not set(row.content_hashes).issubset(
        set(disclosure.value_content_hashes)
    ):
        raise ValueError("CNINFO link does not cover all Sina source page hashes")
    active_id = disclosure.active_report_announcement_id
    if not active_id:
        return None
    matches = [
        version for version in disclosure.versions
        if version.announcement_id == active_id
    ]
    if len(matches) != 1:
        raise ValueError("CNINFO link active announcement is not uniquely described")
    return matches[0]


def sina_s2_row_to_facts(
    row: SinaS2Row,
    *,
    disclosure: FinancialDisclosureLink | None = None,
    trading_calendar: Sequence[date] = (),
    first_seen_at: datetime | None = None,
    ingested_at: datetime | None = None,
    preopen_already_captured: bool = True,
) -> tuple[FinancialFact, ...]:
    """Map one Sina report-period row to five append-only financial facts.

    ``disclosure`` is optional only for diagnostic ingestion.  If it is absent,
    or if its values have not been explicitly verified (the normal state of
    the current CNINFO metadata probe), all facts carry ``UNKNOWN`` availability
    and therefore fail ``VersionedFinancialFactStore.select_pit``.  Missing
    numeric values remain ``None`` and are never converted to zero.

    A linked row needs ``trading_calendar`` because a CNINFO announcement has
    date precision only.  The available time is then the next trading day's
    pre-open snapshot under the shared PIT rule.
    """

    observed = _validate_utc(row.retrieved_at, "row.retrieved_at")
    first_seen = _validate_utc(first_seen_at or observed, "first_seen_at")
    ingested = _validate_utc(ingested_at or observed, "ingested_at")
    instrument_id = normalize_sina_instrument_id(row.stock_code)
    content_hash = _combined_content_hash(row)
    active = _active_disclosure(row, disclosure)

    source_document_id = (
        active.announcement_id
        if active is not None
        else _observation_document_id(row, content_hash, instrument_id)
    )
    source_published_date = active.announced_on if active is not None else None

    if active is not None:
        if not trading_calendar:
            raise ValueError(
                "trading_calendar is required for a CNINFO-linked Sina fact"
            )
        available_at, _ = date_only_available_at(
            active.announced_on,
            trading_calendar,
            preopen_already_captured=preopen_already_captured,
        )
    else:
        # With no official publication date, the only defensible observation
        # point is local ingestion.  The UNKNOWN basis still blocks formal PIT.
        available_at = ingested

    basis = (
        AvailabilityBasis.RECONSTRUCTED
        if active is not None and disclosure is not None and disclosure.pit_eligible
        else AvailabilityBasis.UNKNOWN
    )
    # A Sina historical page is a current observation of a historical report
    # period.  It is never silently represented as a live vendor PIT stream.
    pit_mode = PitMode.HISTORICAL_RECONSTRUCTED
    timestamp_precision = (
        TimestampPrecision.DATE
        if active is not None
        else TimestampPrecision.UNKNOWN
    )

    fields = (
        (METRIC_NET_PROFIT_ATTRIBUTABLE, row.net_income_attributable, ProfitScope.ATTRIBUTABLE),
        (METRIC_NET_PROFIT_CONSOLIDATED, row.net_income, ProfitScope.CONSOLIDATED),
        (METRIC_PARENT_EQUITY, row.parent_equity, None),
        (METRIC_OPERATING_CASHFLOW, row.operating_cashflow, None),
        (METRIC_REVENUE, row.revenue, None),
    )
    return tuple(
        FinancialFact(
            instrument_id=instrument_id,
            metric=metric,
            period_end=row.end_date,
            statement_scope=StatementScope.CONSOLIDATED,
            profit_scope=profit_scope,
            value=value,
            currency="CNY",
            raw_unit=row.amount_unit,
            source_id=row.source_id,
            source_document_id=source_document_id,
            source_published_date=source_published_date,
            source_published_at=None,
            timestamp_precision=timestamp_precision,
            first_seen_at=first_seen,
            ingested_at=ingested,
            available_at=available_at,
            availability_basis=basis,
            pit_mode=pit_mode,
            content_hash=content_hash,
            version_id=_version_id(instrument_id, metric, source_document_id, content_hash),
        )
        for metric, value, profit_scope in fields
    )


def sina_s2_rows_to_facts(
    rows: Sequence[SinaS2Row],
    *,
    disclosures: Sequence[FinancialDisclosureLink] = (),
    trading_calendar: Sequence[date] = (),
    first_seen_at: datetime | None = None,
    ingested_at: datetime | None = None,
    preopen_already_captured: bool = True,
) -> tuple[FinancialFact, ...]:
    """Map a batch while requiring at most one matching disclosure per row."""

    by_period = {link.period_end: link for link in disclosures}
    if len(by_period) != len(disclosures):
        raise ValueError("duplicate CNINFO disclosure links for a report period")
    facts: list[FinancialFact] = []
    seen_rows: set[tuple[str, date]] = set()
    for row in rows:
        row_key = (normalize_sina_instrument_id(row.stock_code), row.end_date)
        if row_key in seen_rows:
            raise ValueError("duplicate Sina rows for a report period")
        seen_rows.add(row_key)
        facts.extend(sina_s2_row_to_facts(
            row,
            disclosure=by_period.get(row.end_date),
            trading_calendar=trading_calendar,
            first_seen_at=first_seen_at,
            ingested_at=ingested_at,
            preopen_already_captured=preopen_already_captured,
        ))
    return tuple(facts)


__all__ = [
    "METRIC_NET_PROFIT_ATTRIBUTABLE",
    "METRIC_NET_PROFIT_CONSOLIDATED",
    "METRIC_OPERATING_CASHFLOW",
    "METRIC_PARENT_EQUITY",
    "METRIC_REVENUE",
    "UNLINKED_DOCUMENT_PREFIX",
    "normalize_sina_instrument_id",
    "sina_s2_row_to_facts",
    "sina_s2_rows_to_facts",
]
