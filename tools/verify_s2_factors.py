"""Compute F07–F10 and S2 ranks for an explicit security scope from formal facts.

This is the acceptance-path verifier: it reads the append-only financial-fact
repository through the PIT selector, matches each security to a decision-date
total market cap, and runs the frozen S2 engine for a stated cutoff.

It answers the only question that counts as "S2 is usable": can the product
compute the four factors and a cross-sectional rank for a known scope, with
every excluded security carrying an explicit reason?  It writes no facts and
registers no strategy version.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aquant.domain.fundamentals.fact_repository import (  # noqa: E402
    FinancialFactRepository,
)
from aquant.domain.fundamentals.versioned import (  # noqa: E402
    VersionedFinancialFactStore,
)
from aquant.domain.data.pit import PitMode  # noqa: E402
from aquant.domain.research.s2 import build_s2_signals, required_s2_periods  # noqa: E402
from aquant.operations.decision_market_caps import load_sidecar  # noqa: E402

SCHEMA = "aquant.s2-factor-verification.v1"
DEFAULT_FACTS = ROOT / "data" / "s2-pdf-review" / "s2-machine-verified-facts.sqlite"
DEFAULT_CAPS = ROOT / "deploy" / "agentctl-q0" / "decision-market-caps.json"
DEFAULT_ARCHIVE = ROOT / "deploy" / "agentctl-q0" / "forward-archive"
MARKET_TZ = ZoneInfo("Asia/Shanghai")


def load_market_caps(
    path: Path, *, trading_day: str | None,
    archive_root: Path = DEFAULT_ARCHIVE,
) -> tuple[dict[str, Decimal], dict]:
    # 无显式交易日时仅从文件取日期；所有结构、数值、时点及归档证据仍由
    # 正式加载器校验，不能把可解析 JSON 当成有效的决策日市值。
    if trading_day is None:
        raw = json.loads(path.read_text(encoding="utf-8"))
        trading_day = raw.get("market_cap_as_of") if isinstance(raw, dict) else ""
    validated = load_sidecar(
        path, expected_as_of=trading_day, archive_root=archive_root)
    caps = {
        instrument_id: Decimal(item.market_cap_cents) / Decimal(100)
        for instrument_id, item in validated.items.items()
    }
    return caps, validated.as_dict()


def cutoff_for(trading_day: date) -> datetime:
    """决策时点：该交易日北京时间 15:00 收盘，存 UTC。"""

    return datetime.combine(trading_day, time(15, 0), tzinfo=MARKET_TZ).astimezone(timezone.utc)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--facts-db", type=Path, default=DEFAULT_FACTS)
    parser.add_argument(
        "--pit-mode", choices=("LIVE_OBSERVED", "HISTORICAL_RECONSTRUCTED"),
        default="LIVE_OBSERVED",
        help="只用这一种 PIT 模式的事实。域层禁止把前向观测与历史重建混进"
             "同一结论（§7.2，否则时点语义歧义），因此这里必须显式选一种，"
             "而不是把两种都塞进同一次计算。")
    parser.add_argument("--market-caps", type=Path, default=DEFAULT_CAPS)
    parser.add_argument("--archive-root", type=Path, default=DEFAULT_ARCHIVE,
                        help="formal market-cap receipt/raw-response archive")
    parser.add_argument("--trading-day", type=date.fromisoformat, default=None,
                        help="decision day; must match the market cap sidecar")
    parser.add_argument("--instrument", action="append", default=[],
                        help="repeatable; defaults to every security in the fact db")
    parser.add_argument("--placeholder-market-cap", metavar="CODE=YUAN",
                        action="append", default=[],
                        help="repeatable; an explicitly synthetic F10 denominator. "
                             "Used only when the decision-day real total market cap "
                             "could not be collected; the report marks it as such.")
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--instrument-file", type=Path, default=None,
                        help="每行一个六位代码；全市场口径下用它替代上千个"
                             "--instrument 开关")
    parser.add_argument("--placeholder-market-cap-file", type=Path, default=None,
                        help="每行 CODE=YUAN；全市场口径下证券数上千，逐条重复"
                             "开关会超出命令行长度，因此允许从文件读（同格式）")
    args = parser.parse_args(argv)
    repository = FinancialFactRepository.open_existing(args.facts_db.resolve())
    store, accepted = repository.load_with_accepted_s2_pdf_reviews()
    # **按模式取单一事实集**：`assert_mode_consistent` 会拒绝混用（§7.2），
    # 所以这里在进入因子引擎之前就把另一种模式剔除，而不是让它一路走到
    # 域层再抛错——那样整只证券会被判 PIT_UNVERIFIED 而丢失。
    mode = PitMode[args.pit_mode]
    mode_facts = [fact for fact in store.facts if fact.pit_mode is mode]
    store = VersionedFinancialFactStore(mode_facts)
    if mode is PitMode.LIVE_OBSERVED:
        # 前向路径的准入依据是**已接受的 PDF 复核束**。
        codes = args.instrument or sorted(
            {fact.instrument_id for fact in mode_facts
             if fact.version_id in accepted})
    else:
        # 重建路径没有 PDF 复核束（它的准入依据是公告绑定 + 归档响应证据），
        # 因此证券范围直接取自该模式下的事实，不套用前向的复核门槛。
        codes = args.instrument or sorted(
            {fact.instrument_id for fact in mode_facts})
    if args.instrument_file:
        # 全市场口径下证券数上千，逐条 ``--instrument`` 会超出命令行长度。
        codes = sorted({line.strip() for line in
                        args.instrument_file.resolve().read_text(
                            encoding="utf-8").splitlines()
                        if line.strip()}) or codes
    if not codes:
        print(f"no security has facts under pit_mode={args.pit_mode}",
              file=sys.stderr)
        return 2
    placeholders: dict[str, Decimal] = {}
    for value in args.placeholder_market_cap:
        code, _, raw = value.partition("=")
        placeholders[code.strip()] = Decimal(raw.strip())
    # 全市场口径下证券数上千，逐条 ``--placeholder-market-cap`` 会超出命令行长度，
    # 因此允许从文件读（每行 ``CODE=YUAN``，与开关同格式）。
    if args.placeholder_market_cap_file:
        for line in args.placeholder_market_cap_file.resolve().read_text(
                encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            code, _, raw = line.partition("=")
            placeholders[code.strip()] = Decimal(raw.strip())
    sidecar: dict = {}
    if placeholders and set(placeholders) >= set(codes):
        caps, sidecar = {}, {
            "source_id": "PLACEHOLDER_NOT_DECISION_DATA",
            "market_cap_as_of": (args.trading_day or date(2026, 9, 29)).isoformat(),
            "receipt_id": None, "content_hash": None,
            "items": {},
        }
        caps = {f"PLACEHOLDER.{code}": value for code, value in placeholders.items()}
    else:
        caps, sidecar = load_market_caps(
            args.market_caps.resolve(),
            trading_day=(args.trading_day.isoformat() if args.trading_day else None),
            archive_root=args.archive_root.resolve())
    trading_day = args.trading_day or date.fromisoformat(sidecar["market_cap_as_of"])
    cutoff = cutoff_for(trading_day)

    # 快照层用 SZ.000651 形式的 instrument_id，领域事实用六位代码；这里显式
    # 给出映射，不做静默猜测。
    market_caps: dict[str, Decimal] = {}
    mapping: dict[str, str] = {}
    for code in codes:
        for prefix in ("SZ.", "SH.", "BJ.", "PLACEHOLDER."):
            if f"{prefix}{code}" in caps:
                mapping[code] = f"{prefix}{code}"
                market_caps[code] = caps[f"{prefix}{code}"]
                break
    placeholder_used = any(key.startswith("PLACEHOLDER.") for key in mapping.values())
    evaluated = build_s2_signals(
        store=store, instrument_ids=codes, cutoff=cutoff,
        market_cap_by_instrument=market_caps,
    )
    complete = [item for item in evaluated if item.signal is not None]
    if mode is PitMode.LIVE_OBSERVED:
        reviewed_complete = [
            item for item in complete
            if all(version in accepted for version in item.factors.fact_version_ids)
        ]
    else:
        # 重建路径**没有** PDF 复核束，所以"已复核"这个口径在这里不成立；
        # 保留同样的字段名会让下游误读，因此留空并靠 pit_mode 自证来源。
        reviewed_complete = []
    report = {
        "schema": SCHEMA,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "facts_db": str(args.facts_db.resolve()),
        "pit_mode": mode.value,
        "market_cap_source": {
            "path": None if placeholder_used else str(args.market_caps.resolve()),
            "archive_root": None if placeholder_used else str(args.archive_root.resolve()),
            "archive_verified": not placeholder_used,
            "source_id": sidecar.get("source_id"),
            "market_cap_as_of": sidecar.get("market_cap_as_of"),
            "receipt_id": sidecar.get("receipt_id"),
            "content_hash": sidecar.get("content_hash"),
            "placeholder_used": placeholder_used,
        },
        "decision_cutoff_at": cutoff.isoformat(),
        "trading_day": trading_day.isoformat(),
        "pool_size": len(codes),
        "market_cap_matched": len(market_caps),
        "market_cap_missing": sorted(set(codes) - set(market_caps)),
        "complete_factor_count": len(complete),
        "reviewed_complete_factor_count": len(reviewed_complete),
        "ranked_count": len(complete),
        "instruments": [
            {
                "instrument_id": item.instrument_id,
                "factors": item.factors.as_dict() if item.factors else None,
                "signal": (
                    {"F07_rank": item.signal.f07_rank, "F08_rank": item.signal.f08_rank,
                     "F09_rank": item.signal.f09_rank, "F10_rank": item.signal.f10_rank,
                     "quality_rank": item.signal.quality_rank,
                     "value_rank": item.signal.value_rank,
                     "signal_rank": item.signal.signal_rank}
                    if item.signal is not None else None
                ),
                "exclusion_code": item.exclusion_code,
                "exclusion_reason": item.exclusion_reason,
                "reviewed": (
                    mode is PitMode.LIVE_OBSERVED and item.factors is not None and
                    all(version in accepted for version in item.factors.fact_version_ids)
                ),
            }
            for item in evaluated
        ],
        "required_periods_for_latest": {
            metric: [period.isoformat() for period in periods]
            for metric, periods in sorted(
                required_s2_periods(date(2026, 6, 30)).items())
        },
        "boundary": (
            "This report proves the frozen S2 engine can compute F07-F10 and a "
            "same-cutoff rank for the stated scope from formally admitted facts. "
            "It does not register the S2 strategy family, publish a snapshot, or "
            "extend the result to any security outside the scope. "
            + ("Financial facts are HISTORICAL_RECONSTRUCTED: this is a research "
               "reconstruction, not evidence that the system observed these facts "
               "at the decision cutoff, and it carries no accepted PDF-review count. "
               if mode is PitMode.HISTORICAL_RECONSTRUCTED else
               "The accepted PDF-review count is reported separately from the "
               "number of computed signals. ")
            + ("F10 and every rank below rest on an EXPLICITLY SYNTHETIC market cap "
               "placeholder, because the decision-day real total market cap was not "
               "collected; they are an engine check, not a product number."
               if placeholder_used else
               "F10 uses the decision-day total market cap sidecar named above.")
        ),
    }
    output = (args.output or (ROOT / "data" / "s2-pdf-review"
                              / "s2-factor-verification.json")).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(report, stream, ensure_ascii=False, indent=1, sort_keys=True)
            stream.write("\n")
        os.replace(temporary, output)
    finally:
        if temporary.exists():
            temporary.unlink()
    print(f"{output}: complete={report['complete_factor_count']}/{report['pool_size']} "
          f"reviewed={report['reviewed_complete_factor_count']} "
          f"ranked={report['ranked_count']} caps_matched={report['market_cap_matched']}")
    for item in report["instruments"]:
        print(f"  {item['instrument_id']}: "
              + (f"F07={item['factors']['F07'][:10]} F08={item['factors']['F08'][:10]} "
                 f"F09={item['factors']['F09'][:10]} F10={item['factors']['F10'][:10]} "
                 f"signal_rank={item['signal']['signal_rank']}"
                 if item["factors"] and item["signal"] else
                 f"EXCLUDED {item['exclusion_code']}"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
