"""Normal batch interception for the finite 600578 FY24 revenue pair.

The caller must first capture and review both annual regular reports and build
each report's full-scope revision-impact proof. This coordinator then writes
the root and adjusted successor atomically and identifies the one ordinary
CURRENT demand that the batch loop must skip.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Mapping

from ...domain.fundamentals.versioned import FinancialFact
from .fy24_revenue_600578_version_chain import (
    ORIGINAL_ID,
    ORIGINAL_PERIOD,
    PRESENTING_ID,
    ReportReviewContext,
    RevenueChainContractError,
    write_600578_fy24_revenue_forward_root,
)


@dataclass(frozen=True, slots=True)
class AnnualRevenuePairRoute:
    """Atomic pair result plus the single normal demand already fulfilled."""

    facts: tuple[FinancialFact, FinancialFact]
    inserted_count: int

    @property
    def suppressed_current_key(self) -> tuple[str, str, date]:
        return ("600578", "revenue", ORIGINAL_PERIOD)

    def suppresses_current(self, *, instrument_id: str, metric: str,
                           period_end: date) -> bool:
        """Prevent a second standalone root only for the finite FY24 key."""
        return (instrument_id, metric, period_end) == self.suppressed_current_key


def coordinate_600578_fy24_revenue_pair(
    *, repository, archive,
    report_contexts: Mapping[str, ReportReviewContext],
    current_company_descriptor: Mapping[str, object],
    calendar_evidence: Mapping[str, object],
) -> AnnualRevenuePairRoute:
    """Write FY24 CURRENT → FY25 PRIOR_ADJUSTED before ordinary batch routing.

    `report_contexts` must contain independently captured annual regular DTOs
    for 1223304346 and 1225184640. Suppression applies only to
    `(600578, revenue, 2024-12-31)`; the FY25 CURRENT key and other FY24 fields
    continue through the ordinary normal path.
    """
    missing = {ORIGINAL_ID, PRESENTING_ID} - set(report_contexts)
    if missing:
        raise RevenueChainContractError(
            "paired FY24/FY25 annual report contexts are required: " +
            ", ".join(sorted(missing))
        )
    pair, inserted = write_600578_fy24_revenue_forward_root(
        repository=repository,
        archive=archive,
        original=report_contexts[ORIGINAL_ID],
        annual=report_contexts[PRESENTING_ID],
        current_company_descriptor=current_company_descriptor,
        calendar_evidence=calendar_evidence,
    )
    return AnnualRevenuePairRoute(pair, inserted)


__all__ = [
    "AnnualRevenuePairRoute",
    "coordinate_600578_fy24_revenue_pair",
]
