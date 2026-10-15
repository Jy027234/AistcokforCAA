"""从**已归档的原始响应**重建结构化候选，不碰网络。

## 为什么需要它

``collect_structured_financials.py`` 是长跑批处理（全市场数小时到十几小时），
而它**只在结束时**才写出候选 JSON。如果中途失败或被中断，那份 JSON 就没了。

但原始响应在设计上**是逐条归档的**（``EastmoneyStructuredClient.fetch_table``
先 ``store_bytes`` + 记收据，再解析），因此数据本身不会丢——丢的只是"解析这
一步"。本工具把解析从归档重做一遍，于是：

* 采集可中断、可续跑，且**不需要重新请求任何东西**；
* 归档字节即事实来源，重建结果与在线采集走**同一套解析代码**
  （``eastmoney_structured`` 的字段映射与取值规则），不会出现两套口径。

## 怎么定位

收据的 ``detail`` 记着 ``"<report> for <secucode>"``，据此还原每份响应属于
哪只证券的哪张表。同一 (report, secucode) 有多份响应时取**最新**一份
（后取的可覆盖先取的），并在输出里记下所用收据，便于复核。

用法::

    python tools/rebuild_structured_from_archive.py --output tmp/rebuilt.json
    python tools/rebuild_structured_from_archive.py --stock 000651 --stock 600519
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aquant.adapters.providers.eastmoney_structured import (  # noqa: E402
    FAMILY_LABELS, FAMILY_ORDER, SOURCE_ID, StatementTable,
    EastmoneyStructuredClient, _KINDS,
)
from aquant.domain.data.db import connect  # noqa: E402
from aquant.domain.data.forward_archive import ForwardArchive  # noqa: E402

SCHEMA = "aquant.structured-financial-candidates.v1"
ARCHIVE_ROOT = ROOT / "deploy" / "agentctl-q0" / "forward-archive"
DEFAULT_OUTPUT = ROOT / "data" / "s2-structured" / "structured-rebuilt.json"
DEFAULT_PERIODS = ("2026-06-30", "2025-12-31", "2025-06-30")
#: 收据 detail 的形态：``"<report> for <secucode>"``（可带后缀）。
_DETAIL = re.compile(r"(RPT_F10_FINANCE_[GBIS][A-Z]+) for ([0-9]{6}\.[A-Z]{2})")
#: 报表名 → (族, 表后缀)。四族共用同一命名规律 ``<族><表>``。
_REPORT_NAME = re.compile(r"RPT_F10_FINANCE_([GBIS])(BALANCE|INCOME|CASHFLOW)")


def _latest_responses(archive_root: Path) -> dict[tuple[str, str], tuple[str, str]]:
    """(report, secucode) → (content_hash, receipt_id)，取最新一份。"""

    chosen: dict[tuple[str, str], tuple[str, str, str]] = {}
    with closing(connect(archive_root / "meta.sqlite")) as con:
        for detail, content_hash, receipt_id, requested_at in con.execute(
                "SELECT detail, content_hash, receipt_id, requested_at "
                "FROM fetch_receipt WHERE source_id = ? AND outcome = 'OK' "
                "AND content_hash IS NOT NULL", (SOURCE_ID,)):
            match = _DETAIL.search(str(detail or ""))
            if not match:
                continue
            key = (match.group(1), match.group(2))
            stamp = str(requested_at or "")
            if key not in chosen or stamp > chosen[key][2]:
                chosen[key] = (str(content_hash), str(receipt_id), stamp)
    return {key: (value[0], value[1]) for key, value in chosen.items()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive-root", type=Path, default=ARCHIVE_ROOT)
    parser.add_argument("--stock", action="append", default=[])
    parser.add_argument("--period", action="append", default=[])
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--trusted-proxy-network", action="append", default=[])
    args = parser.parse_args(argv)
    if args.trusted_proxy_network:
        os.environ["AQUANT_TRUSTED_PROXY_NETWORKS"] = ",".join(args.trusted_proxy_network)

    periods = tuple(args.period) or DEFAULT_PERIODS
    archive_root = args.archive_root.resolve()
    responses = _latest_responses(archive_root)
    if not responses:
        print("no archived structured response found", file=sys.stderr)
        return 2

    # 归档里的报表名形如 ``RPT_F10_FINANCE_<族><表>``，据此还原"哪只证券的
    # 哪种表、属于哪个族"。族的判定与在线路径**同一规则**：资产负债表有数据
    # 的第一个族即为该证券的类型。
    raw: dict[str, dict[str, dict[str, StatementTable]]] = {}
    load_failures: Counter[str] = Counter()
    with closing(connect(archive_root / "meta.sqlite")) as con:
        archive = ForwardArchive(con, archive_root)
        for (report, secucode_value), (content_hash, receipt_id) in responses.items():
            code = secucode_value.split(".")[0]
            if args.stock and code not in set(args.stock):
                continue
            match = _REPORT_NAME.fullmatch(report)
            if not match:
                continue
            family, suffix = match.group(1), match.group(2)
            kind = {value: key for key, value in _KINDS.items()}.get(suffix)
            if kind is None:
                continue
            try:
                payload = archive.load_bytes(content_hash)
                document = json.loads(payload)
            except Exception as exc:  # noqa: BLE001 - 逐条如实计数
                load_failures[type(exc).__name__] += 1
                continue
            result = document.get("result") or {}
            rows = result.get("data") or []
            if not isinstance(rows, list) or not rows:
                continue
            raw.setdefault(code, {}).setdefault(family, {})[kind] = StatementTable(
                report=report, secucode=secucode_value,
                records=tuple(row for row in rows if isinstance(row, dict)),
                receipt_id=receipt_id, content_hash=content_hash, url="",
            )

    # 按 FAMILY_ORDER 选出该证券实际可用的族（与在线路径同序，结果一致）。
    by_code: dict[str, tuple[dict[str, StatementTable], str]] = {}
    for code, families in raw.items():
        for family in FAMILY_ORDER:
            tables = families.get(family) or {}
            if "balance" in tables and "income" in tables and "cashflow" in tables:
                by_code[code] = (tables, family)
                break

    # 复用在线采集的取值逻辑：同一份实现，避免"重建一套、在线一套"。
    client = EastmoneyStructuredClient.__new__(EastmoneyStructuredClient)
    records_out: list[dict[str, object]] = []
    counts: Counter[str] = Counter()
    for code in sorted(by_code):
        tables, family = by_code[code]
        for period_end in periods:
            found = client.period_from_tables(code, period_end, tables, family)
            if found.missing:
                records_out.append({
                    "instrument_id": code, "period_end": period_end,
                    "status": ("NO_REPORT" if not found.values else "PARTIAL_FIELDS"),
                    "report_family": family,
                    "missing": list(found.missing),
                    "values": {k: str(v) for k, v in sorted(found.values.items())},
                    "evidence": found.evidence,
                })
                counts["NO_REPORT" if not found.values else "PARTIAL_FIELDS"] += 1
                continue
            records_out.append({
                "instrument_id": code, "period_end": period_end,
                "status": "CANDIDATE_COMPLETE",
                "report_family": family,
                "report_family_label": FAMILY_LABELS.get(family, family),
                "values": {k: str(v) for k, v in sorted(found.values.items())},
                "evidence": found.evidence,
                "pit_state": "AWAITING_ANNOUNCEMENT_BINDING",
                "unit": "yuan",
            })
            counts["CANDIDATE_COMPLETE"] += 1

    report_doc = {
        "schema": SCHEMA,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": "rebuilt from archived responses (no network)",
        "source_id": SOURCE_ID,
        "research_use_only": True,
        "archive_root": str(archive_root),
        "securities_rebuilt": len(by_code),
        "periods": list(periods),
        "counts": dict(sorted(counts.items())),
        "load_failures": dict(load_failures),
        "records": records_out,
        "boundary": (
            "Rebuilt offline from archived HTTP responses; each value keeps the "
            "originating receipt id and content hash. Announcement timing is still "
            "not bound, so these remain candidates, not PIT-usable facts."
        ),
    }
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(report_doc, stream, ensure_ascii=False, indent=1, sort_keys=True)
            stream.write("\n")
        os.replace(temporary, output)
    finally:
        if temporary.exists():
            temporary.unlink()
    print(f"{output}: 从归档重建 {len(by_code)} 只 / {len(records_out)} 条，"
          f"{report_doc['counts']}")
    if load_failures:
        print(f"   读取失败: {dict(load_failures)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
