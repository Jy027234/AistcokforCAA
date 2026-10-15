"""Machine-admit an explicit Luna official PDF batch using only archived bytes."""
from __future__ import annotations
import argparse
from collections import Counter
from contextlib import closing
from datetime import date, datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
from aquant.adapters.providers.s2_calendar_evidence import verify_calendar_evidence
from aquant.domain.data.db import connect
from aquant.domain.data.forward_archive import ForwardArchive
from aquant.domain.fundamentals.fact_repository import FinancialFactRepository
from aquant.domain.research.s2 import required_s2_periods
from aquant.adapters.providers.cninfo_report_identity import (
    explicit_issuer_company_names, extra_a_share_code_evidence,
    profile_security_code_evidence, IDENTITY_PAGE_LIMIT,
    compound_report_identity,
)
from tools.replay_official_financial_index import SharedArchivedCninfoClient
from tools.promote_s2_machine_facts import _promote_one, _persisted_verified_paths


def save_checkpoint(checkpoint_path, binding, results):
    """Keep complete bytes if Windows rejects the atomic replacement."""
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = checkpoint_path.with_suffix(".tmp")
    temporary.write_text(json.dumps({"binding": binding, "results": results}, ensure_ascii=False, indent=2), encoding="utf-8")
    for attempt in range(3):
        try:
            temporary.replace(checkpoint_path)
            return
        except PermissionError:
            if attempt == 2:
                # The preserved complete .tmp plus committed fact/review IDs
                # allow honest recovery. Do not discard it or report success.
                raise
            time.sleep(0.25)


def company_from_cover(pdf, *, expected_code=None, expected_period_end=None):
    """Read exact full legal names from the archived full report cover pages."""
    from pypdf import PdfReader
    import pdfplumber
    candidates = []
    sources = [(index + 1, page.extract_text() or "", "pypdf")
               for index, page in enumerate(PdfReader(pdf).pages[:3])]
    def find(rows):
        for page, text, method in rows:
            for line in text.splitlines():
                compact = re.sub(r"\s+", "", line)
                match = re.fullmatch(r"([\u4e00-\u9fffA-Za-z（）()]+(?:股份有限公司|股份公司|有限公司))(?:20\d{2}年.*)?", compact)
                if match:
                    candidates.append({"company": match[1], "coverPage": page,
                                       "sourceText": line, "extractor": method})
    find(sources)
    if not candidates:
        with pdfplumber.open(pdf) as document:
            find([(index + 1, page.extract_text() or "", "pdfplumber")
                  for index, page in enumerate(document.pages[:3])])
    names = {row["company"] for row in candidates}
    issuer_pages = None
    if len(names) != 1:
        # A branded prefix or an old footer can make the cover ambiguous.
        # Resolve only from the issuer's explicit legal-name company-info cell.
        reader=PdfReader(pdf)
        issuer_pages=[(i+1,(p.extract_text() or "").splitlines()) for i,p in enumerate(reader.pages[:IDENTITY_PAGE_LIMIT])]
        named=explicit_issuer_company_names(issuer_pages)
        if len({r[2] for r in named}) != 1:
            raise ValueError("report cover lacks one exact full company name")
        page,line,name=named[0]
        identity={"company":name,"coverPage":page,"sourceText":line,
                  "extractor":"pypdf_issuer_company_info"}
    else:
        identity = candidates[0]
    if expected_code is not None:
        if not re.fullmatch(r"\d{6}", expected_code):
            raise ValueError("report identity requires an exact six-digit code")
        reader = PdfReader(pdf)
        code_evidence = []
        for index, page in enumerate(reader.pages[:12]):
            for line in (page.extract_text() or "").splitlines():
                compact = re.sub(r"\s+", "", line)
                if (re.search(r"(?:公司代码|股票代码|证券代码)", compact) and
                        re.search(rf"(?<!\d){expected_code}(?!\d)", compact)):
                    code_evidence.append({"page": index + 1, "sourceText": line})
        if not code_evidence:
            issuer_pages=issuer_pages or [(i+1,(p.extract_text() or "").splitlines())
                                         for i,p in enumerate(reader.pages[:IDENTITY_PAGE_LIMIT])]
            code_evidence=[{"page":p,"sourceText":line,"extractor":"pypdf_a_share_issuer_table"}
                           for p,line in extra_a_share_code_evidence(issuer_pages,expected_code)]
            if not code_evidence:
                code_evidence=[{"page":p,"sourceText":line,"extractor":"pypdf_exact_company_profile"}
                    for p,line in profile_security_code_evidence(issuer_pages,expected_code,company=identity["company"])]
        if not code_evidence:
            # Some original reports have broken pypdf font mappings. The
            # independent text engine must still find an explicit code label.
            with pdfplumber.open(pdf) as document:
                for index, page in enumerate(document.pages[:12]):
                    for line in (page.extract_text() or "").splitlines():
                        compact = re.sub(r"\s+", "", line)
                        if (re.search(r"(?:公司代码|股票代码|证券代码)", compact) and
                                re.search(rf"(?<!\d){expected_code}(?!\d)", compact)):
                            code_evidence.append({"page": index + 1, "sourceText": line,
                                                  "extractor": "pdfplumber"})
        if not code_evidence:
            raise ValueError("report front matter lacks its exact security code")
        identity = {**identity, "securityCode": expected_code,
                    "securityCodeEvidence": code_evidence}
        if expected_period_end is not None:
            issuer_pages=issuer_pages or [(i+1,(p.extract_text() or "").splitlines())
                                         for i,p in enumerate(reader.pages[:IDENTITY_PAGE_LIMIT])]
            chain=compound_report_identity(issuer_pages,expected_code,identity['company'],expected_period_end)
            if chain:
                identity['reportIdentityChain']={"issuerRows":[list(r) for r in chain[0]],
                                                "reportTitleRows":[list(r) for r in chain[1]]}
    return identity


