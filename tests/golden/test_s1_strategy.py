from __future__ import annotations

import math
import statistics

import pytest

from aquant.domain.strategy.s1 import (
    FactorDataError,
    build_s1_signals,
    compute_price_factors,
)


def test_factor_formulas_use_the_documented_windows():
    prices = [100.0 + i for i in range(61)]
    factors = compute_price_factors(prices)

    assert factors.f01_momentum_20d == pytest.approx(prices[-1] / prices[-21] - 1)
    assert factors.f02_momentum_60d_skip_5d == pytest.approx(prices[-6] / prices[-61] - 1)
    returns = [math.log(prices[i] / prices[i - 1]) for i in range(41, 61)]
    assert factors.f04_volatility_20d == pytest.approx(
        statistics.stdev(returns) * math.sqrt(252))


@pytest.mark.parametrize("prices", [[100.0] * 60, [100.0] * 60 + [0.0]])
def test_missing_or_invalid_prices_are_not_filled_with_zero(prices):
    with pytest.raises(FactorDataError):
        compute_price_factors(prices)


def test_s1_combines_momentum_and_low_volatility_with_stable_ties():
    smooth_up = [100 + i for i in range(61)]
    volatile_up = [100 + i + (8 if i % 2 else -8) for i in range(61)]
    flat = [100.0] * 61
    signals = build_s1_signals(
        adjusted_closes_by_instrument={"B": volatile_up, "A": smooth_up, "C": flat},
        industry_by_instrument={"A": "I1", "B": "I1", "C": "I2"},
    )

    assert [s.instrument_id for s in signals] == sorted(
        [s.instrument_id for s in signals],
        key=lambda iid: (-next(s.signal_rank for s in signals if s.instrument_id == iid), iid),
    )
    by_id = {s.instrument_id: s for s in signals}
    assert by_id["A"].low_vol_rank > by_id["B"].low_vol_rank
    for signal in signals:
        assert signal.signal_rank == pytest.approx(
            0.5 * signal.momentum_rank + 0.5 * signal.low_vol_rank)
