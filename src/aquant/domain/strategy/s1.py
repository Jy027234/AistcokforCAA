"""S1 price-only baseline from development document §10.1 and §10.3.

Inputs must be adjusted closes known at the decision cutoff, ordered oldest to newest.
Missing or invalid prices are rejected instead of being replaced with zero.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from decimal import Decimal

from ..portfolio.construction import Candidate


class FactorDataError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class PriceFactors:
    f01_momentum_20d: float
    f02_momentum_60d_skip_5d: float
    f04_volatility_20d: float


@dataclass(frozen=True, slots=True)
class S1Signal:
    instrument_id: str
    industry_code: str
    factors: PriceFactors
    f01_rank: float
    f02_rank: float
    low_vol_rank: float
    momentum_rank: float
    signal_rank: float

    def as_candidate(self) -> Candidate:
        return Candidate(self.instrument_id, self.industry_code, self.signal_rank)


def compute_price_factors(adjusted_closes: list[int | float | Decimal]) -> PriceFactors:
    """Calculate F01, F02 and F04; S1 needs at least 61 valid closes."""

    if len(adjusted_closes) < 61:
        raise FactorDataError(
            f"S1 requires 61 valid adjusted closes; received {len(adjusted_closes)}"
        )
    prices = [float(p) for p in adjusted_closes]
    if any(not math.isfinite(p) or p <= 0 for p in prices):
        raise FactorDataError("adjusted closes must be finite and strictly positive")

    f01 = prices[-1] / prices[-21] - 1.0
    f02 = prices[-6] / prices[-61] - 1.0
    log_returns = [math.log(prices[i] / prices[i - 1])
                   for i in range(len(prices) - 20, len(prices))]
    # Sample standard deviation is the explicit reporting convention used by this baseline.
    f04 = statistics.stdev(log_returns) * math.sqrt(252)
    return PriceFactors(f01, f02, f04)


def _percentile_ranks(values: dict[str, float]) -> dict[str, float]:
    """Ascending average-tie percentile ranks in (0, 1], deterministic by value."""

    if not values:
        return {}
    ordered = sorted(values.items(), key=lambda item: (item[1], item[0]))
    ranks: dict[str, float] = {}
    index = 0
    n = len(ordered)
    while index < n:
        end = index + 1
        while end < n and ordered[end][1] == ordered[index][1]:
            end += 1
        average_ordinal = ((index + 1) + end) / 2.0
        for pos in range(index, end):
            ranks[ordered[pos][0]] = average_ordinal / n
        index = end
    return ranks


def build_s1_signals(
    *,
    adjusted_closes_by_instrument: dict[str, list[int | float | Decimal]],
    industry_by_instrument: dict[str, str],
) -> list[S1Signal]:
    """Cross-sectionally rank S1 and return descending signals with stable ID ties."""

    factors = {iid: compute_price_factors(prices)
               for iid, prices in adjusted_closes_by_instrument.items()}
    missing_industry = sorted(set(factors) - set(industry_by_instrument))
    if missing_industry:
        raise FactorDataError(f"missing PIT industry for: {', '.join(missing_industry)}")

    f01_rank = _percentile_ranks({iid: f.f01_momentum_20d for iid, f in factors.items()})
    f02_rank = _percentile_ranks(
        {iid: f.f02_momentum_60d_skip_5d for iid, f in factors.items()})
    low_vol_rank = _percentile_ranks(
        {iid: -f.f04_volatility_20d for iid, f in factors.items()})

    signals = []
    for iid, factor in factors.items():
        momentum = (f01_rank[iid] + f02_rank[iid]) / 2.0
        signal = 0.5 * momentum + 0.5 * low_vol_rank[iid]
        signals.append(S1Signal(
            instrument_id=iid, industry_code=industry_by_instrument[iid], factors=factor,
            f01_rank=f01_rank[iid], f02_rank=f02_rank[iid],
            low_vol_rank=low_vol_rank[iid], momentum_rank=momentum,
            signal_rank=signal,
        ))
    return sorted(signals, key=lambda s: (-s.signal_rank, s.instrument_id))


def build_s1_candidates(
    *,
    adjusted_closes_by_instrument: dict[str, list[int | float | Decimal]],
    industry_by_instrument: dict[str, str],
) -> list[Candidate]:
    return [s.as_candidate() for s in build_s1_signals(
        adjusted_closes_by_instrument=adjusted_closes_by_instrument,
        industry_by_instrument=industry_by_instrument,
    )]