def run(*, discovery_path, pdf_packet_path, fact_db, calendar_path, output_path,
        checkpoint_path, stocks=(), report_keys=(), revision_impact_sources_path=None):
    discovery = json.loads(discovery_path.read_text(encoding="utf-8"))
    packet = json.loads(pdf_packet_path.read_text(encoding="utf-8"))
    if packet["sourceDiscoverySha256"] != "sha256:" + hashlib.sha256(discovery_path.read_bytes()).hexdigest():
        raise ValueError("official PDF batch differs from its discovery binding")
    archive_root = Path(packet["archiveRoot"])
    calendar_packet = json.loads(calendar_path.read_text(encoding="utf-8"))
    pdfs = packet["pdfRecords"]
    if len({(row["instrumentId"], row["periodEnd"]) for row in pdfs}) != len(pdfs):
        raise ValueError("official PDF batch repeats a security-period")
    binding = {"discoveryHash": packet["sourceDiscoverySha256"],
               "pdfPacketHash": "sha256:" + hashlib.sha256(pdf_packet_path.read_bytes()).hexdigest(),
               "factDb": str(fact_db.resolve()), "stocks": list(stocks)}
    revision_sources = {}
    if revision_impact_sources_path is not None:
        source_packet = json.loads(revision_impact_sources_path.read_text(encoding='utf-8'))
        if (not report_keys or source_packet['sourceDiscoverySha256'] != packet['sourceDiscoverySha256'] or
                Path(source_packet['archiveRoot']).resolve() != archive_root.resolve() or
                source_packet['unresolvedMissingPdfCount'] != 0 or source_packet['unresolvedCandidates']):
            raise ValueError('revision impact sources require exact bounded retry and complete matching archived inputs')
        revision_sources = {item['code']: item for item in source_packet['companies']}
        if len(revision_sources) != len(source_packet['companies']):
            raise ValueError('revision impact source packet repeats a security')
        binding['revisionImpactSourceHash'] = 'sha256:' + hashlib.sha256(revision_impact_sources_path.read_bytes()).hexdigest()
    if report_keys:
        requested = {tuple(item.split(":")) for item in report_keys}
        known_demands = {(company["code"], period.isoformat())
            for company in discovery["companies"] if company.get("latestPeriodEnd")
            for periods in required_s2_periods(date.fromisoformat(company["latestPeriodEnd"])).values()
            for period in periods}
        if any(len(item) != 2 for item in requested) or not requested <= known_demands:
            raise ValueError("offline retry must target exact officially discovered security-period demands")
        binding["reportKeys"] = sorted(report_keys)
        binding["parserHashes"] = {
            name: "sha256:" + hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
            for name in (
                "src/aquant/adapters/providers/cninfo_statement_sections.py",
                "src/aquant/adapters/providers/cninfo_statement_geometry.py",
                "src/aquant/adapters/providers/cninfo_report_identity.py",
                "src/aquant/adapters/providers/cninfo_financial_candidate.py",
                "src/aquant/adapters/providers/s2_machine_verification.py",
                "src/aquant/adapters/providers/pdf_promotion.py",
                "src/aquant/adapters/providers/s2_revision_impact.py",
                "tools/promote_s2_machine_facts.py",
                "tools/promote_official_financials_offline.py",
            )
        }
    results = []
    if checkpoint_path.exists():
        previous = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        if previous.get("binding") != binding:
            raise ValueError("offline promotion checkpoint belongs to different frozen inputs")
        results = previous["results"]
    completed = {(row["code"], row["period_end"]) for row in results}
    with closing(connect(Path(packet["archiveDbPath"]))) as con:
        archive = ForwardArchive(con, archive_root)
        client = SharedArchivedCninfoClient(archive, discovery)
        repository = FinancialFactRepository(fact_db)
        if any(fact.pit_mode.value != "LIVE_OBSERVED" for fact in repository.load().facts):
            raise ValueError("official live promotion must use the live-only repository")
        calendar, info = verify_calendar_evidence(calendar_packet, archive, now=datetime.now(timezone.utc))
        invalid_pdfs = {}
        for row in pdfs:
            key = (row["instrumentId"].split(".")[-1], row["periodEnd"])
            try:
                receipt = con.execute("SELECT * FROM fetch_receipt WHERE receipt_id=?", (row["receipt"]["receipt_id"],)).fetchone()
                valid = not (receipt is None or receipt["source_id"] != "cninfo" or receipt["outcome"] != "OK" or
                    receipt["http_status"] != 200 or receipt["url"] != row["documentUrl"] or
                    receipt["content_hash"] != row["contentHash"] or receipt["byte_size"] != row["byteSize"] or
                    any(row["receipt"].get(key) != receipt[key] for key in row["receipt"]) or
                    not archive.verify(row["contentHash"]) or
                    not archive.load_bytes(row["contentHash"]).startswith(b"%PDF-"))
            except (KeyError, OSError, ValueError, TypeError):
                valid = False
            if not valid:
                invalid_pdfs[key] = "official PDF receipt/hash/identity differs from archive or report bytes are absent"
        for company in discovery["companies"]:
            code = company["code"]
            if stocks and code not in stocks:
                continue
            impact_docs = None
            if revision_impact_sources_path is not None and any(key.startswith(code + ':') for key in report_keys):
                source_company = revision_sources.get(code)
                if source_company is None or any(source_company.get(key) != company.get(key) for key in (
                        'code','instrumentId','organizationId','orgLookupReceiptId','orgLookupContentHash',
                        'allCategoryPages','selectedReportRecords','relatedDocumentRecords')):
                    raise ValueError('revision source issuer/index/original relationship metadata differs from discovery')
                impact_docs = source_company['documentsOverride']
            if not company.get("latestPeriodEnd"):
                if (code, None) not in completed:
                    results.append({"code": code, "period_end": None, "fields": [],
                        "status": "DISCOVERY_PERIOD_UNKNOWN", "reason": "official latest report discovery is pending; no period is inferred"})
                    save_checkpoint(checkpoint_path, binding, results)
                continue
            latest = date.fromisoformat(company["latestPeriodEnd"])
            demand = required_s2_periods(latest)
            for period in sorted({period for periods in demand.values() for period in periods}, reverse=True):
                if report_keys and f"{code}:{period.isoformat()}" not in report_keys:
                    continue
                if (code, period.isoformat()) in completed:
                    continue
                fields = tuple(sorted(metric for metric, periods in demand.items() if period in periods))
                report_pdf = next((row for row in pdfs if row["instrumentId"].split(".")[-1] == code
                                  and row["periodEnd"] == period.isoformat()), None)
                # 法律名称随报告真实版本定位；不能把最新更名后的公司名称
                # 硬套到旧报告。原文证券代码与官方索引的 exact sec_code
                # 分别核对，既不引入别名猜测，也不改已有事实/审阅证据。
                identity = None
                if report_pdf is None:
                    result = {"code": code, "period_end": period.isoformat(), "fields": list(fields),
                              "status": "ARCHIVE_MISSING", "reason": "this required original report has not been archived"}
                elif (code, period.isoformat()) in invalid_pdfs:
                    result = {"code": code, "period_end": period.isoformat(), "fields": list(fields),
                              "status": "RECEIPT_INVALID", "reason": invalid_pdfs[(code, period.isoformat())]}
                else:
                    try:
                        identity = company_from_cover(report_pdf["rawArtifactPath"], expected_code=code,expected_period_end=period)
                    except (ValueError, OSError) as exc:
                        result = {"code": code, "period_end": period.isoformat(), "fields": list(fields),
                                  "status": "IDENTITY_FAILED", "reason": f"{type(exc).__name__}: {exc}"}
                if identity is None:
                    results.append(result)
                    save_checkpoint(checkpoint_path, binding, results)
                    print(json.dumps({"code": code, "period": period.isoformat(), "status": result["status"],
                                      "reason": result.get("reason"), "completed": len(results)}, ensure_ascii=False), flush=True)
                    continue
                with closing(connect(fact_db, read_only=True)) as current:
                    has_roots = current.execute(
                        'SELECT 1 FROM financial_fact WHERE instrument_id=? AND period_end=? LIMIT 1',
                        (code, period.isoformat())).fetchone() is not None
                existing = []
                if has_roots:
                    # Existing roots still require the original full acceptance
                    # check. A report with no rows cannot already be admitted;
                    # do not cold-replay unrelated PDFs just to prove that zero.
                    persisted, accepted = repository.load_with_accepted_s2_pdf_reviews()
                    existing = [fact for fact in persisted.facts if fact.instrument_id == code and
                        fact.period_end == period and fact.metric in fields and
                        fact.source_document_id == report_pdf["announcementId"] and
                        fact.content_hash == report_pdf["contentHash"] and fact.version_id in accepted]
                if {fact.metric for fact in existing} == set(fields):
                    result = {"code": code, "period_end": period.isoformat(), "fields": list(fields),
                        "status": "ALREADY_PROMOTED", "reason": "same exact report already admitted; original times preserved",
                        "fact_version_ids": [fact.version_id for fact in existing],
                        "verified_paths": _persisted_verified_paths(repository, code=code, period_end=period)}
                else:
                    result = _promote_one(client=client, archive=archive, repository=repository,
                        code=code, company=identity["company"], period_end=period, fields=fields,
                        calendar=calendar, cross_through=date.fromisoformat(discovery["observedThroughBeijingDate"]),
                        max_pages=100, commit=True, now=datetime.now(timezone.utc),
                        revision_impact_company=company if impact_docs is not None else None,
                        revision_impact_documents=impact_docs)
                result["companyIdentityEvidence"] = {**identity, "pdfContentHash": report_pdf["contentHash"],
                                                    "pdfReceiptId": report_pdf["receipt"]["receipt_id"]}
                results.append(result)
                save_checkpoint(checkpoint_path, binding, results)
                print(json.dumps({"code": code, "period": period.isoformat(), "status": result["status"],
                                  "reason": result.get("reason"), "completed": len(results)}, ensure_ascii=False), flush=True)
    report = {"schema": "aquant.s2-machine-promotion-run.v1", "generated_at": datetime.now(timezone.utc).isoformat(),
        "committed": True, "fact_db": str(fact_db.resolve()), "archive_root": str(archive_root),
        "providerRequestsStarted": 0, "binding": binding, "counts": dict(Counter(row["status"] for row in results)),
        "results": results, "calendarEvidencePath": str(calendar_path.resolve()),
        "discoveryScopeSecurityCount": len(discovery["companies"]),
        "processingSecurityCount": len([company for company in discovery["companies"] if not stocks or company["code"] in stocks]),
        "scopeNote": "Processing shards preserve the declared discovery scope; missing reports/latest periods stay explicit and do not enter accepted coverage."}
    with output_path.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--discovery", type=Path, required=True)
    parser.add_argument("--pdf-packet", type=Path, required=True)
    parser.add_argument("--facts-db", type=Path, default=ROOT / "data/s2-pdf-review/s2-live-observed-facts.sqlite")
    parser.add_argument("--calendar", type=Path, default=ROOT / "data/s2-pdf-review/calendar-evidence-20261009.json")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--stock", action="append", default=[])
    parser.add_argument("--report-key", action="append", default=[],
                        help="Retry only an exact archived code:YYYY-MM-DD pair; new checkpoint/output required")
    parser.add_argument('--revision-impact-sources', type=Path,
                        help='Complete immutable actual selected/related/alternate PDF source packet for exact report retries')
    args = parser.parse_args(argv)
    run(discovery_path=args.discovery, pdf_packet_path=args.pdf_packet, fact_db=args.facts_db,
        calendar_path=args.calendar, output_path=args.output, checkpoint_path=args.checkpoint,
        stocks=tuple(args.stock), report_keys=tuple(args.report_key),
        revision_impact_sources_path=args.revision_impact_sources)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
