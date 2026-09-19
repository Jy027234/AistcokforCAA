"""降级链与源注册表测试。

这些测试固化 T4 的核心教训：免费源会无预警失效，因此
  1. 必须有备用源，且降级是**显式**的（degraded 标志 + 归档留痕）；
  2. 同一序列禁止静默混接（§6.3）；
  3. 权限未知的源不得进入模型外发链路（§17.2）。
"""

from __future__ import annotations

import pytest

from aquant.adapters.providers.chain import FetchChain, SilentMixError
from aquant.domain.data.source_registry import (
    Domain,
    IntegrationState,
    Rights,
    SourceHealth,
    SourceRegistry,
    SourceSpec,
    default_registry,
)


def spec(source_id: str, *, domains, priority=10, pit="NO", state=IntegrationState.TEST_PASSED,
         rights=None, health=SourceHealth.UNKNOWN) -> SourceSpec:
    return SourceSpec(
        source_id=source_id, display_name=source_id, domains=frozenset(domains),
        priority=priority, pit_available=pit, integration_state=state,
        health=health, rights=rights or {},
    )


class FakeClient:
    def __init__(self, source_id: str, *, ok: bool, detail: str | None = None, value=None):
        self.source_id = source_id
        self._ok = ok
        self._detail = detail
        self._value = value
        self.calls = 0

    def fetch(self, url: str, *, label: str = ""):
        raise NotImplementedError


def make_chain(registry, results):
    """results: source_id -> (ok, value, receipt_id, detail)"""

    def call(client):
        client.calls += 1
        return results[client.source_id]

    return call


# ------------------------------------------------------------------ 降级
def test_primary_success_is_not_degraded():
    reg = SourceRegistry([
        spec("primary", domains=[Domain.DAILY_QUOTES], priority=10),
        spec("backup", domains=[Domain.DAILY_QUOTES], priority=20),
    ])
    chain = FetchChain(reg)
    clients = {"primary": FakeClient("primary", ok=True), "backup": FakeClient("backup", ok=True)}
    res = chain.run(Domain.DAILY_QUOTES, clients,
                    make_chain(reg, {"primary": (True, "P", "r1", None),
                                     "backup": (True, "B", "r2", None)}))
    assert res.ok and res.used_source_id == "primary"
    assert res.degraded is False
    assert clients["backup"].calls == 0, "备用源不应被无谓调用"


def test_failure_falls_back_and_flags_degraded():
    reg = SourceRegistry([
        spec("primary", domains=[Domain.DAILY_QUOTES], priority=10),
        spec("backup", domains=[Domain.DAILY_QUOTES], priority=20),
    ])
    chain = FetchChain(reg)
    clients = {"primary": FakeClient("primary", ok=False), "backup": FakeClient("backup", ok=True)}
    res = chain.run(Domain.DAILY_QUOTES, clients,
                    make_chain(reg, {"primary": (False, None, "r1", "blocked"),
                                     "backup": (True, "B", "r2", None)}))
    assert res.ok and res.used_source_id == "backup"
    assert res.degraded is True, "降级必须被显式标记，供上层产出差异报告"
    assert [a.source_id for a in res.attempts] == ["primary", "backup"]


def test_all_sources_failing_returns_not_ok():
    reg = SourceRegistry([
        spec("a", domains=[Domain.DAILY_QUOTES], priority=10),
        spec("b", domains=[Domain.DAILY_QUOTES], priority=20),
    ])
    chain = FetchChain(reg)
    clients = {"a": FakeClient("a", ok=False), "b": FakeClient("b", ok=False)}
    res = chain.run(Domain.DAILY_QUOTES, clients,
                    make_chain(reg, {"a": (False, None, "r1", "x"), "b": (False, None, "r2", "y")}))
    assert res.ok is False
    assert res.used_source_id is None
    assert len(res.attempts) == 2


def test_no_candidate_for_domain():
    reg = SourceRegistry([spec("a", domains=[Domain.NEWS])])
    chain = FetchChain(reg)
    res = chain.run(Domain.DAILY_QUOTES, {}, lambda c: (True, 1, "r", None))
    assert res.ok is False
    assert "no test-passed source" in res.attempts[0].detail


def test_planned_source_is_not_used_until_test_passed():
    """§6.2 四态：计划接入 != 可用。"""

    reg = SourceRegistry([
        spec("planned", domains=[Domain.DAILY_QUOTES], priority=10,
             state=IntegrationState.PLANNED),
    ])
    chain = FetchChain(reg)
    res = chain.run(Domain.DAILY_QUOTES, {"planned": FakeClient("planned", ok=True)},
                    lambda c: (True, "x", "r", None))
    assert res.ok is False
    assert reg.get("planned").integration_state is IntegrationState.PLANNED


