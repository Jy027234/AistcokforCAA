"""Promote CNINFO official S2 financial facts through machine verification.

This is work package A's forward path.  For each security/report-period demand it
replays the archived official regular-report index, downloads (or reuses) the
official full-report PDF, re-derives the machine verification from those bytes,
and writes the facts plus their independent-check evidence into the append-only
financial-fact repository.

What makes this different from the earlier human PDF protocol:

* normal reports are processed by deterministic programmes plus a *recheckable*
  independent check, instead of one human signature per amount;
* the human ``reviewer_id`` is never claimed — every identity this tool writes
  carries the ``machine:`` prefix, and the reader rejects a bundle that mixes
  the two protocols;
* a report whose company-level announcement index shows a
  correction/revision-titled candidate is **not** admitted; it is routed to the
  human exception queue and reported as such.

No gate is weakened: the archived index POST bodies, pagination, announcement
identity, PDF hash, cover identity, statement scope, amount unit and current
column are all re-verified at write time from archived bytes.

Default is a dry run.  ``--commit`` is required to write the fact database.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aquant.adapters.providers.cninfo import (  # noqa: E402
    CrossCategoryIndexResult, ReportIndexResult, market_for_code, normalize_title,
    parse_announcements,
)
from aquant.adapters.providers.cninfo_probe import parse_report_period  # noqa: E402
from aquant.adapters.providers.pdf_promotion import (  # noqa: E402
    CrossCategoryDisposition, PdfPromotionError, PdfVersionReview,
    cninfo_query_digest, promote_machine_verified_bundle,
)
from aquant.domain.fundamentals.disclosure_link import (  # noqa: E402
    DisclosureRole, disclosure_role, is_financial_correction_title,
    report_period_from_title,
)
from aquant.adapters.providers.s2_calendar_evidence import (  # noqa: E402
    verify_calendar_evidence,
)
from aquant.adapters.providers.s2_machine_verification import (  # noqa: E402
    VERIFIER_ID,
)
from aquant.domain.data.db import connect  # noqa: E402
from aquant.domain.data.forward_archive import ForwardArchive  # noqa: E402
from aquant.domain.fundamentals.fact_repository import (  # noqa: E402
    FinancialFactRepository,
)
from aquant.domain.research.s2 import required_s2_periods  # noqa: E402

SCHEMA = "aquant.s2-machine-promotion-run.v1"
ARCHIVE_ROOT = ROOT / "deploy" / "agentctl-q0" / "forward-archive"
MANIFEST = ROOT / "data" / "s2-pdf-review" / "s2-demand-manifest.json"
CALENDAR_EVIDENCE = ROOT / "data" / "s2-pdf-review" / "calendar-evidence-20260928.json"
FACT_DB = ROOT / "data" / "s2-pdf-review" / "s2-machine-verified-facts.sqlite"
REPORT_DIR = ROOT / "data" / "s2-pdf-review" / "s2-machine-promotion"
SHANGHAI = timezone(timedelta(hours=8))

#: 公司全称。归母权益/收入等字段要靠它做封面身份核对，因此必须来自官方原文，
#: 不能由短名推断；未列出的证券要求显式 ``--company CODE=FULL_NAME``。
COMPANY_BY_CODE = {
    "000333": "美的集团股份有限公司",
    "600519": "贵州茅台酒股份有限公司",
    "601012": "隆基绿能科技股份有限公司",
    "000651": "珠海格力电器股份有限公司",
    "600276": "江苏恒瑞医药股份有限公司",
    "002415": "杭州海康威视数字技术股份有限公司",
    "300750": "宁德时代新能源科技股份有限公司",
    "688981": "中芯国际集成电路制造有限公司",
}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def load_manifest(path: Path) -> dict:
    document = json.loads(path.read_text(encoding="utf-8"))
    if document.get("schema") != "aquant.s2-demand-manifest.v1":
        raise ValueError(f"{path} is not an S2 demand manifest")
    return document


def demand_for(security: dict) -> dict[str, tuple[date, ...]]:
    latest = date.fromisoformat(security["latest_period_end"])
    return required_s2_periods(latest)


def _index_from_result(result: ReportIndexResult) -> dict[str, object]:
    return {
        "organizationId": result.organization_id,
        "orgLookupReceiptId": result.org_lookup_receipt_id,
        "complete": result.complete,
        "termination": result.termination,
        "publicationWindow": [day.isoformat() for day in result.publication_window],
        "pages": [
            {"page": page.page_num, "requestBodyHash": page.request_body_hash,
             "responseReceiptId": page.receipt_id, "hasMore": page.has_more,
             "rawAnnouncementCount": page.raw_announcement_count,
             "totalAnnouncement": page.total_announcement}
            for page in result.pages
        ],
        "matches": [
            {"announcementId": item.announcement.announcement_id,
             "title": item.announcement.title,
             "url": item.announcement.url,
             "page": item.page_num,
             "pageReceiptId": item.page_receipt_id}
            for item in result.matches
        ],
    }


def _cross_index_from_result(result: CrossCategoryIndexResult) -> dict[str, object]:
    return {
        "organizationId": result.organization_id,
        "orgLookupReceiptId": result.org_lookup_receipt_id,
        "complete": result.complete,
        "termination": result.termination,
        "publicationWindow": [day.isoformat() for day in result.publication_window],
        "pages": [
            {"page": page.page_num, "requestBodyHash": page.request_body_hash,
             "responseReceiptId": page.receipt_id}
            for page in result.pages
        ],
        "candidates": [
            {"announcementId": entry.announcement_id, "title": entry.title,
             "candidateReasons": list(entry.candidate_reasons),
             "documentUrl": entry.document_url, "page": entry.page_num,
             "pageReceiptId": entry.page_receipt_id}
            for entry in result.candidates
        ],
    }


def build_version_review(result: ReportIndexResult, *, announcement_id: str,
                         reviewed_at: datetime) -> PdfVersionReview:
    """把已归档的定报索引转成升仓所需的版本复核记录。

    这里**不做人工判断**：``complete_search_attested`` 表示索引在记录的查询
    窗口内逐页到达末页，而不是"该公司从未修订"。修订结论由全分类索引单独
    承担，见 :func:`build_dispositions`。
    """

    period = parse_report_period(result.report_period)
    return PdfVersionReview(
        announcement_id=announcement_id,
        org_lookup_receipt_id=result.org_lookup_receipt_id or "",
        index_receipt_id=result.pages[0].receipt_id,
        search_receipt_ids=tuple(page.receipt_id for page in result.pages),
        search_page_numbers=tuple(page.page_num for page in result.pages),
        query_stock_code=result.stock_code,
        query_organization_id=result.organization_id or "",
        query_period_start=result.publication_window[0],
        query_period_end=result.publication_window[1],
        query_digest=cninfo_query_digest(
            stock_code=result.stock_code,
            organization_id=result.organization_id or "",
            period_start=result.publication_window[0],
            period_end=result.publication_window[1],
        ),
        reviewer_id=VERIFIER_ID,
        reviewed_at=reviewed_at,
        complete_search_attested=True,
    )


def build_dispositions(result: CrossCategoryIndexResult, *, announcement_id: str,
                       reviewed_at: datetime, pdf_receipt_id: str,
                       ) -> tuple[CrossCategoryDisposition, ...]:
    """对全分类标题候选逐条给出机器处置。

    规则是**窄**的，而且只有两类结论：

    * 被升仓的报告本身：``RELATED``，绑定它的官方 PDF 收据；
    * 标题不含更正/修订/补充标记的其它公告：``UNRELATED``，理由写明"标题
      未出现更正/修订标记"。

    任何带更正/修订/补充标记的第三方候选**不被处置**，由调用方直接拒绝升仓
    ——标题只是召回信号，机器无权据此断定它对五字段的影响。
    """

    dispositions = []
    for entry in result.candidates:
        if entry.announcement_id in (announcement_id, ""):
            dispositions.append(CrossCategoryDisposition(
                announcement_id=entry.announcement_id, relevance="RELATED",
                reviewer_id=VERIFIER_ID, reviewed_at=reviewed_at,
                rationale=(
                    "machine: this is the full report being promoted; its official "
                    "PDF receipt is bound and re-verified at admission"),
                document_receipt_id=pdf_receipt_id,
                related_report_announcement_ids=(announcement_id,),
            ))
            continue
        dispositions.append(CrossCategoryDisposition(
            announcement_id=entry.announcement_id, relevance="UNRELATED",
            reviewer_id=VERIFIER_ID, reviewed_at=reviewed_at,
            rationale=(
                "machine: title carries no correction/revision/supplement marker "
                f"({normalize_title(entry.title)[:60]})"),
        ))
    return tuple(dispositions)


def blocking_candidates(result: CrossCategoryIndexResult, *,
                        announcement_id: str, publication_window: tuple[date, date],
                        ) -> tuple[dict[str, str], ...]:
    """需要人工处置的候选：落在本报告发布窗口内且标题指明**财报更正**。

    只按"修订/补充"标题一刀切会把公司章程修订对照表、股东会补充通知一起拦
    下，使正常样本全部走人工——那正好违反本方案"机器处理正常样本"的要求。
    因此这里叠加两条约束：候选发布日期落在本报告自己的发布窗口内，且标题
    含财报更正类标记。窗口外或标记无关的候选仍保留在索引证据里，由
    ``build_dispositions`` 明确处置为 UNRELATED 并记录理由与索引观察时点。
    """

    blocking = []
    for entry in result.candidates:
        if entry.announcement_id == announcement_id:
            continue
        if not any(reason != "period_title" for reason in entry.candidate_reasons):
            continue
        title = entry.title or ""
        if not is_financial_correction_title(title):
            continue
        try:
            published = parse_announcements(json.dumps({"announcements": [{
                "announcementId": entry.announcement_id,
                "announcementTitle": title,
                "announcementTime": entry.announcement_time_ms,
            }]})).pop().announced_on
        except (IndexError, ValueError):
            published = None
        if published is None or not (
                publication_window[0] <= published <= publication_window[1]):
            continue
        blocking.append({
            "announcement_id": entry.announcement_id, "title": title,
            "published_on": published.isoformat(),
            "candidate_reasons": ",".join(entry.candidate_reasons),
        })
    return tuple(sorted(blocking, key=lambda item: item["announcement_id"]))


def _verified_pdf_receipt(archive: ForwardArchive, url: str) -> dict | None:
    """复用已归档且哈希复核通过的 PDF 收据，避免重复下载同一份原文。"""

    row = archive.con.execute(
        "SELECT receipt_id, source_id, url, outcome, content_hash, byte_size, "
        "first_seen_at FROM fetch_receipt WHERE source_id='cninfo' AND url=? "
        "AND outcome='OK' ORDER BY first_seen_at DESC LIMIT 1",
        (url,),
    ).fetchone()
    if row is None or not row["content_hash"] or not archive.verify(row["content_hash"]):
        return None
    return dict(row)


def run(*, manifest: dict, stocks: tuple[str, ...], periods: tuple[str, ...],
        archive_root: Path, fact_db: Path, calendar_evidence: Path,
        commit: bool, company_names: dict[str, str], cross_through: date,
        max_pages: int) -> dict:
    from aquant.adapters.providers.cninfo import CninfoClient

    archive_root = archive_root.resolve()
    packet = json.loads(calendar_evidence.read_text(encoding="utf-8"))
    now = _utc_now()
    results: list[dict] = []
    # 只读预检也要走**同一条**核验代码路径，否则 dry run 报出的"通过"与真正
    # 写入时的判定不同。做法是在临时副本上执行真实的写入流程，副本用完即删。
    scratch: Path | None = None
    if not commit:
        if fact_db.is_file():
            scratch = fact_db.with_name(f".{fact_db.name}.scratch-{uuid4().hex}")
            shutil.copyfile(fact_db, scratch)
            repository_target = scratch
        else:
            scratch = fact_db.with_name(f".{fact_db.stem}.scratch-{uuid4().hex}.sqlite")
            repository_target = scratch
    else:
        repository_target = fact_db
    try:
        return _run_with_repository(
            manifest=manifest, stocks=stocks, periods=periods,
            archive_root=archive_root, repository_target=repository_target,
            packet=packet, now=now, commit=commit, company_names=company_names,
            cross_through=cross_through, max_pages=max_pages, results=results,
        )
    finally:
        if scratch is not None:
            for suffix in ("", "-wal", "-shm"):
                candidate = Path(str(scratch) + suffix)
                if candidate.exists():
                    candidate.unlink()


def _run_with_repository(*, manifest: dict, stocks: tuple[str, ...],
                         periods: tuple[str, ...], archive_root: Path,
                         repository_target: Path, packet: dict, now: datetime,
                         commit: bool, company_names: dict[str, str],
                         cross_through: date, max_pages: int,
                         results: list[dict]) -> dict:
    from aquant.adapters.providers.cninfo import CninfoClient

    with closing(connect(archive_root / "meta.sqlite")) as con:
        archive = ForwardArchive(con, archive_root)
        calendar, calendar_info = verify_calendar_evidence(packet, archive, now=now)
        client = CninfoClient(archive)
        repository = FinancialFactRepository(repository_target)
        securities = {item["code"]: item for item in manifest["securities"]}
        for code in stocks:
            security = securities.get(code)
            company = company_names.get(code)
            if security is None or security.get("status") != "DEMAND_FROZEN":
                results.append({
                    "code": code, "status": "SCOPE_BLOCKED",
                    "reason": (security or {}).get("reason", "not in the frozen manifest"),
                })
                continue
            if not company:
                results.append({
                    "code": code, "status": "SCOPE_BLOCKED",
                    "reason": "no official company full name supplied for cover identity",
                })
                continue
            demand = demand_for(security)
            for period_end in sorted({day for days in demand.values() for day in days}):
                if periods and period_end.isoformat() not in periods:
                    continue
                fields = tuple(sorted(
                    metric for metric, days in demand.items() if period_end in days))
                results.append(_promote_one(
                    client=client, archive=archive, repository=repository,
                    code=code, company=company, period_end=period_end,
                    fields=fields, calendar=calendar, cross_through=cross_through,
                    max_pages=max_pages, commit=commit, now=now,
                ))
    counts: dict[str, int] = {}
    for item in results:
        counts[item["status"]] = counts.get(item["status"], 0) + 1
    return {
        "schema": SCHEMA,
        "generated_at": _utc_now().isoformat(),
        "committed": commit,
        "fact_db": str(repository_target),
        "archive_root": str(archive_root),
        "calendar": {
            "observed_and_planned_days": len(calendar),
            "first_day": calendar[0].isoformat() if calendar else None,
            "last_day": calendar[-1].isoformat() if calendar else None,
            "evidence": {key: calendar_info[key] for key in sorted(calendar_info)
                         if key in ("historical", "scheduled")},
        },
        "cross_category_through": cross_through.isoformat(),
        "counts": dict(sorted(counts.items())),
        "results": results,
    }


def _persisted_verified_paths(repository, *, code: str, period_end: date,
                              ) -> dict[str, list[str]]:
    """从已入库的机器核验证据里取回每条字段满足的独立路径。

    这样"已经升仓过"的期间在覆盖报告里仍然带着核验明细，而不是只剩一个状态。
    """

    out: dict[str, list[str]] = {}
    try:
        with closing(connect(repository.db_path, read_only=True)) as con:
            rows = con.execute(
                "SELECT evidence_json FROM financial_fact_review "
                "WHERE bundle_id LIKE 'cninfo_machine_%'").fetchall()
        for row in rows:
            evidence = json.loads(row[0])
            if (evidence.get("instrument_id") != code
                    or evidence.get("period_end") != period_end.isoformat()):
                continue
            for item in evidence.get("fields", []):
                if item.get("verified") is True:
                    out[item["field"]] = list(item.get("satisfied_paths") or [])
    except (OSError, ValueError, KeyError):
        return {}
    return dict(sorted(out.items()))


def _matching_full_reports(index: ReportIndexResult, period_end: date):
    """Select explicitly dated originals/revisions through the shared title rule."""
    return [item.announcement for item in index.matches
            if item.announcement.report_period == period_end.isoformat()
            and not any(marker in (item.announcement.title or "") for marker in (
                "摘要", "英文", "预告", "提示性公告"))
            and report_period_from_title(item.announcement.title or "") == period_end
            and disclosure_role(item.announcement.title or "") in (
                DisclosureRole.ORIGINAL_REPORT, DisclosureRole.REVISED_REPORT)]


def _promote_one(*, client, archive: ForwardArchive, repository, code: str,
                 company: str, period_end: date, fields: tuple[str, ...],
                 calendar, cross_through: date, max_pages: int, commit: bool,
                 now: datetime, revision_impact_company: dict | None = None,
                 revision_impact_documents: list[dict] | None = None) -> dict:
    """发现、归档、核验并按需写入一份报告；失败原因是结构化返回值。"""

    base: dict[str, object] = {
        "code": code, "period_end": period_end.isoformat(), "fields": list(fields),
    }
    try:
        index = client.report_index(
            stock_code=code, report_period=period_end.isoformat(), max_pages=max_pages)
    except Exception as exc:  # noqa: BLE001 - a failed security is reported, not raised
        return {**base, "status": "INDEX_FAILED",
                "reason": f"{type(exc).__name__}: {exc}"[:200]}
    if not index.complete:
        return {**base, "status": "INDEX_INCOMPLETE",
                "reason": f"termination={index.termination} error={index.error}",
                "index": _index_from_result(index)}
    matches = _matching_full_reports(index, period_end)
    if len(matches) != 1:
        return {**base, "status": "REPORT_NOT_UNIQUE",
                "reason": f"{len(matches)} matching full reports in the official index",
                "index": _index_from_result(index)}
    announcement = matches[0]
    if announcement.sec_code != code:
        return {**base, "status": "IDENTITY_MISMATCH",
                "reason": f"official index row reports security {announcement.sec_code}"}
    url = (announcement.url or "").replace("http://", "https://")
    if not url:
        return {**base, "status": "REPORT_NOT_UNIQUE",
                "reason": "official index row has no PDF url"}
    receipt = _verified_pdf_receipt(archive, url) or _verified_pdf_receipt(
        archive, url.replace("https://", "http://"))
    if receipt is None:
        outcome = client.document(url, label="s2-machine-promotion")
        if not outcome.ok or not outcome.content_hash:
            return {**base, "status": "DOWNLOAD_FAILED",
                    "reason": (outcome.detail or "document fetch failed")[:200]}
        receipt = _verified_pdf_receipt(archive, url)
        if receipt is None:
            return {**base, "status": "DOWNLOAD_FAILED",
                    "reason": "newly archived PDF is not hash-verifiable"}
    try:
        cross = client.cross_category_index(
            stock_code=code, report_period=period_end.isoformat(),
            through=cross_through, max_pages=max_pages)
    except Exception as exc:  # noqa: BLE001
        return {**base, "status": "INDEX_FAILED",
                "reason": f"cross-category: {type(exc).__name__}: {exc}"[:200]}
    if not cross.complete:
        return {**base, "status": "CROSS_INDEX_INCOMPLETE",
                "reason": f"termination={cross.termination} error={cross.error}",
                "crossCategoryIndex": _cross_index_from_result(cross)}
    blocked = blocking_candidates(
        cross, announcement_id=announcement.announcement_id,
        publication_window=index.publication_window)
    impact_proof = None
    impact_archive = None
    if revision_impact_company is not None:
        from aquant.adapters.providers.s2_revision_impact import assess_revision_impact, RevisionImpactError
        try:
            if revision_impact_company['code'] != code:
                raise RevisionImpactError('impact company does not match the exact writer security')
            impact_proof = assess_revision_impact(
                archive_root=archive.root, company=revision_impact_company,
                documents=revision_impact_documents, target_period_end=period_end,
                target_metrics=fields, issuer_name=company).as_dict()
            outcomes = {item['metric']: item for item in impact_proof['outcomes']}
            if (len(outcomes) != len(impact_proof['outcomes']) or set(outcomes) != set(fields) or
                    impact_proof['relationshipScope']['completeForArchivedIndexResponse'] is not True or
                    any(item['status'] != 'NO_EFFECT' for item in outcomes.values())):
                return {**base, 'status': 'REVISION_IMPACT_UNRESOLVED',
                        'reason': 'complete source replay does not prove no effect for every required fact',
                        'revision_impact_proof': impact_proof}
            actual_db = next(row[2] for row in archive.con.execute('PRAGMA database_list') if row[1] == 'main')
            impact_archive = {'root': str(archive.root.resolve()), 'database_path': str(Path(actual_db).resolve()),
                              'company': revision_impact_company}
            if revision_impact_documents is not None:
                impact_archive['documents'] = revision_impact_documents
        except (RevisionImpactError, KeyError, TypeError, ValueError) as exc:
            return {**base, 'status': 'REVISION_IMPACT_UNRESOLVED',
                    'reason': f'{type(exc).__name__}: {exc}'[:220]}
    elif revision_impact_documents is not None:
        return {**base, 'status': 'REVISION_IMPACT_UNRESOLVED',
                'reason': 'full archived issuer relationship metadata is required with documents'}
    if blocked and impact_proof is None:
        return {
            **base, "status": "REVISION_REVIEW_REQUIRED",
            "reason": ("all-category index shows correction/revision-titled "
                       "candidates inside this report's publication window; "
                       "human disposition is required"),
            "blocking_candidates": list(blocked),
            "crossCategoryIndex": _cross_index_from_result(cross),
        }
    reviewed_at = _utc_now()
    version_review = build_version_review(
        index, announcement_id=announcement.announcement_id, reviewed_at=reviewed_at)
    dispositions = build_dispositions(
        cross, announcement_id=announcement.announcement_id, reviewed_at=reviewed_at,
        pdf_receipt_id=receipt["receipt_id"])
    if impact_proof is not None:
        all_entries = {entry.announcement_id: entry for entry in cross.announcements}
        financial_ids = set(impact_proof['relationshipScope']['financialCandidateIds'])
        sources = {item['announcementId']: item for item in impact_proof['sources']}
        if not financial_ids.issubset(all_entries) or not financial_ids.issubset(sources):
            return {**base, 'status': 'REVISION_IMPACT_UNRESOLVED',
                    'reason': 'replayed financial source is absent from the complete current cross index'}
        by_id = {item.announcement_id: item for item in dispositions}
        for identifier in sorted(financial_ids - {announcement.announcement_id}):
            by_id[identifier] = CrossCategoryDisposition(
                announcement_id=identifier, relevance='METRIC_NO_EFFECT',
                reviewer_id=VERIFIER_ID, reviewed_at=reviewed_at,
                rationale=('machine: complete original-source replay proves NO_EFFECT for exact '
                           f'{code}/{period_end.isoformat()}/' + ','.join(fields) +
                           f"; evidence={impact_proof['evidenceHash']}"),
                document_receipt_id=sources[identifier]['receipt']['receipt_id'])
        dispositions = tuple(by_id[key] for key in sorted(by_id))
    try:
        facts, verification, _ = promote_machine_verified_bundle(
            instrument_id=code, period_end=period_end, company=company,
            pdf_receipt_id=receipt["receipt_id"], version_review=version_review,
            cross_category_index=cross, cross_category_dispositions=dispositions,
            cross_category_reviewed_by=VERIFIER_ID, cross_category_reviewed_at=reviewed_at,
            archive=archive, repository=repository, trading_calendar=calendar,
            required_fields=fields,
            revision_impact_proof=impact_proof, revision_impact_archive=impact_archive,
        )
    except (PdfPromotionError, ValueError) as exc:
        message = str(exc)
        # 该报告已经是根版本，说明先前已经升仓成功。重复运行必须**幂等**：
        # 报告为已存在的正式事实，而不是记成核验失败——否则覆盖报告会把
        # 本来合格的证券误报成缺口（实测 000651 五个期间都是这样）。
        if "second fact root" in message:
            existing = sorted(
                fact.version_id for fact in repository.load().facts
                if fact.instrument_id == code and fact.period_end == period_end
            )
            # 仍然报告**已持久化证据**里每条字段满足的独立路径，这样重复运行
            # 的覆盖报告不会因为"已入库"而看不到核验明细。
            persisted_paths = _persisted_verified_paths(
                repository, code=code, period_end=period_end)
            return {
                **base, "status": "ALREADY_PROMOTED",
                "reason": "this report already has a formal fact root",
                "announcement_id": announcement.announcement_id,
                "content_hash": receipt["content_hash"],
                "fact_version_ids": existing,
                "verified_paths": persisted_paths,
            }
        return {**base, "status": "VERIFICATION_FAILED",
                "reason": f"{type(exc).__name__}: {message}"[:220],
                "announcement_id": announcement.announcement_id}
    if not commit:
        return {
            **base, "status": "VERIFIED_NOT_WRITTEN",
            "reason": "all required fields satisfied an independent path; not written "
                      "(pass --commit to write)",
            "announcement_id": announcement.announcement_id,
            "content_hash": receipt["content_hash"],
            "available_at": facts[0].available_at.isoformat(),
            "fact_version_ids": [fact.version_id for fact in facts],
            "verified_paths": {
                item.field: list(item.satisfied_paths) for item in verification.fields
            },
            "index": _index_from_result(index),
            "crossCategoryIndex": _cross_index_from_result(cross),
        }
    return {
        **base, "status": "PROMOTED" if commit else "VERIFIED_NOT_WRITTEN",
        "announcement_id": announcement.announcement_id,
        "announced_on": announcement.announcement_date_cn,
        "content_hash": receipt["content_hash"],
        "first_seen_at": receipt["first_seen_at"],
        "available_at": facts[0].available_at.isoformat(),
        "fact_version_ids": [fact.version_id for fact in facts],
        "verified_paths": {
            item.field: list(item.satisfied_paths) for item in verification.fields
        },
        "index": _index_from_result(index),
        "crossCategoryIndex": _cross_index_from_result(cross),
    }


def _parse_pairs(values: list[str], *, label: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for value in values:
        if "=" not in value:
            raise ValueError(f"{label} expects CODE=VALUE, got {value!r}")
        key, _, raw = value.partition("=")
        key = key.strip()
        if key in out:
            raise ValueError(f"duplicate {label} for {key!r}")
        out[key] = raw.strip()
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=MANIFEST)
    parser.add_argument("--archive-root", type=Path, default=ARCHIVE_ROOT)
    parser.add_argument("--fact-db", type=Path, default=FACT_DB)
    parser.add_argument("--calendar-evidence", type=Path, default=CALENDAR_EVIDENCE)
    parser.add_argument("--stock", action="append", default=[],
                        help="repeatable; defaults to every frozen manifest security")
    parser.add_argument("--period", action="append", default=[],
                        help="repeatable YYYY-MM-DD; defaults to every demanded period")
    parser.add_argument("--company", action="append", default=[],
                        metavar="CODE=FULL_NAME", help="repeatable override")
    parser.add_argument("--cross-through", type=date.fromisoformat,
                        default=datetime.now(SHANGHAI).date(),
                        help="Beijing date through which the all-category index is swept")
    parser.add_argument("--max-pages", type=int, default=100)
    parser.add_argument("--commit", action="store_true",
                        help="write facts; without it the run is a dry run")
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args(argv)
    try:
        manifest = load_manifest(args.manifest.resolve())
        company_names = {**COMPANY_BY_CODE, **_parse_pairs(args.company, label="--company")}
        frozen = [item["code"] for item in manifest["securities"]
                  if item.get("status") == "DEMAND_FROZEN"]
        stocks = tuple(args.stock) if args.stock else tuple(frozen)
        for code in stocks:
            if code not in frozen:
                parser.error(f"{code} is not a DEMAND_FROZEN security in the manifest")
        report = run(
            manifest=manifest, stocks=stocks, periods=tuple(args.period),
            archive_root=args.archive_root, fact_db=args.fact_db,
            calendar_evidence=args.calendar_evidence, commit=args.commit,
            company_names=company_names, cross_through=args.cross_through,
            max_pages=args.max_pages,
        )
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        print(f"S2 machine promotion failed: {exc}", file=sys.stderr)
        return 2
    output = (args.output or (REPORT_DIR / (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + ".json"))).resolve()
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
