"""独立对抗性核验：不由修复作者编写。"""
import sys, tempfile, sqlite3
from pathlib import Path
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

ROOT = Path(r'E:\IT\A股量化交易')
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT / 'tests' / 'integration'))

from aquant.domain.data.db import apply_migrations, connect
from aquant.domain.data.ingest import SnapshotBuilder
from aquant.domain.data.reader import SnapshotReader
from aquant.domain.data.snapshot import SnapshotStore
from aquant.domain.portfolio.construction import Candidate, ConstructionParams
from aquant.domain.portfolio.plan import PlanError, PlanService
from aquant.domain.simulation.fees import synthetic_fee_table
from aquant.domain.simulation.simulator import BoardRule
from test_m1_ingest_e2e import build_snapshot

tmp = Path(tempfile.mkdtemp())
con = connect(tmp / 'm.sqlite'); apply_migrations(con)
root = tmp / 'api'; root.mkdir()
store = SnapshotStore(con, root); builder = SnapshotBuilder(con, root / 'datasets')
build_snapshot(con, builder, store)
reader = SnapshotReader(store)
RULES = [BoardRule(exchange='SSE', board='MAIN', price_limit_pct=Decimal('10'), lot_size=100, effective_from=date(2026,7,6)),
         BoardRule(exchange='SZSE', board='MAIN', price_limit_pct=Decimal('10'), lot_size=100, effective_from=date(2026,7,6))]
svc = PlanService(con, reader, synthetic_fee_table(), RULES,
                  {'SYN.A.600519': ('SSE','MAIN'), 'SYN.A.000001': ('SZSE','MAIN')},
                  ConstructionParams(max_holdings=3, max_single_name_pct=Decimal('20'), max_single_industry_pct=Decimal('50')))
TD = date(2026,9,8); AS_OF = datetime(2026,9,11,12,30,tzinfo=timezone.utc)
C = [Candidate('SYN.A.600519','A',0.9)]

print('=== 1. 前视检查：订单参考价是否只用执行日之前的价格 ===')
pv = svc.preview(portfolio_id='pf-syn-m', snapshot_id='snap-syn-001', trading_day=TD,
                 as_of=AS_OF, candidates=C, cash_available_cents=100_000_000, lots=[],
                 confirm_subject='user:alice')
o = pv.orders[0]
day8 = {r.trading_day: r for r in reader.daily_quotes('snap-syn-001', as_of=AS_OF, instrument_id='SYN.A.600519')}[TD]
day7 = {r.trading_day: r for r in reader.daily_quotes('snap-syn-001', as_of=AS_OF, instrument_id='SYN.A.600519')}[date(2026,9,7)]
print(f'  执行日({TD}) 开盘={day8.open_cents} 收盘={day8.close_cents}')
print(f'  前一交易日(09-07) 收盘={day7.close_cents}')
print(f'  订单价={o["price_cents"]}  参考日={o["reference_price_day"]}')
assert o['price_cents'] != day8.close_cents, '订单价等于执行日收盘价 = 前视！'
assert o['price_cents'] != day8.open_cents, '订单价等于执行日开盘价 = 使用了执行日价格'
assert o['price_cents'] == day7.close_cents, '订单价不等于前收'
assert o['reference_price_day'] == '2026-09-07'
print('  PASS: 订单只用执行日之前的价格，且记录了参考日')

print()
print('=== 2. 令牌：篡改预览后是否真的被拒 ===')
pv2 = svc.preview(portfolio_id='pf-syn-m', snapshot_id='snap-syn-001', trading_day=TD,
                  as_of=AS_OF, candidates=C, cash_available_cents=100_000_000, lots=[],
                  confirm_subject='user:alice')
tok = svc.issue_confirmation(preview=pv2, subject='user:alice', current_lots=[],
                             current_cash_cents=100_000_000)
pv2.orders[0]['quantity'] += 100
try:
    svc.freeze(preview=pv2, confirm_subject='user:alice', confirmation_token=tok,
               expected_account_version=pv2.account_version, current_lots=[],
               current_cash_cents=100_000_000)
    print('  FAIL: 篡改预览后仍冻结成功')
except PlanError as e:
    print(f'  PASS: 篡改被拒 -> {e.code} / {e.message[:52]}')

print()
print('=== 3. 令牌：换主体是否被拒 ===')
pv3 = svc.preview(portfolio_id='pf-syn-m', snapshot_id='snap-syn-001', trading_day=TD,
                  as_of=AS_OF, candidates=C, cash_available_cents=100_000_000, lots=[],
                  confirm_subject='user:alice')
tok3 = svc.issue_confirmation(preview=pv3, subject='user:alice', current_lots=[],
                              current_cash_cents=100_000_000)
try:
    svc.freeze(preview=pv3, confirm_subject='user:bob', confirmation_token=tok3,
               expected_account_version=pv3.account_version, current_lots=[],
               current_cash_cents=100_000_000)
    print('  FAIL: 换主体仍冻结成功')
except PlanError as e:
    print(f'  PASS: 换主体被拒 -> {e.code} / {e.message[:52]}')

print()
print('=== 4. 令牌：伪造随机串是否被拒 ===')
pv4 = svc.preview(portfolio_id='pf-syn-m', snapshot_id='snap-syn-001', trading_day=TD,
                  as_of=AS_OF, candidates=C, cash_available_cents=100_000_000, lots=[],
                  confirm_subject='user:alice')
try:
    svc.freeze(preview=pv4, confirm_subject='user:alice',
               confirmation_token='forged-token-1234567890',
               expected_account_version=pv4.account_version, current_lots=[],
               current_cash_cents=100_000_000)
    print('  FAIL: 伪造令牌仍冻结成功')
except PlanError as e:
    print(f'  PASS: 伪造令牌被拒 -> {e.code} / {e.message[:52]}')

print()
print('=== 5. 客户端谎报现金是否被账本拒绝 ===')
pv5 = svc.preview(portfolio_id='pf-syn-m', snapshot_id='snap-syn-001', trading_day=TD,
                  as_of=AS_OF, candidates=C, cash_available_cents=100_000_000, lots=[],
                  confirm_subject='user:alice')
tok5 = svc.issue_confirmation(preview=pv5, subject='user:alice', current_lots=[],
                              current_cash_cents=100_000_000)
try:
    svc.freeze(preview=pv5, confirm_subject='user:alice', confirmation_token=tok5,
               expected_account_version=pv5.account_version, current_lots=[],
               current_cash_cents=5)   # 谎报：账本里其实是 1 亿
    print('  FAIL: 谎报现金仍冻结成功')
except PlanError as e:
    print(f'  PASS: 与账本不符被拒 -> {e.code} / {e.message[:52]}')
