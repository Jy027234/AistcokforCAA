"""把结构化财报候选绑定到**官方公告时点与修订链**。

## 为什么需要这一步

``collect_structured_financials.py`` 产出的是"结构化候选"：有五个字段值、来源
字段名、收据与内容哈希、采集时刻，但**不含"何时披露、有无更正"**。没有时点就
不能按 PIT 语义使用——同一个数字在披露日之前是不可知的。

本工具把每条候选绑到巨潮公告索引上，产出：

* ``published_date``：官方**公告日期**（来自索引元数据，不是我们的采集时间）；
* ``available_at``：按仓内既有约定推导——公告日之后**第一个交易日**的盘前
  09:00（``aquant.domain.fundamentals.pit.available_at``，要求已证实的交易日历）；
* ``is_correction`` / ``is_revision``：该期间是否存在更正/修订公告；
* ``pit_mode = HISTORICAL_RECONSTRUCTED``、``availability_basis = RECONSTRUCTED``：
  因为这个可得时点是**从原始档案重建**的，不是我们实时观测到的。

这正是方案里 C 包（历史重建）的生产端——此前消费端齐备但**从未产出过一条
重建事实**。

## 时点纪律

* **交易日历必须来自已证实来源**，不能用工作日近似：近似会在长假前后算出
  错误的可得时点，而错误方向恰好是"提前可用"。日历缺失时**失败关闭**。
* 索引查询本身会归档（请求体、响应收据、内容哈希），因此"哪份公告、什么
  时候披露"同样可复核。
* 存在更正/修订时**不猜哪份是当期值**，如实标注并把该期间交给修订链处理。

用法::

    python tools/bind_structured_to_announcements.py --limit 10 --output tmp/bind-probe.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from contextlib import closing
from dataclasses import asdict
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aquant.adapters.providers.cninfo import (  # noqa: E402
    CircuitBreaker, CninfoClient, RateLimiter, default_policy,
)
from aquant.adapters.providers.s2_calendar_evidence import (  # noqa: E402
    verify_calendar_evidence,
)
from aquant.domain.data.db import connect  # noqa: E402
from aquant.domain.data.forward_archive import ForwardArchive  # noqa: E402
from aquant.domain.fundamentals.pit import available_at  # noqa: E402

SCHEMA = "aquant.structured-announcement-bound.v1"
ARCHIVE_ROOT = ROOT / "deploy" / "agentctl-q0" / "forward-archive"
CANDIDATES = ROOT / "data" / "s2-structured" / "structured-candidates.json"
CALENDAR = ROOT / "data" / "s2-pdf-review" / "calendar-evidence-current.json"
DEFAULT_OUTPUT = ROOT / "data" / "s2-structured" / "bound-facts.json"

#: 英文译本等**非中文权威版本**的标题特征。它们在标题里通常带"英文"或
#: "English"，而正报标题不带。剔除译本后才能判断"是不是真的有多个版本"。
_TRANSLATION_TITLE = re.compile(r"英文|English|\(Eng", re.IGNORECASE)


def _is_translation(title: str) -> bool:
    return bool(_TRANSLATION_TITLE.search(title))


def current_report_version(versions: list):
    """从同一期间的多份全文里选出**当期值**那一份。

    返回 ``(head, tied_ids)``：

    * ``head``：公告时间最新的一份。``matches`` 已按
      ``(announcement_time_ms, announcement_id)`` 倒序，因此这里取第一个；
      为稳妥仍显式排序，避免调用方传入未排序的列表。
    * ``tied_ids``：与 ``head`` **同一时间戳**的公告 ID 集合（包含 head）。多于一个表示
      "同一时刻有多份不同文件"，仅凭索引无法判断哪份是当期值——那才需要人工
      复核。时间戳不同的多版本**不构成歧义**：最新版就是当期值。

    **为什么最新版即当期值**：同一期间的更正后/更新后文件必然晚于原版。而且
    可得时点必须取自最新版——用原版公告日配更正后的数字是**前视偏差**，那组
    数字在更正日之前并不存在。这里返回 ``head`` 是为了让调用方从那一条取日期。
    """

    ordered = sorted(
        versions,
        key=lambda item: (getattr(item, "announcement_time_ms", 0) or 0,
                          getattr(item, "announcement_id", "") or ""),
        reverse=True)
    if not ordered:
        return None, set()
    head = ordered[0]
    newest_stamp = getattr(head, "announcement_time_ms", 0) or 0
    tied = {getattr(item, "announcement_id", "") for item in ordered
            if (getattr(item, "announcement_time_ms", 0) or 0) == newest_stamp}
    return head, tied


def is_full_report(announcement) -> bool:
    """全文报告的标题必须以报告及可选版本后缀结束，通知不是报告。"""
    title = re.sub(r"\s+", "", announcement.title or "")
    return (".PDF" in (announcement.url or "").upper()
            and "摘要" not in title and "关于" not in title
            and not _is_translation(title)
            and bool(re.search(r"报告(?:全文)?(?:[（(][^（）()]{1,40}[）)])?$", title)))


def revision_marked(announcement) -> bool:
    return bool(announcement.is_correction or announcement.is_revision
                or re.search(r"更正|修订|更新|勘误|补充", announcement.title or ""))


def verify_revision_values(row: dict, head, archive: ForwardArchive) -> dict:
    """只用所选报告的归档 PDF 证明五字段版本；不发出网络请求。"""
    receipt = archive.con.execute(
        "SELECT * FROM fetch_receipt WHERE source_id='cninfo' AND url=? "
        "AND outcome='OK' AND http_status=200 AND content_hash IS NOT NULL "
        "ORDER BY requested_at DESC,receipt_id DESC LIMIT 1", (head.url,)).fetchone()
    proof = {"announcement_id": head.announcement_id,
             "status": "PDF_NOT_ARCHIVED"}
    if receipt is None:
        return proof
    proof.update(receipt_id=receipt["receipt_id"], content_hash=receipt["content_hash"])
    if not archive.verify(receipt["content_hash"]):
        return dict(proof, status="PDF_ARCHIVE_INVALID")
    try:
        from qc_structured_against_pdf import _cover_company_name
        from aquant.adapters.providers.cninfo_statement_sections import extract_section_facts
        from aquant.adapters.providers.s2_machine_verification import verify_machine_evidence
        from aquant.adapters.providers.eastmoney_structured import FIELD_SOURCES
        pdf = archive.load_bytes(receipt["content_hash"])
        company = _cover_company_name(pdf)
        if company is None:
            return dict(proof, status="PDF_UNVERIFIED", reason="cover company is not unique")
        candidate = extract_section_facts(
            pdf, instrument_id=str(row["instrument_id"]), company=company,
            period_end=date.fromisoformat(str(row["period_end"])),
            announcement_id=head.announcement_id, document_url=head.url)
        verification = verify_machine_evidence(
            pdf, candidate=candidate, expected_fields=tuple(FIELD_SOURCES))
        proof["verification"] = verification.as_evidence()
        if not verification.verified or set(candidate.by_field) != set(FIELD_SOURCES):
            return dict(proof, status="PDF_UNVERIFIED", reason="five fields are not all verified")
        differences = {
            metric: {"structured": str((row.get("values") or {}).get(metric)),
                     "pdf": str(fact.value_yuan)}
            for metric, fact in candidate.by_field.items()
            if Decimal(str((row.get("values") or {}).get(metric))) != fact.value_yuan}
        return dict(proof, status="MATCH" if not differences else "MISMATCH",
                    differences=differences, verified_fields=sorted(candidate.by_field))
    except Exception as exc:  # 每份报告独立失败关闭，保留可复核原因。
        return dict(proof, status="PDF_UNVERIFIED",
                    reason=f"{type(exc).__name__}: {exc}"[:240])


def receipt_observed_at(row: dict, archive: ForwardArchive) -> str | None:
    """整束首次可观测时刻取所需原始收据的最大值，不用处理时间回填。"""
    ids = {str(ev["receipt_id"]) for ev in (row.get("evidence") or {}).values()
           if ev.get("receipt_id")}
    announcement = row.get("announcement") or {}
    if announcement.get("index_receipt_id"):
        ids.add(announcement["index_receipt_id"])
    ids.update(page["receipt_id"] for page in (row.get("index") or {}).get("pages", [])
               if page.get("receipt_id"))
    proof = row.get("revision_value_verification") or {}
    if proof.get("status") == "MATCH" and proof.get("receipt_id"):
        ids.add(proof["receipt_id"])
    stamps = []
    for rid in ids:
        receipt = archive.con.execute(
            "SELECT first_seen_at FROM fetch_receipt WHERE receipt_id=?", (rid,)).fetchone()
        if receipt is None:
            return None
        stamps.append(receipt[0])
    return max(stamps, key=datetime.fromisoformat) if stamps else None


def bind_record(row: dict, result, calendar: list[date],
                archive: ForwardArchive) -> dict:
    """在线采集与归档重放共用一套绑定、版本与可得时点规则。"""
    entry = {"instrument_id": str(row["instrument_id"]),
             "period_end": str(row["period_end"]),
             "values": row.get("values"), "evidence": row.get("evidence"),
             "index": {"complete": result.complete, "termination": result.termination,
                       "pages": [asdict(page) for page in result.pages],
                       "skipped": dict(result.skipped)}}
    if not result.complete:
        return dict(entry, status="INDEX_INCOMPLETE",
                    reason=f"termination={result.termination} error={result.error}")
    full = [hit.announcement for hit in result.matches if is_full_report(hit.announcement)]
    if not full:
        return dict(entry, status="NO_PERIODIC_REPORT", reason="index carries no Chinese full report")
    head, tied = current_report_version(full)
    selected = next(hit for hit in result.matches
                    if hit.announcement.announcement_id == head.announcement_id)
    entry["announcement"] = {
        "announcement_id": head.announcement_id, "title": head.title,
        "source_security_code": head.sec_code,
        "published_date": head.announcement_date_cn, "url": head.url,
        "report_kind": head.report_kind,
        "is_correction": bool(head.is_correction),
        "is_revision": bool(head.is_revision),
        "has_correction_or_revision": revision_marked(head),
        "index_receipt_id": selected.page_receipt_id,
        "index_page_hash": selected.page_content_hash,
    }
    entry["duplicate_reports"] = len(full)
    entry["report_versions"] = [
        {"announcement_id": item.announcement_id, "title": item.title,
         "source_security_code": item.sec_code,
         "published_date": item.announcement_date_cn,
         "is_current": item.announcement_id == head.announcement_id,
         "is_correction": bool(item.is_correction),
         "is_revision": bool(item.is_revision),
         "revision_marker_observed": revision_marked(item)}
        for item in sorted(full, key=lambda item: (
            item.announcement_time_ms, item.announcement_id), reverse=True)]
    corrections = [hit.announcement for hit in result.matches
                   if revision_marked(hit.announcement)]
    entry["correction_announcements"] = [
        {"announcement_id": item.announcement_id, "title": item.title,
         "published_date": item.announcement_date_cn,
         "is_full_report": is_full_report(item)} for item in corrections]
    # 保留真正的采集时刻，离线生成时间不是新一次源数据观测。
    receipt_ids = {page.receipt_id for page in result.pages}
    receipt_ids.update(str(ev.get("receipt_id")) for ev in
                       (row.get("evidence") or {}).values() if ev.get("receipt_id"))
    stamps = [archive.con.execute("SELECT first_seen_at FROM fetch_receipt WHERE receipt_id=?",
                                  (rid,)).fetchone() for rid in receipt_ids]
    observed = [stamp[0] for stamp in stamps if stamp is not None]
    if observed:
        entry["observed_at"] = max(observed, key=datetime.fromisoformat)
    if len(tied) > 1:
        return dict(entry, status="REVISION_REVIEW_REQUIRED",
                    reason=f"{len(tied)} full reports share the newest timestamp")
    if len(full) > 1 or corrections:
        proof = verify_revision_values(row, head, archive)
        entry["revision_value_verification"] = proof
        if proof["status"] != "MATCH":
            return dict(entry, status="REVISION_REVIEW_REQUIRED",
                        reason=f"selected report values are not proven: {proof['status']}")
        entry["report_content_hash"] = proof["content_hash"]
        pdf_seen = archive.con.execute(
            "SELECT first_seen_at FROM fetch_receipt WHERE receipt_id=?",
            (proof["receipt_id"],)).fetchone()[0]
        entry["observed_at"] = max(entry.get("observed_at", pdf_seen), pdf_seen,
                                   key=datetime.fromisoformat)
    if not head.announcement_date_cn:
        return dict(entry, status="NO_PUBLICATION_DATE", reason="missing publication date")
    try:
        entry["available_at"] = available_at(
            date.fromisoformat(head.announcement_date_cn), calendar).isoformat()
    except Exception as exc:
        return dict(entry, status="AVAILABILITY_UNRESOLVED",
                    reason=f"{type(exc).__name__}: {exc}"[:180])
    entry["status"] = "RECONSTRUCTED_READY"
    entry["pit_mode"] = "HISTORICAL_RECONSTRUCTED"
    entry["availability_basis"] = "RECONSTRUCTED"
    entry["timestamp_precision"] = "DATE"
    return entry


def verified_organization_map(path: Path, archive: ForwardArchive) -> tuple[dict, dict, dict]:
    """别名只能来自已归档的官方 A 股清单，不从代码形状猜发行人。"""
    packet = json.loads(path.read_text(encoding="utf-8"))
    url = "http://www.cninfo.com.cn/new/data/szse_stock.json"
    receipt = archive.con.execute("SELECT * FROM fetch_receipt WHERE receipt_id=?",
                                   (packet.get("receipt_id"),)).fetchone()
    digest = packet.get("content_hash")
    if (receipt is None or receipt["source_id"] != "cninfo" or receipt["url"] != url
            or receipt["outcome"] != "OK" or receipt["http_status"] != 200
            or receipt["content_hash"] != digest or packet.get("source_url") != url
            or packet.get("category") != "A股" or not archive.verify(digest)):
        raise ValueError("organization map does not match its official archived receipt")
    document = json.loads(archive.load_bytes(digest))
    rows = document.get("stockList") if isinstance(document, dict) else document
    expected = {str(row["code"]).strip(): str(row["orgId"]).strip()
                for row in rows if isinstance(row, dict)
                and str(row.get("category", "")).strip() == "A股"
                and len(str(row.get("code", "")).strip()) == 6
                and str(row.get("code", "")).strip().isdigit()
                and str(row.get("orgId", "")).strip()}
    if packet.get("organization_ids") != expected:
        raise ValueError("organization map differs from official source bytes")
    groups = {}
    for code, organization in expected.items():
        groups.setdefault(organization, []).append(code)
    provenance = {"receipt_id": receipt["receipt_id"], "content_hash": digest,
                  "source_url": url, "first_seen_at": receipt["first_seen_at"]}
    return expected, {key: tuple(sorted(codes)) for key, codes in groups.items()}, provenance


def attach_alias_evidence(entry: dict, result, groups: dict, provenance: dict) -> None:
    source_codes = sorted({hit.announcement.sec_code for hit in result.matches})
    if any(code != result.stock_code for code in source_codes):
        entry["security_alias_evidence"] = dict(
            provenance, requested_security_code=result.stock_code,
            source_security_codes=source_codes, organization_id=result.organization_id,
            official_codes=list(groups[result.organization_id]))


def _verified_trading_days(path: Path, archive: ForwardArchive) -> list[date]:
    """读**已证实**的交易日历。

    复用 ``verify_calendar_evidence``：它会拿归档的原始字节重建整包并逐字比对，
    因此"裸日期数组"或被人改过的日历都不接受。日历过期时它同样拒绝——这是
    刻意的，因为用工作日近似会在长假前后算出错误的可得时点，而错误方向恰好是
    **提前可用**。日历失效时正确做法是重跑 ``admit_s2_industrial_pdf_review.py
    calendar`` 刷新，而不是让本工具放宽。
    """

    if not path.is_file():
        raise SystemExit(
            f"calendar evidence not found at {path}; refresh it with "
            "`python tools/admit_s2_industrial_pdf_review.py calendar`")
    packet = json.loads(path.read_text(encoding="utf-8"))
    try:
        days, _ = verify_calendar_evidence(
            packet, archive, now=datetime.now(timezone.utc))
    except ValueError as exc:
        raise SystemExit(
            f"calendar evidence is not usable: {exc}\n"
            "refresh it with "
            "`python tools/admit_s2_industrial_pdf_review.py calendar "
            "--archive-root <root> --output <path>`") from exc
    return list(days)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidates", type=Path, default=CANDIDATES)
    parser.add_argument("--calendar", type=Path, default=CALENDAR)
    parser.add_argument("--archive-root", type=Path, default=ARCHIVE_ROOT)
    parser.add_argument("--stock", action="append", default=[])
    parser.add_argument("--period", action="append", default=[],
                        help="repeatable YYYY-MM-DD；只绑这些期间。"
                             "绑定是逐 (证券, 期间) 计的，先绑最新一期可把"
                             "全市场首轮从约 11 小时压到约 3.7 小时")
    parser.add_argument("--org-map", type=Path, default=None,
                        help="巨潮全量清单导出的 orgId 映射（universe-orgids.json）；"
                             "提供后跳过逐期的 orgId 查询，全市场省约 12.9 小时")
    parser.add_argument("--min-interval", type=float, default=2.5,
                        help="同一主机的请求最小间隔（秒）。全市场跑批实测会被"
                             "巨潮限流：熔断器连续 3 次失败即打开 120 秒，之后"
                             "的请求被快速拒绝而未真正尝试——那批只能重试，"
                             "重试时把间隔放宽、阈值提高才跑得完。")
    parser.add_argument("--breaker-threshold", type=int, default=3,
                        help="熔断器连续失败阈值；重试跑批时调大，避免被"
                             "瞬时抖动一次性打开")
    parser.add_argument("--breaker-cooldown", type=float, default=120.0,
                        help="熔断冷却秒数")
    parser.add_argument("--limit", type=int, default=0,
                        help="只处理前 N 条，用于探针")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--trusted-proxy-network", action="append", default=[])
    args = parser.parse_args(argv)
    if args.trusted_proxy_network:
        os.environ["AQUANT_TRUSTED_PROXY_NETWORKS"] = ",".join(args.trusted_proxy_network)

    payload = json.loads(args.candidates.resolve().read_text(encoding="utf-8"))
    records = [row for row in (payload.get("records") or [])
               if row.get("status") == "CANDIDATE_COMPLETE"]
    if args.stock:
        wanted = set(args.stock)
        records = [row for row in records if str(row.get("instrument_id")) in wanted]
    if args.period:
        # 绑定是逐 (证券, 期间) 计的：期间数直接乘上耗时，因此先绑最新一期
        # 能让全市场首轮更快可用，老期间随后再补。
        periods_wanted = {str(item)[:10] for item in args.period}
        records = [row for row in records
                   if str(row.get("period_end"))[:10] in periods_wanted]
    if args.limit:
        records = records[:args.limit]
    if not records:
        print("no candidate selected", file=sys.stderr)
        return 2

    archive_root = args.archive_root.resolve()
    started = datetime.now(timezone.utc)
    t0 = time.time()
    bound: list[dict[str, object]] = []
    counts: dict[str, int] = {}
    with closing(connect(archive_root / "meta.sqlite")) as con:
        archive = ForwardArchive(con, archive_root)
        # 日历要拿归档字节验证，因此必须在 archive 打开之后读。
        calendar = _verified_trading_days(args.calendar.resolve(), archive)
        client = CninfoClient(
            archive, policy=default_policy(),
            limiter=RateLimiter(min_interval_seconds=args.min_interval),
            breaker=CircuitBreaker(failure_threshold=args.breaker_threshold,
                                   cooldown_seconds=args.breaker_cooldown))
        # orgId 映射来自巨潮官方全量清单（响应已归档，见 universe-orgids.json
        # 的 receipt_id/content_hash）。同一只证券的 orgId 逐期都一样，不预解析
        # 就要为每个期间重复查一次；全市场 6179 只 × 3 期约多 18537 次请求。
        organization_ids: dict[str, str] = {}
        alias_groups: dict = {}
        organization_provenance: dict = {}
        if args.org_map is not None and args.org_map.is_file():
            organization_ids, alias_groups, organization_provenance = verified_organization_map(
                args.org_map, archive)
        for index, row in enumerate(records, start=1):
            code = str(row["instrument_id"])
            period_end = str(row["period_end"])
            try:
                result = client.report_index(
                    stock_code=code, report_period=period_end.replace("-", ""),
                    organization_id=organization_ids.get(code),
                    security_aliases=alias_groups.get(organization_ids.get(code), ()))
                entry = bind_record(row, result, calendar, archive)
                attach_alias_evidence(entry, result, alias_groups, organization_provenance)
            except Exception as exc:  # noqa: BLE001 - 逐条如实记录
                entry = {"instrument_id": code, "period_end": period_end,
                         "values": row.get("values"), "evidence": row.get("evidence"),
                         "status": "INDEX_FAILED",
                         "reason": f"{type(exc).__name__}: {exc}"[:180]}
            bound.append(entry)
            status = str(entry["status"])
            counts[status] = counts.get(status, 0) + 1
            if index % 50 == 0:
                print(f"  ... {index}/{len(records)}，{time.time()-t0:.0f}s", flush=True)

    elapsed = time.time() - t0
    report = {
        "schema": SCHEMA,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "observed_at": started.isoformat(),
        "candidates": str(args.candidates),
        "elapsed_seconds": round(elapsed, 1),
        "counts": dict(sorted(counts.items())),
        "records": bound,
        "boundary": (
            "available_at is RECONSTRUCTED from the official announcement date via the "
            "verified trading calendar (next session pre-open 09:00 Shanghai). It is not "
            "an observation of our own. Revised values require a selected-report PDF match; unproven versions "
            "remain REVISION_REVIEW_REQUIRED."
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
    print(f"{output}: {len(records)} 条候选，{elapsed:.1f}s，{report['counts']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
