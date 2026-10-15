"""全市场结构化财报批量采集（研究/内部用途）。

把主数据源从"逐份解析官方 PDF"换成结构化接口，因此产能不再受版式适配限制：
**每只证券 3 次请求（三张表），每次请求都归档并留收据**。实测 5 只 1.5 秒，
推算 5400 只约 27 分钟（串行）。

**这一步产出的是"结构化候选"，不是正式 PIT 事实。** 每条值都带：

* 来源报表与字段名；
* 归档内容哈希与收据 ID（可回溯到原始响应字节）；
* 采集时刻（``observed_at``）。

它**不含**"何时披露、有无更正"——那部分靠巨潮公告索引补齐，之后才能按
PIT 语义使用。这一点在输出里以 ``pit_state`` 字段显式标注，避免下游误当成
已具备时点语义。

用法::

    python tools/collect_structured_financials.py --limit 20 --output tmp/probe.json
    python tools/collect_structured_financials.py --all --output data/.../structured.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aquant.adapters.providers.eastmoney_structured import (  # noqa: E402
    FAMILY_LABELS, EastmoneyError, EastmoneyStructuredClient,
    FinancialTemplateUnsupported,
)
from aquant.domain.data.db import connect  # noqa: E402
from aquant.domain.data.forward_archive import ForwardArchive  # noqa: E402

SCHEMA = "aquant.structured-financial-candidates.v1"
ARCHIVE_ROOT = ROOT / "deploy" / "agentctl-q0" / "forward-archive"
POOL = ROOT / "configs" / "real-pool-csrc.yaml"
DEFAULT_OUTPUT = ROOT / "data" / "s2-structured" / "structured-candidates.json"

#: 需要的期间。方案要求报告期本身（不做 TTM 折算），按报告期直接取。
#: 用 ``--periods`` 覆盖，不在这里硬编码单一最新期。
DEFAULT_PERIODS = ("2026-06-30", "2025-12-31", "2025-06-30")


def _pool_symbols(path: Path) -> list[str]:
    """读研究池里的六位证券代码。只认显式 ``- "000651"`` 或 ``code:`` 形式，
    避免把 YAML 里其它数字当成代码。"""

    import re

    codes: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        match = re.search(r"\b(\d{6})\b", line)
        if match:
            codes.append(match.group(1))
    return sorted(set(codes))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pool", type=Path, default=POOL)
    parser.add_argument("--stock", action="append", default=[],
                        help="repeatable；默认取研究池全部")
    parser.add_argument("--period", action="append", default=[],
                        help="repeatable YYYY-MM-DD；默认 2026-06-30/2025-12-31/2025-06-30")
    parser.add_argument("--limit", type=int, default=0,
                        help="只做前 N 只，用于探针")
    parser.add_argument("--archive-root", type=Path, default=ARCHIVE_ROOT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--trusted-proxy-network", action="append", default=[])
    args = parser.parse_args(argv)
    if args.trusted_proxy_network:
        os.environ["AQUANT_TRUSTED_PROXY_NETWORKS"] = ",".join(args.trusted_proxy_network)

    stocks = list(args.stock) or _pool_symbols(args.pool.resolve())
    if args.limit:
        stocks = stocks[:args.limit]
    periods = tuple(args.period) or DEFAULT_PERIODS
    if not stocks:
        print("no security selected", file=sys.stderr)
        return 2

    archive_root = args.archive_root.resolve()
    started = datetime.now(timezone.utc)
    t0 = time.time()
    records: list[dict[str, object]] = []
    counts: dict[str, int] = {}
    with closing(connect(archive_root / "meta.sqlite")) as con:
        client = EastmoneyStructuredClient(ForwardArchive(con, archive_root))
        for index, code in enumerate(stocks, start=1):
            # 三张表**每只只请求一次**，全部期间共用；否则请求数会乘上期数。
            try:
                tables, family = client.tables(code)
            except FinancialTemplateUnsupported as exc:
                # 金融企业（银行/保险/券商）用另一套科目，本接口取不到。
                # 与"来源故障"分开计数——本仓 S2 口径本就排除金融行业模板。
                for period_end in periods:
                    records.append({
                        "instrument_id": code, "period_end": period_end,
                        "status": "FINANCIAL_TEMPLATE_UNSUPPORTED",
                        "reason": str(exc)[:160],
                    })
                counts["FINANCIAL_TEMPLATE_UNSUPPORTED"] = counts.get(
                    "FINANCIAL_TEMPLATE_UNSUPPORTED", 0) + len(periods)
                continue
            except EastmoneyError as exc:
                for period_end in periods:
                    records.append({
                        "instrument_id": code, "period_end": period_end,
                        "status": "SOURCE_UNAVAILABLE", "reason": str(exc)[:200],
                    })
                counts["SOURCE_UNAVAILABLE"] = counts.get(
                    "SOURCE_UNAVAILABLE", 0) + len(periods)
                continue
            for period_end in periods:
                found = client.period_from_tables(code, period_end, tables, family)
                if found.missing:
                    status = ("NO_REPORT" if len(found.missing) == len(found.evidence) + len(found.missing)
                              and not found.evidence else "PARTIAL_FIELDS")
                    records.append({
                        "instrument_id": code, "period_end": period_end,
                        "status": status, "missing": list(found.missing),
                        "values": {k: str(v) for k, v in sorted(found.values.items())},
                        "evidence": found.evidence,
                    })
                    counts[status] = counts.get(status, 0) + 1
                    continue
                records.append({
                    "instrument_id": code, "period_end": period_end,
                    "status": "CANDIDATE_COMPLETE",
                    "report_family": family,
                    "report_family_label": FAMILY_LABELS.get(family, family),
                    "values": {k: str(v) for k, v in sorted(found.values.items())},
                    "evidence": found.evidence,
                    # **显式标注**：本产物只有结构化值与采集时刻，
                    # 尚未绑定公告时点与修订链，因此还不是 PIT 可用事实。
                    "pit_state": "AWAITING_ANNOUNCEMENT_BINDING",
                    "unit": "yuan",
                })
                counts["CANDIDATE_COMPLETE"] = counts.get("CANDIDATE_COMPLETE", 0) + 1
            if index % 200 == 0:
                print(f"  ... {index}/{len(stocks)} 只，{time.time()-t0:.0f}s", flush=True)

    elapsed = time.time() - t0
    report = {
        "schema": SCHEMA,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "observed_at": started.isoformat(),
        "source_id": "eastmoney-structured-f10",
        "research_use_only": True,
        "securities_requested": len(stocks),
        "periods": list(periods),
        "elapsed_seconds": round(elapsed, 1),
        "counts": dict(sorted(counts.items())),
        "records": records,
        "boundary": (
            "Structured candidates only: values + archived HTTP evidence + observation "
            "time. Announcement timing and revision chain (CNINFO index) are NOT yet "
            "bound, so these are not PIT-usable facts. PDF machine verification is used "
            "as SAMPLE quality control, not as a per-security gate."
        ),
    }
    output = args.output.resolve()
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
    print(f"{output}: {len(stocks)} 只 × {len(periods)} 期，{elapsed:.1f}s，{report['counts']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
