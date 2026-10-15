"""Company-level S2 announcement index: one scan per company, shared evidence.

The plan requires the announcement index to be built **once per company** and
shared by every report period, instead of one isolated stock×period process per
report.  This tool does that and persists what an incremental run needs:

* one all-category index sweep per company covering earliest-to-cutoff, so
  corrections, revisions and supplements are discovered by the same query that
  serves every period;
* a persisted **watermark** per (source, company, channel), so the next run
  only has to look at what is new;
* an explicit per-security status, so a coverage report never has to guess.

Every query and response goes through the existing guarded ``CninfoClient`` and
lands in the forward archive with its request-body hash and receipts, so the
evidence requirement is unchanged.  This tool writes no financial facts.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aquant.adapters.providers.cninfo import (  # noqa: E402
    CninfoClient, CrossCategoryIndexResult, ReportIndexResult, default_policy,
)
from aquant.adapters.providers.cninfo_probe import parse_report_period  # noqa: E402
from aquant.domain.data.db import connect  # noqa: E402
from aquant.domain.data.forward_archive import ForwardArchive  # noqa: E402
from aquant.domain.fundamentals.disclosure_link import (  # noqa: E402
    is_financial_correction_title,
)
from aquant.domain.research.s2 import required_s2_periods  # noqa: E402

SCHEMA = "aquant.s2-company-index.v1"
ARCHIVE_ROOT = ROOT / "deploy" / "agentctl-q0" / "forward-archive"
CONFIG_PATH = ROOT / "configs" / "real-pool-csrc.yaml"
MANIFEST = ROOT / "data" / "s2-pdf-review" / "s2-demand-manifest.json"
OUTPUT = ROOT / "data" / "s2-pdf-review" / "s2-company-index.json"
SHANGHAI = timezone(timedelta(hours=8))

#: 每家公司最早需要核对的报告期。修订链必须覆盖原报告发布之后的一切，
#: 因此窗口从"最早期末"开始，而不是固定回看 730 天。
EARLIEST_PERIOD = date(2024, 6, 30)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


#: 真正可能改写财报金额的更正类标题。判据与升仓层共用
#: ``is_financial_correction_title``，三处调用方只有一份定义。


def _index_summary(result: ReportIndexResult | CrossCategoryIndexResult,
                   *, period: str | None = None) -> dict[str, object]:
    summary: dict[str, object] = {
        "complete": result.complete,
        "termination": result.termination,
        "error": result.error,
        "organizationId": result.organization_id,
        "orgLookupReceiptId": result.org_lookup_receipt_id,
        "publicationWindow": [day.isoformat() for day in result.publication_window],
        "pages": [
            {"page": page.page_num, "requestBodyHash": page.request_body_hash,
             "responseReceiptId": page.receipt_id,
             "rawAnnouncementCount": page.raw_announcement_count,
             "hasMore": page.has_more}
            for page in result.pages
        ],
    }
    if period is not None:
        summary["reportPeriod"] = period
    if isinstance(result, ReportIndexResult):
        summary["matches"] = [
            {"announcementId": item.announcement.announcement_id,
             "title": item.announcement.title, "url": item.announcement.url,
             "page": item.page_num}
            for item in result.matches
        ]
    else:
        summary["candidates"] = [
            {"announcementId": entry.announcement_id, "title": entry.title,
             "candidateReasons": list(entry.candidate_reasons),
             "documentUrl": entry.document_url, "page": entry.page_num}
            for entry in result.candidates
        ]
    return summary


def observation_watermark(archive: ForwardArchive,
                          result: ReportIndexResult | CrossCategoryIndexResult,
                          ) -> str | None:
    """本次索引观察时刻：本页收据的最新 ``first_seen_at``。

    水位只能表示"当时看到的东西"，所以用它自己的收据时间，而不是运行时钟。
    下一次增量运行以它为下界，配合重叠窗口使用。
    """

    seen = []
    for page in result.pages:
        row = archive.con.execute(
            "SELECT first_seen_at FROM fetch_receipt WHERE receipt_id = ?",
            (page.receipt_id,),
        ).fetchone()
        if row and row[0]:
            seen.append(row[0])
    return max(seen) if seen else None


def load_previous_watermarks(path: Path) -> dict[str, str]:
    """从上一份索引报告取回各证券的水位；文件不存在时返回空表。"""

    if not path.is_file():
        return {}
    try:
        previous = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out: dict[str, str] = {}
    for row in previous.get("companies", []):
        stamp = row.get("watermarkAfter")
        if row.get("code") and stamp:
            out[row["code"]] = stamp
    return out


def scan_company(client, *, code: str, period_end: date, cutoff: date,
                 max_pages: int, archive: ForwardArchive) -> dict[str, object]:
    """扫描一家公司的定报索引与全分类索引。"""

    period = period_end.isoformat()
    report = client.report_index(
        stock_code=code, report_period=period, max_pages=max_pages)
    through = max(cutoff, period_end)
    cross = client.cross_category_index(
        stock_code=code, report_period=period, through=through, max_pages=max_pages)
    revision_titles = [
        {"announcementId": entry.announcement_id, "title": entry.title,
         "candidateReasons": list(entry.candidate_reasons)}
        for entry in cross.candidates
        if any(reason != "period_title" for reason in entry.candidate_reasons)
    ]
    correction_titles = [
        item for item in revision_titles
        if is_financial_correction_title(item["title"])
    ]
    return {
        "reportPeriod": period,
        "reportIndex": _index_summary(report, period=period),
        "crossCategoryIndex": _index_summary(cross, period=period),
        "reportIndexComplete": report.complete,
        "crossCategoryComplete": cross.complete,
        "titleCandidates": revision_titles,
        "revisionCandidates": correction_titles,
        "watermarkAfter": observation_watermark(archive, cross)
        or observation_watermark(archive, report),
    }


def status_for(company: dict[str, object], *, report_index_ok: bool,
               cross_index_ok: bool) -> tuple[str, str]:
    """给这一只证券一个明确状态与原因，覆盖报告直接汇总它。

    状态必须区分"标题命中但不像财报更正"与"标题指明财报更正"：前者是召回
    噪声，正常处理；后者才需要人工判断影响。把两者合成一个"需要修订复核"
    会让正常公司长期挂在异常队列里，违反本方案的分工。
    """

    if not report_index_ok:
        return ("INDEX_FAILED",
                "official regular-report index did not exhaust its pages")
    if not cross_index_ok:
        return ("CROSS_INDEX_FAILED",
                "official all-category index did not exhaust its pages")
    corrections = company.get("revisionCandidates") or []
    if corrections:
        return ("REVISION_REVIEW_REQUIRED",
                f"{len(corrections)} correction-titled candidates need disposition")
    titles = company.get("titleCandidates") or []
    if titles:
        return ("INDEXED",
                f"indexes exhausted; {len(titles)} revision/supplement-titled "
                "candidates carry no financial-correction marker")
    return ("INDEXED", "both official indexes exhausted within the recorded window")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=MANIFEST)
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    parser.add_argument("--archive-root", type=Path, default=ARCHIVE_ROOT)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    parser.add_argument("--stock", action="append", default=[],
                        help="repeatable six-digit code")
    parser.add_argument("--through", type=date.fromisoformat,
                        default=datetime.now(SHANGHAI).date())
    parser.add_argument("--max-pages", type=int, default=100)
    parser.add_argument("--trusted-proxy-network", action="append", default=[])
    args = parser.parse_args(argv)
    if args.trusted_proxy_network:
        os.environ["AQUANT_TRUSTED_PROXY_NETWORKS"] = ",".join(
            args.trusted_proxy_network)
    if args.through > datetime.now(SHANGHAI).date():
        parser.error("--through cannot be a future Beijing date")

    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    securities = {item["code"]: item for item in manifest["securities"]}
    codes = args.stock or [item["code"] for item in manifest["securities"]
                           if item.get("status") == "DEMAND_FROZEN"]
    if not codes:
        print("no security selected", file=sys.stderr)
        return 2

    archive_root = args.archive_root.resolve()
    started = _utc_now()
    output = args.output.resolve()
    previous_watermarks = load_previous_watermarks(output)
    companies: list[dict[str, object]] = []
    counts: dict[str, int] = {}
    with closing(connect(archive_root / "meta.sqlite")) as con:
        archive = ForwardArchive(con, archive_root)
        # ``default_policy()`` 只在构造时读一次环境变量，因此这里显式把
        # ``--trusted-proxy-network`` 变成进程级声明后再取策略；仅设置
        # os.environ 而不重建策略不会生效（实测会报 blocked-address）。
        client = CninfoClient(archive, policy=default_policy())
        for code in codes:
            security = securities.get(code, {})
            latest = security.get("latest_period_end")
            if not latest:
                companies.append({"code": code, "status": "NO_LATEST_PERIOD",
                                  "reason": "manifest has no latest disclosed period"})
                counts["NO_LATEST_PERIOD"] = counts.get("NO_LATEST_PERIOD", 0) + 1
                continue
            period_end = date.fromisoformat(latest)
            row: dict[str, object] = {
                "code": code, "name": security.get("name"),
                "industry": security.get("industry"),
                "latestPeriodEnd": latest,
                "watermarkBefore": previous_watermarks.get(code),
            }
            try:
                row.update(scan_company(
                    client, code=code, period_end=period_end,
                    cutoff=max(args.through, EARLIEST_PERIOD), max_pages=args.max_pages,
                    archive=archive))
            except Exception as exc:  # noqa: BLE001 - one company must not stop the batch
                row["status"] = "INDEX_FAILED"
                row["reason"] = f"{type(exc).__name__}: {exc}"[:200]
                companies.append(row)
                counts[row["status"]] = counts.get(row["status"], 0) + 1
                continue
            status, reason = status_for(
                row, report_index_ok=bool(row.get("reportIndexComplete")),
                cross_index_ok=bool(row.get("crossCategoryComplete")))
            row["status"] = status
            row["reason"] = reason
            row["requiredMetricPeriodCount"] = security.get("required_metric_period_count")
            row["requiredPeriods"] = sorted(
                {day.isoformat()
                 for days in required_s2_periods(period_end).values() for day in days})
            companies.append(row)
            counts[status] = counts.get(status, 0) + 1

    report = {
        "schema": SCHEMA,
        "generated_at": _utc_now().isoformat(),
        "started_at": started.isoformat(),
        "observed_through_date": args.through.isoformat(),
        "archive_root": str(archive_root),
        "scope": codes,
        "counts": dict(sorted(counts.items())),
        "companies": companies,
        "boundary": (
            "An exhausted index proves the recorded query window was fully paged at "
            "the observation time; it does not prove the company never amended, and "
            "a later announcement is a new incremental event. Watermarks bound the "
            "next incremental sweep; they are not a claim of completeness."
        ),
    }
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
    print(f"{output}: {report['counts']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
