"""Manual readiness needs a valid pair without replaying unused history."""
from types import SimpleNamespace

import pytest

from apps.api import main as api


@pytest.fixture
def state(tmp_path):
    value = api.build_state(tmp_path)
    yield value
    value.con.close()


def snapshot(sid, *, day='2026-10-23', mode='PRODUCTION', available=True,
             asof='2026-10-25T14:00:00+00:00', published='2026-10-25T14:01:00+00:00'):
    return SimpleNamespace(snapshot_id=sid, trading_day=day, data_mode=mode,
        as_of_time=asof, published_at=published, s1_decision=SimpleNamespace(available=available))


@pytest.mark.parametrize('case', ['usable', 'late_publication', 'short_window', 'same_day', 'different_mode', 'no_pair'])
def test_readiness_stops_after_valid_pair_and_preserves_pair_rules(state, monkeypatch, case):
    existing = state
    execution = snapshot('execution', day='2026-10-26',
                         asof='2026-10-26T14:00:00+00:00', published='2026-10-26T14:01:00+00:00')
    first = snapshot('first')
    if case == 'late_publication':
        first.published_at = '2026-10-26T03:00:00+00:00'
    elif case == 'short_window':
        first.s1_decision.available = False
    elif case == 'same_day':
        first.trading_day = '2026-10-26'
    elif case == 'different_mode':
        first.data_mode = 'SYNTHETIC'
    fallback = snapshot('fallback', day='2026-10-22', asof='2026-10-22T14:00:00+00:00',
                        published='2026-10-22T14:01:00+00:00')
    if case == 'no_pair':
        first.s1_decision.available = fallback.s1_decision.available = False
    rows = [execution, first, fallback]
    metadata = [dict(snapshot_id=item.snapshot_id, data_mode=item.data_mode,
                     as_of_time=item.as_of_time, input_cutoff_at=item.as_of_time) for item in rows]
    if case != 'no_pair':
        metadata.append(dict(snapshot_id='unused-history', data_mode='PRODUCTION',
                             as_of_time='2026-01-01T00:00:00+00:00', input_cutoff_at='2026-01-01T00:00:00+00:00'))
    calls = []

    def read(sid):
        calls.append(sid)
        assert sid != 'unused-history', 'unused historical data must not be replayed for readiness'
        return next(item for item in rows if item.snapshot_id == sid)

    reader = SimpleNamespace(store=SimpleNamespace(list_published=lambda: metadata), published_snapshot=read)
    state = SimpleNamespace(reader=reader, con=existing.con, fees=existing.fees, data_dir=existing.data_dir)
    monkeypatch.setattr(api, '_snapshot_last_day', lambda state: '2026-10-26')
    result = api._manual_trial_readiness(state, {'dataMode': 'PRODUCTION', 'snapshotId': 'execution',
                                               'readiness': 'READY', 'blockingIssues': []})
    assert ('DECISION_EXECUTION_PAIR_MISSING' in {issue['code'] for issue in result['blockingIssues']}) == (case == 'no_pair')
    assert calls == (['execution', 'first'] if case == 'usable' else
                     ['execution', 'fallback'] if case == 'different_mode' else
                     ['execution', 'first', 'fallback'])