def test_repeated_failure_marks_source_blocked():
    """东财教训：连续失败后不要继续试探。"""

    reg = SourceRegistry([
        spec("flaky", domains=[Domain.DAILY_QUOTES], priority=10),
        spec("ok", domains=[Domain.DAILY_QUOTES], priority=20),
    ])
    chain = FetchChain(reg)
    clients = {"flaky": FakeClient("flaky", ok=False), "ok": FakeClient("ok", ok=True)}
    call = make_chain(reg, {"flaky": (False, None, "r", "down"), "ok": (True, "v", "r2", None)})
    for _ in range(2):
        chain.run(Domain.DAILY_QUOTES, clients, call)
    assert reg.get("flaky").health is SourceHealth.BLOCKED


def test_candidates_are_ordered_by_priority():
    reg = SourceRegistry([
        spec("c", domains=[Domain.DAILY_QUOTES], priority=30),
        spec("a", domains=[Domain.DAILY_QUOTES], priority=10),
        spec("b", domains=[Domain.DAILY_QUOTES], priority=20),
    ])
    assert [s.source_id for s in reg.candidates(Domain.DAILY_QUOTES)] == ["a", "b", "c"]


def test_candidates_include_blocked_sources_for_recovery():
    """按 health 预过滤会让降级链永远不尝试恢复的源。"""

    reg = SourceRegistry([
        spec("was-blocked", domains=[Domain.DAILY_QUOTES], priority=10,
             health=SourceHealth.BLOCKED),
    ])
    assert [s.source_id for s in reg.candidates(Domain.DAILY_QUOTES)] == ["was-blocked"]


# ------------------------------------------------------------------ 静默混接
def test_silent_mix_is_rejected():
    with pytest.raises(SilentMixError) as exc:
        FetchChain.assert_no_silent_mix(["a", "b"])
    assert "mix sources" in str(exc.value)


def test_declared_mix_is_allowed():
    FetchChain.assert_no_silent_mix(["a", "b"], declared="dataversion-2026-09-13")


def test_single_source_is_not_a_mix():
    FetchChain.assert_no_silent_mix(["a", "a"])
    FetchChain.assert_no_silent_mix([])


# ------------------------------------------------------------------ 权利
def test_unknown_rights_block_model_egress():
    """§17.2 未知权限默认不开放。"""

    reg = SourceRegistry([
        spec("no-rights", domains=[Domain.NEWS], priority=10, rights={}),
    ])
    assert reg.model_safe_sources(Domain.NEWS) == []


def test_explicit_model_permission_allows_egress():
    reg = SourceRegistry([
        spec("licensed", domains=[Domain.NEWS], priority=10,
             rights={"model_processing": Rights.ALLOWED}),
    ])
    assert [s.source_id for s in reg.model_safe_sources(Domain.NEWS)] == ["licensed"]


def test_prohibited_model_permission_blocks_egress():
    reg = SourceRegistry([
        spec("restricted", domains=[Domain.NEWS], priority=10,
             rights={"model_processing": Rights.PROHIBITED}),
    ])
    assert reg.model_safe_sources(Domain.NEWS) == []


# ------------------------------------------------------------------ 默认注册表
def test_default_registry_has_independent_quote_sources():
    """日行情必须有多个独立源，否则单点失效即全盘停摆。"""

    reg = default_registry()
    ids = [s.source_id for s in reg.candidates(Domain.DAILY_QUOTES)]
    assert len(ids) >= 3, ids


def test_tushare_financials_stays_gated_until_real_provider_validation():
    """文档字段齐全不等于真实账户、历史覆盖与 PIT 已经验收。"""

    source = default_registry().get("tushare-pro")
    assert Domain.FINANCIALS in source.domains
    assert source.requires_credentials is True
    assert source.integration_state is IntegrationState.CREDENTIALS_CONFIGURED
    assert source.pit_available == "PARTIAL"
    assert source.rights_open_for("research_use") is False


def test_default_registry_rights_are_unknown():
    """在人工确认条款之前，所有源都不得进入模型外发链路。"""

    reg = default_registry()
    for s in reg.all():
        assert not s.can_enter_model_context(), s.source_id


def test_default_registry_declares_no_pit_for_quote_sources():
    """实测结论：免费行情源均不提供历史时点版本。"""

    reg = default_registry()
    for s in reg.all():
        if Domain.DAILY_QUOTES in s.domains and s.source_id.startswith(("eastmoney", "tencent", "sina")):
            assert s.pit_available == "NO", s.source_id


def test_official_sources_have_partial_pit():
    """交易所与巨潮的公告页面自带发布时间，是时点证据的首选。"""

    reg = default_registry()
    for sid in ("sse-site", "szse-site", "cninfo"):
        assert reg.get(sid).pit_available == "PARTIAL"
