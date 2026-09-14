import sys, tempfile, json
sys.path.insert(0, r'E:\IT\A股量化交易\src')
sys.path.insert(0, r'E:\IT\A股量化交易\tests\integration')
from pathlib import Path
from datetime import date, datetime, timezone
from decimal import Decimal
from aquant.domain.data.db import apply_migrations, connect
from aquant.domain.data.ingest import SnapshotBuilder
from aquant.domain.data.reader import SnapshotReader
from aquant.domain.data.snapshot import SnapshotStore
from aquant.domain.portfolio.construction import Candidate, ConstructionParams
from aquant.domain.portfolio.plan import PlanService
from aquant.domain.simulation.fees import synthetic_fee_table
from aquant.domain.simulation.simulator import BoardRule, Lot
from tests.integration.test_m1_ingest_e2e import build_snapshot

tmp = Path(tempfile.mkdtemp()); con = connect(tmp/'m.sqlite'); apply_migrations(con)
root = tmp/'api'; root.mkdir()
store = SnapshotStore(con, root); builder = SnapshotBuilder(con, root/'datasets')
build_snapshot(con, builder, store)
reader = SnapshotReader(store)
RULES = [BoardRule(exchange='SSE', board='MAIN', price_limit_pct=Decimal('10'), lot_size=100, effective_from=date(2026,7,6)),
         BoardRule(exchange='SZSE', board='MAIN', price_limit_pct=Decimal('10'), lot_size=100, effective_from=date(2026,7,6))]
svc = PlanService(con, reader, synthetic_fee_table(), RULES,
                  {'SYN.A.600519': ('SSE','MAIN'), 'SYN.A.000001': ('SZSE','MAIN')},
                  ConstructionParams(max_holdings=3, max_single_name_pct=Decimal('20'), max_single_industry_pct=Decimal('50')))
C = [Candidate('SYN.A.600519','IND_FOOD',0.9), Candidate('SYN.A.000001','IND_BANK',0.7)]
TD = date(2026,9,8); AS_OF = datetime(2026,9,11,12,30,tzinfo=timezone.utc)
pv = svc.preview(portfolio_id='pf-syn-m', snapshot_id='snap-syn-001', trading_day=TD, as_of=AS_OF,
                 candidates=C, cash_available_cents=100_000_000, lots=[], confirm_subject='user:alice')
token = svc.issue_confirmation(preview=pv, subject='user:alice', current_lots=[],
                               current_cash_cents=100_000_000)
svc.freeze(preview=pv, confirm_subject='user:alice', confirmation_token=token,
           expected_account_version=pv.account_version, current_lots=[], current_cash_cents=100_000_000)
lots = []
ex = svc.execute(plan_id=pv.plan_id, lots=lots, cash_available_cents=100_000_000)
cash = 100_000_000 + sum(e['amount_cents'] for e in ex['cash_entries'])
val = svc.value(portfolio_id='pf-syn-m', snapshot_id='snap-syn-001', trading_day=TD,
                as_of=AS_OF, lots=lots, cash_available_cents=cash)
rec = svc.reconcile(portfolio_id='pf-syn-m')
print('resolved plan_version   :', pv.plan_version)
print('resolved account_version:', pv.account_version)
print('orders                  :', [f"{o['side']} {o['instrument_id']} x{o['quantity']}@{o['price_cents']}" for o in pv.orders])
print('fills                   :', len(ex['fills']), '| rejections:', len(ex['rejections']))
print('estimated fees          :', pv.estimated_fees_cents, 'cents')
print('cash after execution    :', cash)
print('positions               :', {p['instrument_id']: p['quantity'] for p in val['positions']})
print('positions value         :', val['positions_value_cents'])
print('net value               :', val['net_value_cents'])
print('published               :', val['published'])
print('invariants all_ok       :', val['invariants']['all_ok'])
print('reconciled              :', rec['reconciled'], '| duplicate fee groups:', rec['duplicate_fee_groups'])
print('ledger rows             : plans=%d orders=%d fills=%d fees=%d lots=%d cash=%d valuations=%d' % (
    con.execute('SELECT COUNT(*) FROM simulation_plan').fetchone()[0],
    con.execute('SELECT COUNT(*) FROM "order"').fetchone()[0],
    con.execute('SELECT COUNT(*) FROM fill').fetchone()[0],
    con.execute('SELECT COUNT(*) FROM fee_charge').fetchone()[0],
    con.execute('SELECT COUNT(*) FROM position_lot').fetchone()[0],
    con.execute('SELECT COUNT(*) FROM cash_entry').fetchone()[0],
    con.execute('SELECT COUNT(*) FROM valuation').fetchone()[0]))
