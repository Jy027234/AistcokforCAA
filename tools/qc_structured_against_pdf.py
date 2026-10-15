"""结构化源 vs 官方 PDF 的抽样质检。

**为什么需要这一步。** 结构化接口"说的"不能当证据。唯一能钉死它的办法是：
拿**独立来源**（官方 PDF）走本仓既有的机器核验链路，再与结构化值逐值对拍。

抽样刻意选**此前从未核验过**的证券，否则就是拿已知答案自我印证。

每条输出三态之一：

* ``MATCH``：PDF 机器核验通过，且五个字段与结构化值**逐值相等**；
* ``MISMATCH``：核验通过但值不等（**最严重**，必须查）；
* ``PDF_UNVERIFIED``：PDF 侧未能通过机器核验（不构成对结构化源的否定，
  只说明该样本无法用于对拍），附具体原因。

用法::

    python tools/qc_structured_against_pdf.py --sample 10 --output tmp/qc-pdf.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from contextlib import closing
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aquant.adapters.providers.cninfo import CninfoClient, default_policy  # noqa: E402
from aquant.adapters.providers.cninfo_statement_sections import (  # noqa: E402
    extract_section_facts,
)
from aquant.adapters.providers.s2_machine_verification import (  # noqa: E402
    MachineVerificationError, verify_machine_evidence,
)
from aquant.domain.data.db import connect  # noqa: E402
from aquant.domain.data.forward_archive import ForwardArchive  # noqa: E402

SCHEMA = "aquant.structured-pdf-sample-qc.v1"
ARCHIVE_ROOT = ROOT / "deploy" / "agentctl-q0" / "forward-archive"
CANDIDATES = ROOT / "data" / "s2-structured" / "structured-candidates.json"
DEFAULT_OUTPUT = ROOT / "data" / "s2-structured" / "qc-vs-pdf.json"

#: 全文 PDF 体积上限。策略默认上限（16 MiB）是按 JSON 响应设的，年报常有
#: 200+ 页、远超它；与巨潮路径保持同一量级（64 MiB），仍是有界读取。
DOCUMENT_MAX_BYTES = 64 * 1024 * 1024

#: 纸质核验里"公司全称"用于封面身份核对，必须来自官方原文而不是推断。
#: 抽样证券不在这张表里时，用 PDF 封面自带的公司名（``check_report_cover``
#: 会用同一份原文比对），因此这里不需要预先知道全称。


def _cover_company_name(pdf: bytes) -> str | None:
    """从 PDF **封面**取公司全称。

    不能用 CNINFO 索引里的 ``sec_name``——那是**简称**（实测 ``000021`` 是
    "深科技"），而 ``check_report_cover`` 要求**全称**出现在封面，简称必然
    对不上，抽样会整批假失败。

    取值方式刻意保守：只在封面文本里找"…股份有限公司/有限公司"形态的串，
    找到多个就**返回 None**（不猜），交由调用方交回失败关闭。
    """

    import io
    import re

    try:
        import pdfplumber
    except ImportError:  # pragma: no cover - environment guard
        return None
    with pdfplumber.open(io.BytesIO(pdf)) as document:
        cover = (document.pages[0].extract_text() or "").strip()
    found = re.findall(r"[\u4e00-\u9fff（）()]{2,30}?(?:股份有限公司|有限公司)", cover)
    unique = sorted({item for item in found})
    return unique[0] if len(unique) == 1 else None


def _sample(records: list[dict], count: int, periods: tuple[str, ...]) -> list[dict]:
    """按证券去重后均匀抽样，避免全部落在同一行业或同一期间。"""

    seen: dict[str, dict] = {}
    for row in records:
        if row.get("status") != "CANDIDATE_COMPLETE":
            continue
        if periods and row.get("period_end") not in periods:
            continue
        seen.setdefault(str(row["instrument_id"]), row)
    ordered = sorted(seen.values(), key=lambda r: str(r["instrument_id"]))
    if count >= len(ordered):
        return ordered
    step = len(ordered) / count
    return [ordered[int(index * step)] for index in range(count)]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidates", type=Path, default=CANDIDATES)
    parser.add_argument("--archive-root", type=Path, default=ARCHIVE_ROOT)
    parser.add_argument("--sample", type=int, default=10)
    parser.add_argument("--period", action="append", default=[],
                        help="repeatable；默认取候选里出现最多的那一期")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--trusted-proxy-network", action="append", default=[])
    args = parser.parse_args(argv)
    if args.trusted_proxy_network:
        os.environ["AQUANT_TRUSTED_PROXY_NETWORKS"] = ",".join(args.trusted_proxy_network)

    payload = json.loads(args.candidates.resolve().read_text(encoding="utf-8"))
    records = payload.get("records") or []
    periods = tuple(args.period)
    if not periods:
        from collections import Counter
        counts = Counter(str(r.get("period_end")) for r in records
                         if r.get("status") == "CANDIDATE_COMPLETE")
        periods = (counts.most_common(1)[0][0],) if counts else ()
    chosen = _sample(records, args.sample, periods)
    if not chosen:
        print("no candidate selected", file=sys.stderr)
        return 2

    archive_root = args.archive_root.resolve()
    results: list[dict[str, object]] = []
    counts: dict[str, int] = {}
    with closing(connect(archive_root / "meta.sqlite")) as con:
        archive = ForwardArchive(con, archive_root)
        client = CninfoClient(archive, policy=default_policy())
        for row in chosen:
            code, period_end = str(row["instrument_id"]), str(row["period_end"])
            entry: dict[str, object] = {
                "instrument_id": code, "period_end": period_end,
                "structured_values": row.get("values"),
            }
            try:
                index = client.report_index(
                    stock_code=code, report_period=period_end.replace("-", ""))
                full = []
                for match in index.matches:
                    meta = match.announcement
                    url = meta.url or ""
                    if ".PDF" not in url.upper():
                        continue
                    if "摘要" in (meta.title or ""):
                        continue
                    full.append(meta)
            except Exception as exc:  # noqa: BLE001 - 抽样工具，失败原因如实记录
                entry["status"] = "PDF_NOT_FOUND"
                entry["reason"] = f"index failed: {type(exc).__name__}: {exc}"[:180]
                results.append(entry)
                counts["PDF_NOT_FOUND"] = counts.get("PDF_NOT_FOUND", 0) + 1
                continue
            if not index.complete or not full:
                entry["status"] = "PDF_NOT_FOUND"
                entry["reason"] = (f"index complete={index.complete} "
                                   f"termination={index.termination} full reports={len(full)}")
                results.append(entry)
                counts["PDF_NOT_FOUND"] = counts.get("PDF_NOT_FOUND", 0) + 1
                continue
            # 同一期间只应有一份全文；多于一份说明存在修订，抽样工具不猜哪份是
            # 当期值，如实记为不唯一交给上游按修订链处理。
            if len(full) > 1:
                entry["status"] = "PDF_NOT_UNIQUE"
                entry["reason"] = f"{len(full)} full reports; revision chain required"
                entry["announcement_ids"] = [m.announcement_id for m in full]
                results.append(entry)
                counts["PDF_NOT_UNIQUE"] = counts.get("PDF_NOT_UNIQUE", 0) + 1
                continue
            announcement = full[0]
            try:
                outcome = client.document(
                    announcement.url, label=f"qc:{code}:{period_end}",
                    max_bytes=DOCUMENT_MAX_BYTES)
            except Exception as exc:  # noqa: BLE001
                entry["status"] = "PDF_DOWNLOAD_FAILED"
                entry["reason"] = f"{type(exc).__name__}: {exc}"[:160]
                results.append(entry)
                counts["PDF_DOWNLOAD_FAILED"] = counts.get("PDF_DOWNLOAD_FAILED", 0) + 1
                continue
            if not outcome.ok or not outcome.payload:
                entry["status"] = "PDF_DOWNLOAD_FAILED"
                entry["reason"] = str(outcome.detail)[:160]
                results.append(entry)
                counts["PDF_DOWNLOAD_FAILED"] = counts.get("PDF_DOWNLOAD_FAILED", 0) + 1
                continue
            pdf = outcome.payload
            entry["pdf"] = {
                "announcement_id": announcement.announcement_id,
                "title": announcement.title,
                "announcement_date": announcement.announcement_date_cn,
                "is_correction": announcement.is_correction,
                "is_revision": announcement.is_revision,
                "content_hash": outcome.content_hash,
                "receipt_id": outcome.receipt_id,
            }
            try:
                company = _cover_company_name(pdf)
                if company is None:
                    entry["status"] = "PDF_UNVERIFIED"
                    entry["reason"] = ("cover full company name is not uniquely "
                                       "determinable from page 1")
                    results.append(entry)
                    counts["PDF_UNVERIFIED"] = counts.get("PDF_UNVERIFIED", 0) + 1
                    continue
                entry["cover_company"] = company
                candidate = extract_section_facts(
                    pdf, instrument_id=code, company=company,
                    period_end=date.fromisoformat(period_end),
                    announcement_id=announcement.announcement_id,
                    document_url=announcement.url or "")
                verification = verify_machine_evidence(
                    pdf, candidate=candidate, expected_fields=tuple(candidate.by_field))
            except Exception as exc:  # noqa: BLE001
                entry["status"] = "PDF_UNVERIFIED"
                entry["reason"] = f"{type(exc).__name__}: {exc}"[:200]
                results.append(entry)
                counts["PDF_UNVERIFIED"] = counts.get("PDF_UNVERIFIED", 0) + 1
                continue
            if not verification.verified:
                entry["status"] = "PDF_UNVERIFIED"
                entry["reason"] = "unverified fields: " + ", ".join(
                    verification.unverified_fields)
                results.append(entry)
                counts["PDF_UNVERIFIED"] = counts.get("PDF_UNVERIFIED", 0) + 1
                continue
            structured = {k: Decimal(str(v)) for k, v in (row.get("values") or {}).items()}
            differences = {
                field: {
                    "structured": str(structured.get(field)),
                    "pdf": str(fact.value_yuan),
                }
                for field, fact in candidate.by_field.items()
                if field in structured and structured[field] != fact.value_yuan
            }
            entry["differences"] = differences
            entry["status"] = "MATCH" if not differences else "MISMATCH"
            results.append(entry)
            counts[entry["status"]] = counts.get(entry["status"], 0) + 1

    report = {
        "schema": SCHEMA,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "candidates": str(args.candidates),
        "periods": list(periods),
        "sampled": len(chosen),
        "counts": dict(sorted(counts.items())),
        "results": results,
        "boundary": (
            "PDF_UNVERIFIED is NOT evidence against the structured source: it means "
            "this sample could not be cross-checked (PDF layout not machine-verifiable "
            "yet). Only MISMATCH is a finding, and it must be investigated."
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
    print(f"{output}: 抽样 {len(chosen)} 只，{report['counts']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
