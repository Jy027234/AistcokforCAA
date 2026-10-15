"""核对深交所备源能否为某份报告提供**字节同一**的官方原文。

实施方案的来源路由要求：来源切换按**一份报告**进行，并且不能逐字段静默拼源。
同站/跨站相同 PDF 哈希证明原文字节一致、来源可互证，**但不等价于两份独立的
会计证据**。因此这个工具只做一件事并如实报告结论：

1. 用深交所检索接口按证券与日期窗口发现报告；
2. 下载候选原文并归档（保留请求体哈希、响应收据与内容哈希）；
3. 与已归档的巨潮原文逐字节比较；
4. 输出"能否作为该报告的字节备源"，以及**明确写在结论里的边界**。

它不写财务事实、不改巨潮身份、不做逐字段拼源。当两站哈希不同时，它不自动
选择"时间更晚"的一份，而是标记为需要判定修订/摘要/不同期间。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aquant.adapters.providers.szse import (  # noqa: E402
    CHANNEL_FIXED, SzseClient, SzseError, cross_source_evidence, default_policy,
)
from aquant.domain.data.db import connect  # noqa: E402
from aquant.domain.data.forward_archive import ForwardArchive  # noqa: E402

SCHEMA = "aquant.s2-szse-fallback-check.v1"
ARCHIVE_ROOT = ROOT / "deploy" / "agentctl-q0" / "forward-archive"
CANDIDATES = ROOT / "data" / "s2-pdf-review" / "cninfo-industrial-candidates.json"
DEFAULT_OUTPUT = ROOT / "data" / "s2-pdf-review" / "szse-fallback-check.json"
SHANGHAI = timezone(timedelta(hours=8))
#: 检索窗口按公告日附近放宽，避免交易所口径的时区/边界差异漏掉目标。
WINDOW_BEFORE_DAYS = 2
WINDOW_AFTER_DAYS = 2
#: 全文 PDF 的体积上限。策略默认上限（16 MiB）是按 JSON 响应设的，年报常有
#: 200+ 页、远超它；这里与巨潮路径保持同一量级（64 MiB），仍是有界读取。
DOCUMENT_MAX_BYTES = 64 * 1024 * 1024
#: 每个窗口最多试下载几份全文。窗口会命中同公司其它定期报告，身份由内容
#: 哈希判定；设上限避免一份报告把整个窗口的 PDF 都拉下来。
MAX_CANDIDATE_DOWNLOADS = 3
_CNINFO_PDF = re.compile(
    r"^https?://static\.cninfo\.com\.cn/finalpage/(\d{4}-\d{2}-\d{2})/(\d+)\.PDF$")


def _load_candidates(path: Path, *, code: str | None = None) -> dict[tuple[str, str], dict]:
    """读入候选清单。两种真实形状都要认：

    * 工业候选清单：每行有 ``instrumentId``；
    * 早期单公司试点文件（``s2-official-pdf-pilot-*.json``）：**没有**
      ``instrumentId``（整份文件只讲一只证券），因此需要 ``--extra-code``
      显式给出证券代码。
    """

    payload = json.loads(path.read_text(encoding="utf-8"))
    out: dict[tuple[str, str], dict] = {}
    for row in payload.get("reports", []):
        instrument = row.get("instrumentId") or code
        period = row.get("periodEnd")
        if instrument and period and row.get("contentHash"):
            out[(instrument, period)] = {**row, "instrumentId": instrument}
    return out


def _cninfo_bytes(archive: ForwardArchive, content_hash: str) -> bytes | None:
    if not archive.verify(content_hash):
        return None
    return archive.load_bytes(content_hash)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidates", type=Path, default=CANDIDATES)
    parser.add_argument("--archive-root", type=Path, default=ARCHIVE_ROOT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--stock", action="append", default=[],
                        help="repeatable six-digit code; 默认取候选清单里全部")
    parser.add_argument("--period", action="append", default=[],
                        help="repeatable YYYY-MM-DD; 默认全部")
    parser.add_argument("--trusted-proxy-network", action="append", default=[])
    parser.add_argument("--extra-candidates", type=Path, action="append", default=[],
                        help="repeatable; 追加候选清单（如单公司试点文件）")
    parser.add_argument("--extra-code", action="append", default=[],
                        help="repeatable CODE=PATH; 该试点文件对应的证券代码"
                             "（试点文件本身没有 instrumentId）")
    args = parser.parse_args(argv)
    if args.trusted_proxy_network:
        os.environ["AQUANT_TRUSTED_PROXY_NETWORKS"] = ",".join(
            args.trusted_proxy_network)

    candidates = _load_candidates(args.candidates.resolve())
    for raw in args.extra_candidates:
        candidates.update(_load_candidates(raw.resolve()))
    for pair in args.extra_code:
        code, _, path = pair.partition("=")
        if not code or not path:
            parser.error("--extra-code expects CODE=PATH")
        candidates.update(_load_candidates(Path(path).resolve(), code=code))
    pairs = [
        key for key in sorted(candidates)
        if (not args.stock or key[0] in set(args.stock))
        and (not args.period or key[1] in set(args.period))
    ]
    if not pairs:
        print("no candidate report selected", file=sys.stderr)
        return 2

    archive_root = args.archive_root.resolve()
    results: list[dict[str, object]] = []
    counts: dict[str, int] = {}
    with closing(connect(archive_root / "meta.sqlite")) as con:
        archive = ForwardArchive(con, archive_root)
        client = SzseClient(archive, policy=default_policy())
        for code, period in pairs:
            row = candidates[(code, period)]
            record: dict[str, object] = {
                "code": code, "period_end": period,
                "cninfo_announcement_id": row.get("announcementId"),
                "cninfo_content_hash": row.get("contentHash"),
                "cninfo_document_url": row.get("documentUrl"),
            }
            published = row.get("announcedOn")
            if not published:
                match = _CNINFO_PDF.match(str(row.get("documentUrl") or ""))
                published = match.group(1) if match else None
            if not published:
                record["status"] = "NO_PUBLICATION_DATE"
                results.append(record)
                counts["NO_PUBLICATION_DATE"] = counts.get("NO_PUBLICATION_DATE", 0) + 1
                continue
            day = date.fromisoformat(published)
            index = client.index(
                stock_code=code,
                begin=day - timedelta(days=WINDOW_BEFORE_DAYS),
                end=day + timedelta(days=WINDOW_AFTER_DAYS),
                channel=CHANNEL_FIXED, max_pages=10,
            )
            record["szse_index"] = {
                "complete": index.complete, "termination": index.termination,
                "error": index.error,
                "pages": [
                    {"page": page.page, "requestBodyHash": page.request_body_hash,
                     "responseReceiptId": page.receipt_id,
                     "responseContentHash": page.content_hash}
                    for page in index.pages
                ],
            }
            if not index.complete:
                record["status"] = "SZSE_INDEX_INCOMPLETE"
                results.append(record)
                counts["SZSE_INDEX_INCOMPLETE"] = counts.get(
                    "SZSE_INDEX_INCOMPLETE", 0) + 1
                continue
            full_reports = [item for item in index.announcements
                            if item.is_full_report and item.document_url]
            record["szse_candidates"] = [
                {"document_id": item.document_id, "title": item.title,
                 "published_on": item.published_on.isoformat() if item.published_on
                 else None, "document_url": item.document_url}
                for item in full_reports
            ]
            if not full_reports:
                record["status"] = "SZSE_REPORT_NOT_FOUND"
                record["reason"] = "no full report in the SZSE window"
                results.append(record)
                counts["SZSE_REPORT_NOT_FOUND"] = counts.get(
                    "SZSE_REPORT_NOT_FOUND", 0) + 1
                continue
            # 窗口里出现多份全文是正常的（年报窗口会同时命中上一年年报、
            # 一季报等）。身份由**内容哈希**判定，不靠窗口唯一性或标题：
            # 逐份下载并与巨潮归档字节比较，找到字节同一的那一份。
            cninfo = _cninfo_bytes(archive, str(row["contentHash"]))
            attempts: list[dict[str, object]] = []
            matched: dict[str, object] | None = None
            for item in full_reports[:MAX_CANDIDATE_DOWNLOADS]:
                try:
                    payload, receipt = client.download(
                        item.document_url,
                        label=f"szse-pdf:{code}:{period}:{item.document_id[:8]}",
                        max_bytes=DOCUMENT_MAX_BYTES)
                except SzseError as exc:
                    attempts.append({
                        "document_id": item.document_id,
                        "status": "DOWNLOAD_FAILED", "reason": str(exc)[:160],
                    })
                    continue
                evidence = cross_source_evidence(
                    document_id=item.document_id, szse_bytes=payload,
                    cninfo_bytes=cninfo)
                attempts.append({
                    "document_id": item.document_id, "title": item.title,
                    "document_url": item.document_url,
                    "receipt_id": receipt.get("receipt_id"),
                    "content_hash": receipt.get("content_hash"),
                    "byte_size": len(payload),
                    "byte_identical": evidence["byte_identical"],
                })
                if evidence["byte_identical"]:
                    matched = {
                        "document_id": item.document_id, "title": item.title,
                        "document_url": item.document_url,
                        "receipt_id": receipt.get("receipt_id"),
                        "content_hash": receipt.get("content_hash"),
                        "byte_size": len(payload),
                    }
                    break
            record["szse_download_attempts"] = attempts
            if matched is not None:
                record["szse_download"] = matched
                record["cross_source"] = cross_source_evidence(
                    document_id=str(matched["document_id"]),
                    szse_bytes=archive.load_bytes(str(matched["content_hash"])),
                    cninfo_bytes=cninfo)
                record["status"] = "BYTE_IDENTICAL_FALLBACK_AVAILABLE"
            else:
                record["status"] = "DIFFERENT_BYTES_REQUIRES_JUDGEMENT"
                record["reason"] = (
                    "no SZSE copy in the window is byte-identical to the archived "
                    "CNINFO original; a differing file must be judged as "
                    "revision/summary/different period rather than preferred by date")
            results.append(record)
            counts[str(record["status"])] = counts.get(str(record["status"]), 0) + 1

    report = {
        "schema": SCHEMA,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "archive_root": str(archive_root),
        "window_days": [WINDOW_BEFORE_DAYS, WINDOW_AFTER_DAYS],
        "counts": dict(sorted(counts.items())),
        "checks": results,
        "boundary": (
            "Identical bytes show the two sites served the same document, so the "
            "SZSE copy is a legitimate byte-level fallback for discovery and archive. "
            "It is NOT an independent numeric verification, and this tool never "
            "switches sources field by field or prefers the later-dated copy."
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
    print(f"{output}: {report['counts']}")
    for item in results:
        print(f"  {item['code']} {item['period_end']}: {item['status']}"
              + (f" ({item.get('reason')})" if item.get("reason") else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
