"""CAPCO supplement claims stay bound to original source receipts and rows."""
from copy import deepcopy
from datetime import datetime
from pathlib import Path
import sqlite3

import pytest

from aquant.domain.data.forward_archive import ForwardArchive


@pytest.fixture
def capco(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "tools"))
    import verify_research_industry_supplement as verify
    import pypdf
    monkeypatch.setattr(pypdf, "PdfReader", lambda *a: type("PDF", (), {"pages": [None, None]})())
    raw = [{"code_raw": "920001", "page": 2, "company_name_raw": "测试公司",
            "category_code": "C", "category_name": "制造业", "subcategory_code": "CG", "subcategory_name": "设备",
            "major_code": "36", "major_name": "汽车制造业", "classification_code_tail_raw": "C制造业CG设备36汽车制造业",
            "company_and_classification_raw": "测试公司 C 制造业 CG 设备 36 汽车制造业", "parse_status": "parsed"},
           {"code_raw": "000001", "page": 1, "company_name_raw": "银行", "category_code": "J", "category_name": "金融业",
            "subcategory_code": None, "subcategory_name": None, "major_code": "66", "major_name": "货币金融服务",
            "classification_code_tail_raw": "J金融业66货币金融服务", "company_and_classification_raw": "银行 J 金融业 66 货币金融服务", "parse_status": "parsed"}]
    monkeypatch.setattr(verify, "extract_rows", lambda body: (deepcopy(raw), ["2026年上半年上市公司行业分类结果"]))
    universe = {"920001": "org1", "831001": "org1", "920002": "org2"}
    quotes = {"rows": [{"official_code": code, "close_time_eligible": True, "volume_raw": "1", "turnover_amount_raw": "1"}
                        for code in ("920001", "920002")]}
    industry = {"rows": []}
    pdf_url = "https://sp.capco.org.cn:82/file/202609/gwfile/2026shangbangupiaodaima.pdf"
    article = f'<title>2026年上半年上市公司行业分类结果</title><p>发布时间：2026-09-30</p><a href="{pdf_url}">按股票代码排序</a>'
    guideline = f'<p>发布时间：2023-05-21</p><h1>{verify.TITLE}</h1><p>《国民经济行业分类》（GB T4754-2017）</p>'
    bodies = {"result_article": article.encode(), "classification_pdf": b"%PDF-archived-fixture",
              "guideline_article": guideline.encode()}
    con = sqlite3.connect(tmp_path / "meta.sqlite")
    archive = ForwardArchive(con, tmp_path)
    sources = {}
    for index, (role, body) in enumerate(bodies.items()):
        url = {"result_article": verify.ARTICLE_URL, "classification_pdf": pdf_url, "guideline_article": verify.GUIDE_URL}[role]
        content_hash, _ = archive.store_bytes(body)
        requested, responded = f"2026-10-09T12:0{index}:00+00:00", f"2026-10-09T12:0{index}:01+00:00"
        receipt = archive.record(source_id=verify.SOURCE_ID, url=url, outcome="OK", http_status=200,
                                 requested_at=datetime.fromisoformat(requested), responded_at=datetime.fromisoformat(responded),
                                 content_hash=content_hash, byte_size=len(body))
        sources[role] = {"receipt_id": receipt.receipt_id, "url": url, "requested_at": requested, "responded_at": responded,
                         "http_status": 200, "content_hash": content_hash, "byte_size": len(body)}
    con.close()
    sources["classification_pdf"].update(origin_url=pdf_url, page_count=2, raw_extracted_code_row_count=2, unique_pdf_category_pairs=2)
    sources["guideline_article"].update(title=verify.TITLE, publication_date="2023-05-21")
    packet = {"schema": "aquant.capco_research_industry_supplement.v1", "captured_from": sources["result_article"]["requested_at"],
              "captured_to": sources["guideline_article"]["responded_at"], "generated_at": "2026-10-09T12:03:00+00:00", "sources": sources,
              "taxonomy": {"publisher": "中国上市公司协会", "taxonomy_title": verify.TITLE, "taxonomy_publication_date": "2023-05-21",
                  "result_title": verify.RESULT_TITLE, "reference_period": "2026年上半年", "result_publication_date": "2026-09-30",
                  "classification_asof_date": None, "guideline_text_excerpt": f"{verify.TITLE} 《国民经济行业分类》（GB T4754-2017）",
                  "category_pairs_in_pdf": [{"code": "C", "label": "制造业"}, {"code": "J", "label": "金融业"}],
                  "financial_category": {"code": "J", "label": "金融业"}},
              "rows": [{"official_code": "920001", "org_id": "org1", "same_org_official_codes_annotation_only": ["831001"],
                        "company_name_raw": "测试公司", "industry_category_code": "C", "industry_category_label": "制造业",
                        "industry_subcategory_code": "CG", "industry_subcategory_label": "设备", "industry_major_code": "36", "industry_major_label": "汽车制造业",
                        "classification_code_tail_raw": raw[0]["classification_code_tail_raw"], "source_row_raw": raw[0]["company_and_classification_raw"],
                        "source_page": 2, "parse_status": "parsed"}],
              "scope": {"official_universe_code_count": 3, "target_count": 2, "target_category_counts": {"C 制造业": 1},
                        "matched_target_code_count": 1, "missing_target_codes": ["920002"], "duplicate_target_source_codes": [],
                        "conflicting_target_source_codes": [], "parse_status_counts": {"parsed": 1}},
              "unmatched_target_codes": ["920002"], "duplicate_source_rows": {}, "conflicting_source_rows": {}}
    return verify, packet, universe, tmp_path, quotes, industry, raw


