"""Freeze the S2 report-period demand manifest for an explicit security scope.

The plan requires the field/period demand to be **generated** by
``required_s2_periods()`` and frozen per run, instead of hard-coding "the five
2026-H1 reports".  This tool produces that frozen manifest: for every security
and latest report period it records the exact metric/period demand, the industry
template decision, and the reason a security is outside the ordinary-business
template.

It performs no network access and writes no facts.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aquant.domain.research.s2 import (  # noqa: E402
    DEFAULT_S2_METRICS,
    S2Template,
    required_s2_periods,
)

SCHEMA = "aquant.s2-demand-manifest.v1"
DEFAULT_CONFIG = ROOT / "configs" / "real-pool-csrc.yaml"
DEFAULT_OUTPUT = ROOT / "data" / "s2-pdf-review" / "s2-demand-manifest.json"
#: 本方案固定验收范围：原有八股。它们必须先闭环，随后才扩展到配置池。
FIXED_SCOPE = (
    "000333", "600519", "601012", "000651", "600276", "002415", "300750", "688981",
)
SHANGHAI = timezone(timedelta(hours=8))
#: 池配置里 ``code`` 写成 ``sz300308``，``instrument_id`` 写成 ``SZ.300308``。
_CODE = re.compile(r"(?<!\d)(\d{6})(?!\d)")


def load_pool(path: Path) -> dict[str, dict[str, str]]:
    """读取配置研究池（该文件是 JSON，尽管后缀是 .yaml）。"""

    document = json.loads(path.read_text(encoding="utf-8"))
    instruments = document.get("instruments")
    if not isinstance(instruments, list):
        raise ValueError("pool config must contain an instruments list")
    rows: dict[str, dict[str, str]] = {}
    for row in instruments:
        code = None
        for key in ("code", "instrument_id", "instrumentId"):
            found = _CODE.search(str(row.get(key) or ""))
            if found is not None:
                code = found.group(1)
                break
        if code is None:
            continue
        if code in rows:
            raise ValueError(f"duplicate security code in pool config: {code}")
        rows[code] = {
            "code": code,
            "instrument_id": str(row.get("instrument_id") or row.get("instrumentId") or ""),
            "name": str(row.get("name") or ""),
            "industry": str(row.get("industry") or ""),
        }
    if not rows:
        raise ValueError("pool config produced no usable security codes")
    return rows


def industry_decisions(rows: dict[str, dict[str, str]]) -> tuple[dict, list[dict]]:
    """按证监会行业门类决定模板适用性；金融业明确排除而不是算作抓取失败。"""

    usable: dict[str, dict[str, str]] = {}
    excluded: list[dict] = []
    for code, row in sorted(rows.items()):
        industry = row["industry"].strip().upper()
        if industry.startswith("J"):
            excluded.append({
                "code": code, "name": row["name"], "industry": row["industry"],
                "reason": "S2_FINANCIAL_INDUSTRY_TEMPLATE_UNSUPPORTED",
                "detail": "CSRC J sector needs a separate bank/insurance/securities template",
            })
            continue
        if not industry:
            excluded.append({
                "code": code, "name": row["name"], "industry": row["industry"],
                "reason": "S2_INDUSTRY_UNCLASSIFIED",
                "detail": "no industry classification is available for template selection",
            })
            continue
        usable[code] = row
    return usable, excluded


def demand_for(latest_period_end: date) -> dict[str, object]:
    """按计算器的真实依赖展开字段—期间清单。"""

    required = required_s2_periods(latest_period_end)
    return {
        "latest_period_end": latest_period_end.isoformat(),
        "required_periods": {
            metric: [period.isoformat() for period in periods]
            for metric, periods in sorted(required.items())
        },
        "required_metric_period_count": sum(len(periods) for periods in required.values()),
    }


def build_manifest(*, config_path: Path, scope: tuple[str, ...],
                   latest_periods: dict[str, date], cutoff: date) -> dict[str, object]:
    pool = load_pool(config_path)
    usable, excluded = industry_decisions(pool)
    securities = []
    for code in scope:
        row = pool.get(code)
        if row is None:
            securities.append({
                "code": code, "status": "NOT_IN_CONFIG_POOL",
                "reason": "security is not present in the configured research pool",
            })
            continue
        if code not in usable:
            securities.append({
                "code": code, "name": row["name"], "industry": row["industry"],
                "status": "TEMPLATE_UNSUPPORTED",
                "reason": next(item["reason"] for item in excluded if item["code"] == code),
            })
            continue
        latest = latest_periods.get(code)
        if latest is None:
            securities.append({
                "code": code, "name": row["name"], "industry": row["industry"],
                "status": "LATEST_PERIOD_UNKNOWN",
                "reason": "no disclosed report period was supplied for this security",
            })
            continue
        securities.append({
            "code": code, "name": row["name"], "industry": row["industry"],
            "status": "DEMAND_FROZEN",
            "industry_template": S2Template.ORDINARY.value,
            **demand_for(latest),
        })
    frozen = [item for item in securities if item["status"] == "DEMAND_FROZEN"]
    return {
        "schema": SCHEMA,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "cutoff_beijing_date": cutoff.isoformat(),
        "config_path": str(config_path),
        "scope": list(scope),
        "metric_names": {
            "net_profit_attributable": DEFAULT_S2_METRICS.attributable_net_profit,
            "net_profit_consolidated": DEFAULT_S2_METRICS.consolidated_net_profit,
            "operating_cashflow": DEFAULT_S2_METRICS.operating_cash_flow,
            "revenue": DEFAULT_S2_METRICS.revenue,
            "parent_equity": DEFAULT_S2_METRICS.parent_equity,
        },
        "securities": securities,
        "summary": {
            "scope_size": len(scope),
            "demand_frozen": len(frozen),
            "required_metric_period_count": sum(
                item["required_metric_period_count"] for item in frozen),
            "template_unsupported": sum(
                1 for item in securities if item["status"] == "TEMPLATE_UNSUPPORTED"),
            "other_blocked": sum(
                1 for item in securities
                if item["status"] not in ("DEMAND_FROZEN", "TEMPLATE_UNSUPPORTED")),
            "pool_size": len(pool),
            "pool_ordinary_business": len(usable),
            "pool_financial_excluded": len(excluded),
        },
        "boundary": (
            "This manifest freezes the demand, not the data. A frozen demand entry "
            "means the calculator requires those metric-period values; it does not "
            "mean any of them has been collected or verified."
        ),
    }


def _parse_latest(values: list[str]) -> dict[str, date]:
    out: dict[str, date] = {}
    for value in values:
        if "=" not in value:
            raise ValueError(f"--latest-period expects CODE=YYYY-MM-DD, got {value!r}")
        code, _, raw = value.partition("=")
        code = code.strip()
        if len(code) != 6 or not code.isdigit():
            raise ValueError(f"--latest-period code must be six digits: {code!r}")
        out[code] = date.fromisoformat(raw.strip())
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--stock", action="append", default=[], metavar="CODE",
                        help="repeatable; defaults to the fixed eight-security scope")
    parser.add_argument("--latest-period", action="append", default=[],
                        metavar="CODE=YYYY-MM-DD",
                        help="repeatable; the latest disclosed period per security")
    parser.add_argument("--cutoff", type=date.fromisoformat,
                        default=datetime.now(SHANGHAI).date(),
                        help="Beijing cutoff date recorded in the manifest")
    args = parser.parse_args(argv)
    scope = tuple(args.stock) if args.stock else FIXED_SCOPE
    if len(set(scope)) != len(scope):
        parser.error("security scope contains duplicates")
    try:
        manifest = build_manifest(
            config_path=args.config.resolve(), scope=scope,
            latest_periods=_parse_latest(args.latest_period), cutoff=args.cutoff,
        )
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        print(f"manifest failed: {exc}", file=sys.stderr)
        return 2
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(manifest, stream, ensure_ascii=False, indent=1, sort_keys=True)
            stream.write("\n")
        os.replace(temporary, output)
    finally:
        if temporary.exists():
            temporary.unlink()
    summary = manifest["summary"]
    print(f"{output}: {summary['demand_frozen']}/{summary['scope_size']} securities frozen, "
          f"{summary['required_metric_period_count']} metric-period values required")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
