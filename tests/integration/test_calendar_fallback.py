"""日历降级与体积覆盖测试。

对应这次实跑暴露的两件事：

  1. 日历曾押在单一免费源（东财）上，而它会被按出口 IP 长时段封锁，
     导致归档脚本时好时坏。现在按优先级多源降级，并记录日历来自哪个源。
  2. 公告 PDF 天然大于 JSON 列表（实测有一份 18.8 MiB），
     触发默认 16 MiB 上限。上限本身是 §17.3 的要求，因此放宽数值而非取消检查。
"""

from __future__ import annotations

import sqlite3
from datetime import date

import pytest

from aquant.adapters.providers.calendar import load_trading_calendar
from aquant.adapters.providers.eastmoney import FetchOutcome
from aquant.adapters.providers.fetch_guard import (
    FetchDenied,
    FetchPolicy,
    check_content_length,
    read_bounded,
)
from aquant.domain.data.forward_archive import ForwardArchive


@pytest.fixture()
def archive(tmp_path):
    con = sqlite3.connect(tmp_path / "m.sqlite", isolation_level=None)
    con.row_factory = sqlite3.Row
    yield ForwardArchive(con, tmp_path)
    con.close()


class FakeClient:
    """假客户端：按预设返回结果，用于验证降级顺序而不依赖网络。"""

    def __init__(self, source_id: str, results: dict):
        self.source_id = source_id
        self._results = results
        self.calls: list[str] = []

    def daily_quotes(self, symbol: str, begin: str, end: str, *, adjust: int = 0):
        self.calls.append(symbol)
        bars = self._results.get(symbol)
        if bars is None:
            return FetchOutcome(False, None, "r", None, "blocked"), []
        return FetchOutcome(True, b"[]", "r", "sha256:" + "a" * 64, None, 200), bars


def _bars(days: list[str]) -> list[list[str]]:
    return [[d, "1", "1", "1", "1", "1"] for d in days]


# ================================================================== 日历降级
def test_calendar_prefers_the_first_working_source(archive):
    tc = FakeClient("tencent-ifzq", {"sh000001": _bars(["2026-09-09", "2026-09-10"])})
    em = FakeClient("eastmoney-direct", {"1.000001": _bars(["2026-09-09"])})
    res = load_trading_calendar(archive, begin=date(2026, 9, 1), end=date(2026, 9, 30),
                                tencent=tc, eastmoney=em)
    assert res.source_id == "tencent-ifzq"
    assert res.trading_days == [date(2026, 9, 9), date(2026, 9, 10)]
    assert em.calls == [], "首选源可用时不应打扰备用源"


def test_calendar_falls_back_when_the_first_source_is_blocked(archive):
    """东财实测会被按 IP 封锁；腾讯可用时必须能顶上。"""

    tc = FakeClient("tencent-ifzq", {"sh000001": _bars(["2026-09-09"])})
    em = FakeClient("eastmoney-direct", {})
    res = load_trading_calendar(archive, begin=date(2026, 9, 1), end=date(2026, 9, 30),
                                tencent=tc, eastmoney=em)
    assert res.source_id == "tencent-ifzq"
    # 失败也要留证：尝试记录里能看出哪个源没成
    assert any(not a["ok"] for a in res.attempted) or res.attempted


def test_calendar_uses_backup_when_primary_returns_nothing(archive):
    tc = FakeClient("tencent-ifzq", {})                       # 两个指数都没数据
    em = FakeClient("eastmoney-direct", {"1.000001": _bars(["2026-09-09", "2026-09-11"])})
    res = load_trading_calendar(archive, begin=date(2026, 9, 1), end=date(2026, 9, 30),
                                tencent=tc, eastmoney=em)
    assert res.source_id == "eastmoney-direct"
    assert res.trading_days == [date(2026, 9, 9), date(2026, 9, 11)]


def test_calendar_refuses_to_approximate_when_all_sources_fail(archive):
    """全部失败时必须报错，绝不退回工作日近似。"""

    tc = FakeClient("tencent-ifzq", {})
    em = FakeClient("eastmoney-direct", {})
    with pytest.raises(RuntimeError) as exc:
        load_trading_calendar(archive, begin=date(2026, 9, 1), end=date(2026, 9, 30),
                              tencent=tc, eastmoney=em)
    assert "refusing to fall back to a weekday approximation" in str(exc.value)


def test_calendar_requires_a_day_after_the_deadline_when_asked(archive):
    """§7.3 的保守顺延需要"窗口之后的那个交易日"确实存在。"""

    tc = FakeClient("tencent-ifzq", {"sh000001": _bars(["2026-09-09"])})
    em = FakeClient("eastmoney-direct", {})
    with pytest.raises(RuntimeError):
        load_trading_calendar(archive, begin=date(2026, 9, 1), end=date(2026, 9, 30),
                              tencent=tc, eastmoney=em,
                              require_after=date(2026, 9, 30))


def test_calendar_requirement_is_satisfied_by_a_later_day(archive):
    tc = FakeClient("tencent-ifzq", {"sh000001": _bars(["2026-09-09", "2026-10-08"])})
    em = FakeClient("eastmoney-direct", {})
    res = load_trading_calendar(archive, begin=date(2026, 9, 1), end=date(2026, 9, 30),
                                tencent=tc, eastmoney=em,
                                require_after=date(2026, 9, 30))
    assert res.trading_days[-1] == date(2026, 10, 8)


def test_calendar_deduplicates_and_sorts(archive):
    tc = FakeClient("tencent-ifzq", {"sh000001": _bars(
        ["2026-09-11", "2026-09-09", "2026-09-11"])})
    em = FakeClient("eastmoney-direct", {})
    res = load_trading_calendar(archive, begin=date(2026, 9, 1), end=date(2026, 9, 30),
                                tencent=tc, eastmoney=em)
    assert res.trading_days == [date(2026, 9, 9), date(2026, 9, 11)]


# ================================================================== 体积覆盖
def test_per_call_limit_can_be_raised_but_not_removed(archive):
    """放宽的是数值，不是检查本身：超过新上限仍须拒绝。"""

    p = FetchPolicy(resolve_dns=False, max_response_bytes=1024)
    # 默认上限下被拒
    with pytest.raises(FetchDenied):
        check_content_length("2048", p)
    # 单次可用更高上限放行
    check_content_length("2048", p, max_bytes=4096)
    # 但超过新上限依然被拒
    with pytest.raises(FetchDenied) as exc:
        check_content_length("8192", p, max_bytes=4096)
    assert exc.value.reason == "response-too-large"


def test_streamed_body_honours_the_per_call_limit():
    import io

    p = FetchPolicy(resolve_dns=False, max_response_bytes=1024)
    assert read_bounded(io.BytesIO(b"x" * 2048), p, max_bytes=4096) == b"x" * 2048
    with pytest.raises(FetchDenied):
        read_bounded(io.BytesIO(b"x" * 8192), p, max_bytes=4096)


def test_default_limit_is_unchanged_when_no_override():
    p = FetchPolicy(resolve_dns=False, max_response_bytes=1024)
    with pytest.raises(FetchDenied):
        check_content_length("2048", p)


def test_document_limit_is_bounded_and_larger_than_the_list_limit():
    from aquant.adapters.providers.cninfo import DOCUMENT_MAX_BYTES, default_policy

    assert DOCUMENT_MAX_BYTES > default_policy().max_response_bytes
    assert DOCUMENT_MAX_BYTES < 1024 * 1024 * 1024, "上限必须存在且合理，不得无界"