def admit(capco, packet=None):
    verify, original, universe, root, quotes, industry, _ = capco
    verify.verify_industry_supplement(packet or original, universe, root, target_quotes=quotes, target_industry=industry)


def test_capco_own_taxonomy_exact_identity_and_explicit_missing_are_admitted(capco):
    admit(capco)


@pytest.mark.parametrize("fault", ["org", "alias", "page", "category", "major", "excerpt", "missing", "target_count", "matched_count",
    "categories", "parse_count", "duplicates", "conflicts", "financial", "effective_date", "taxonomy_date", "reference_period",
    "guideline_excerpt", "source_time", "source_url", "source_hash", "byte_count", "capture_start", "capture_end", "pdf_count", "original_bytes"])
def test_capco_rejects_forged_source_identity_taxonomy_rows_and_coverage(capco, fault):
    _, original, _, root, _, _, _ = capco
    packet = deepcopy(original)
    row, taxonomy, sources = packet["rows"][0], packet["taxonomy"], packet["sources"]
    if fault in ("org", "alias", "page", "category", "major", "excerpt"):
        field, value = {"org": ("org_id", "wrong"), "alias": ("official_code", "831001"), "page": ("source_page", 1),
                        "category": ("industry_category_code", "J"), "major": ("industry_major_code", "66"), "excerpt": ("source_row_raw", "wrong")}[fault]
        row[field] = value
    elif fault == "missing":
        packet["unmatched_target_codes"] = []
    elif fault in ("target_count", "matched_count", "categories", "parse_count"):
        field, value = {"target_count": ("target_count", 1), "matched_count": ("matched_target_code_count", 2),
                        "categories": ("target_category_counts", {"J 金融业": 1}), "parse_count": ("parse_status_counts", {"parsed": 2})}[fault]
        packet["scope"][field] = value
    elif fault in ("duplicates", "conflicts"):
        packet["duplicate_source_rows" if fault == "duplicates" else "conflicting_source_rows"] = {"920001": []}
    elif fault == "financial":
        taxonomy["financial_category"]["code"] = "C"
    elif fault in ("effective_date", "taxonomy_date", "reference_period", "guideline_excerpt"):
        taxonomy[{"effective_date": "classification_asof_date", "taxonomy_date": "taxonomy_publication_date", "reference_period": "reference_period",
                  "guideline_excerpt": "guideline_text_excerpt"}[fault]] = "forged"
    elif fault in ("source_time", "source_url", "source_hash", "byte_count"):
        sources["classification_pdf"][{"source_time": "responded_at", "source_url": "url", "source_hash": "content_hash", "byte_count": "byte_size"}[fault]] = "forged"
    elif fault in ("capture_start", "capture_end"):
        packet["captured_from" if fault == "capture_start" else "captured_to"] = "2026-10-10T00:00:00+00:00"
    elif fault == "pdf_count":
        sources["classification_pdf"]["raw_extracted_code_row_count"] = 3
    else:
        bare = sources["classification_pdf"]["content_hash"][7:]
        (root / "raw" / bare[:2] / bare).write_bytes(b"%PDF-modified")
    with pytest.raises(ValueError):
        admit(capco, packet)


def test_target_admission_uses_explicit_target_packets_and_never_substitutes_same_org_alias(capco):
    _, _, _, _, quotes, _, _ = capco
    quotes["rows"][0]["official_code"] = "831001"
    with pytest.raises(ValueError, match="target rows"):
        admit(capco)


def test_duplicate_pdf_rows_cannot_be_silently_deduplicated(capco):
    *_, raw = capco
    raw.append(deepcopy(raw[0]))
    with pytest.raises(ValueError):
        admit(capco)


@pytest.mark.parametrize("fault", ["source_id", "outcome", "http_status"])
def test_source_receipt_must_be_successful_capco_http_receipt(capco, fault):
    _, packet, _, root, _, _, _ = capco
    con = sqlite3.connect(root / "meta.sqlite")
    con.execute("DROP TRIGGER fetch_receipt_no_update")
    value = {"source_id": "other-provider", "outcome": "HTTP_ERROR", "http_status": 500}[fault]
    con.execute(f"UPDATE fetch_receipt SET {fault}=? WHERE receipt_id=?", (value, packet["sources"]["classification_pdf"]["receipt_id"]))
    con.commit()
    con.close()
    with pytest.raises(ValueError, match="outcome/source/HTTP"):
        admit(capco)
