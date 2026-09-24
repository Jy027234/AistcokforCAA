"""采集 BaoStock 季频财务数据到可续跑缓存。

从 2025Q1 起保留季度记录，供 TTM 使用上一年年报和同期数据。
只在相应报告的披露窗口结束后纳入新季度，避免批量请求尚未公布的报告。

    python tools/collect_financials.py --limit 20
    python tools/collect_financials.py
    python tools/collect_financials.py --retry-failed

输出：deploy/agentctl-q0/financials-cache.json
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aquant.adapters.providers.baostock import (  # noqa: E402
    BaostockClient, BaostockUnavailable,
)

OUT = ROOT / "deploy" / "agentctl-q0" / "financials-cache.json"
POOL = ROOT / "configs" / "real-pool-csrc.yaml"
ARCHIVE = ROOT / "deploy" / "agentctl-q0" / "baostock-archive"
BEIJING = timezone(timedelta(hours=8))
FIRST_YEAR = 2025


def available_periods(as_of: date | datetime | None = None) -> list[tuple[int, int]]:
    """披露窗口已结束的报告期；有时区的输入时间先换算为北京时间。"""

    if isinstance(as_of, datetime):
        today = as_of.astimezone(BEIJING).date()
    else:
        today = as_of or datetime.now(BEIJING).date()
    periods: list[tuple[int, int]] = []
    for year in range(FIRST_YEAR, today.year + 1):
        # 年报与次年一季报同在 4 月结束披露窗口。半年报、三季报
        # 分别在 8 月、10 月结束。窗口结束次日才批量查询全池。
        for quarter, ready_on in (
            (1, date(year, 5, 1)),
            (2, date(year, 9, 1)),
            (3, date(year, 11, 1)),
            (4, date(year + 1, 5, 1)),
        ):
            if ready_on <= today:
                periods.append((year, quarter))
    return periods


def period_key(period: tuple[int, int]) -> str:
    return f"{period[0]}Q{period[1]}"


def _profit_missing(row: dict | None) -> bool:
    return not row or any(row.get(field) in (None, "")
                          for field in ("netProfit", "statDate", "pubDate"))


def planned_periods(
    rows: dict, errors: dict, periods: list[tuple[int, int]], *,
    retry_failed: bool = False, refresh: bool = False,
) -> list[tuple[int, int]]:
    """逐期补缺；CFO 字段缺失或为空时继续补查现金流。"""

    if retry_failed:
        return [period for period in periods if period_key(period) in errors]
    if refresh:
        return list(periods)
    return [period for period in periods
            if _profit_missing(rows.get(period_key(period)))
            or period_key(period) in errors
            or rows[period_key(period)].get("CFOToNP") in (None, "")]


def load() -> dict:
    if OUT.exists():
        return json.loads(OUT.read_text(encoding="utf-8"))
    return {"created_at": None, "statements": {}, "failed": {}}


def save(doc: dict) -> None:
    doc["updated_at"] = datetime.now(timezone.utc).isoformat()
    tmp = OUT.with_suffix(".tmp")
    tmp.write_bytes(json.dumps(doc, ensure_ascii=False).encode("utf-8"))
    tmp.replace(OUT)


def _cash_flow(bs: BaostockClient, code: str, year: int, quarter: int) -> dict | None:
    """使用客户端的查询保护和原始记录归档，返回第一条现金流记录。"""

    bs._guard()
    result = bs._run(
        lambda: bs._bs.query_cash_flow_data(code=code, year=year, quarter=quarter),
        label=f"query_cash_flow_data({code})",
    )
    _fields, rows = bs._drain(result)
    bs._record(f"cash_flow:{code}:{year}Q{quarter}", rows)
    return rows[0] if rows else None


def _error(stage: str, exc: Exception) -> str:
    return f"{stage}: {type(exc).__name__}: {str(exc)[:80]}"


def _collect_period(
    bs: BaostockClient, code: str, year: int, quarter: int,
    rows: dict, errors: dict, *, refresh: bool,
) -> None:
    key = period_key((year, quarter))
    existing = rows.get(key)
    need_profit = (refresh or _profit_missing(existing)
                   or str(errors.get(key, "")).startswith("profit:"))
    if need_profit:
        try:
            profit, _receipt = bs.profit(code, year=year, quarter=quarter)
            if not profit:
                errors[key] = "profit: empty response"
                return
            # Refresh may return blank fields. Keep already cached nonblank facts.
            merged = dict(existing or {})
            merged.update({field: value for field, value in profit.items()
                           if value not in (None, "") or field not in merged})
            rows[key] = merged
            if _profit_missing(merged):
                errors[key] = "profit: incomplete netProfit or report date"
                return
        except Exception as exc:
            errors[key] = _error("profit", exc)
            return

    # A cached profit record may predate a failed cash-flow request. Reuse it.
    if rows.get(key) and (need_profit or key in errors
                          or rows[key].get("CFOToNP") in (None, "")):
        try:
            cash = _cash_flow(bs, code, year, quarter)
            if cash is None:
                errors[key] = "cash_flow: empty response"
                return
            value = cash.get("CFOToNP")
            if value in (None, ""):
                errors[key] = "cash_flow: empty CFOToNP"
                return
            rows[key]["CFOToNP"] = value
        except Exception as exc:
            errors[key] = _error("cash_flow", exc)
            return
    errors.pop(key, None)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--retry-failed", action="store_true")
    mode.add_argument("--refresh", action="store_true")
    args = ap.parse_args()

    pool = json.loads(POOL.read_text(encoding="utf-8"))
    targets = [item["instrument_id"] for item in pool["instruments"]]
    periods = available_periods()
    keys = [period_key(period) for period in periods]

    doc = load()
    statements: dict = doc.setdefault("statements", {})
    failed: dict = doc.setdefault("failed", {})
    doc["created_at"] = doc.get("created_at") or datetime.now(timezone.utc).isoformat()
    doc["periods"] = keys

    # 旧缓存的 failed[证券] 是字符串，代表整只证券采集失败。
    # 迁移成逐期错误，且只重试未落库的期；旧 statements 结构保持原样。
    for iid, error in list(failed.items()):
        if isinstance(error, str):
            missing = {key: error for key in keys if not statements.get(iid, {}).get(key)}
            if missing:
                failed[iid] = missing
            else:
                failed.pop(iid)

    if args.retry_failed:
        targets = [iid for iid in targets if iid in failed]
    if args.limit:
        targets = targets[:args.limit]

    plans = {
        iid: planned_periods(statements.get(iid, {}), failed.get(iid, {}),
                             periods, retry_failed=args.retry_failed,
                             refresh=args.refresh)
        for iid in targets
    }
    targets = [iid for iid in targets if plans[iid]]
    print(f"池内 {len(pool['instruments'])} 只，本次处理 {len(targets)} 只，"
          f"可用季度 {len(periods)} 个，待查 {sum(map(len, plans.values()))} 个证券季度")

    if not targets:
        save(doc)
        return 0

    attempted: set[tuple[str, str]] = set()
    try:
        with BaostockClient(ARCHIVE) as bs:
            completed = 0
            for i, iid in enumerate(targets, 1):
                code = iid[:2].lower() + "." + iid[3:]
                rows = statements.setdefault(iid, {})
                errors = failed.setdefault(iid, {})
                for year, quarter in plans[iid]:
                    _collect_period(bs, code, year, quarter, rows, errors,
                                    refresh=args.refresh)
                    attempted.add((iid, period_key((year, quarter))))
                    completed += 1
                if not errors:
                    failed.pop(iid, None)
                if i % 25 == 0 or i == len(targets):
                    print(f"  {i}/{len(targets)}  已处理 {completed} 个证券季度，"
                          f"失败 {sum(len(v) for v in failed.values())} 个")
                    save(doc)
    except BaostockUnavailable as exc:
        # 登录失败时也把未尝试的证券+期间写入缓存，供 --retry-failed 续跑。
        for iid in targets:
            errors = failed.setdefault(iid, {})
            for period in plans[iid]:
                key = period_key(period)
                if (iid, key) not in attempted:
                    errors[key] = _error("session", exc)
        save(doc)
        print(f"BaoStock 会话不可用：{exc}")
        return 2

    have = sum(bool(rows) for rows in statements.values())
    print(f"财务采集完成：{have} 只有数据，"
          f"失败 {sum(len(v) for v in failed.values())} 个证券季度")
    print(f"缓存：{OUT}（{OUT.stat().st_size / 1024:.0f} KB）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
