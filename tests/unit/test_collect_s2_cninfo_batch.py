from __future__ import annotations

import hashlib
import json
import sys
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import collect_s2_cninfo_batch as batch  # noqa: E402


def _config(path: Path, rows: list[dict]) -> Path:
    path.write_text(json.dumps({"instruments": rows}, ensure_ascii=False), encoding="utf-8")
    return path


def _index(stock: str, short_name: str) -> SimpleNamespace:
    announcement_id = f"123456{stock}"
    announcement = SimpleNamespace(
        announcement_id=announcement_id,
        sec_code=stock,
        sec_name=short_name,
        title="2026年半年度报告",
        announcement_date_cn="2026-08-25",
        url=f"https://static.cninfo.com.cn/finalpage/2026-08-25/{announcement_id}.PDF",
    )
    page = SimpleNamespace(
        page_num=1,
        request_body_hash="sha256:" + "a" * 64,
        receipt_id="index-page-" + stock,
        content_hash="sha256:" + "b" * 64,
        raw_announcement_count=1,
        total_announcement=1,
        has_more=False,
    )
    match = SimpleNamespace(
        announcement=announcement,
        page_num=1,
        page_receipt_id=page.receipt_id,
        page_content_hash=page.content_hash,
    )
    return SimpleNamespace(
        organization_id="gssz0000001",
        org_lookup_receipt_id="org-" + stock,
        org_lookup_content_hash="sha256:" + "c" * 64,
        publication_window=(date(2026, 6, 30), date(2028, 6, 29)),
        complete=True,
        termination="has_more_false",
        error=None,
        pages=(page,),
        matches=(match,),
    )


class _StubClient:
    state: dict

    def __init__(self, archive):  # noqa: ANN001
        self.archive = archive
        self.state["instances"] += 1

    def report_index(self, *, stock_code: str, report_period: str,
                     max_pages: int, through: date) -> SimpleNamespace:
        self.state["index_calls"] += 1
        if stock_code in self.state.get("fail_index_for", set()):
            raise OSError("simulated index transport failure")
        assert report_period == "2026-06-30"
        assert max_pages == 20
        return _index(stock_code, self.state["short_names"][stock_code])

    def document(self, url: str, *, label: str) -> SimpleNamespace:
        self.state["downloads"] += 1
        stock = url.rsplit("/", 1)[-1].split(".", 1)[0][-6:]
        payload = b"%PDF-1.4 " + stock.encode("ascii")
        digest, _ = self.archive.store_bytes(payload, media_type="application/pdf")
        now = datetime.now(timezone.utc)
        receipt = self.archive.record(
            source_id="cninfo", url=url, outcome="OK", requested_at=now,
            http_status=200, content_hash=digest, byte_size=len(payload), detail=label,
        )
        return SimpleNamespace(ok=True, payload=payload, content_hash=digest,
                               receipt_id=receipt.receipt_id, detail=None)


def _candidate_extractor(state: dict):
    def extract(pdf_bytes: bytes, *, instrument_id: str, company: str,
                period_end: date, announcement_id: str, document_url: str,
                required_fields: tuple[str, ...]) -> SimpleNamespace:
        state["extract_calls"] += 1
        state["extract_args"].append((instrument_id, company, required_fields))
        digest = "sha256:" + hashlib.sha256(pdf_bytes).hexdigest()
        candidates = tuple(SimpleNamespace(
            field=field, value_yuan=Decimal("123.45"), pdf_page=3,
            column_header="2026年半年度", amount_unit="元", currency="CNY",
            source_cells=(field, "123.45", "100.00"),
            source_current_cell_index=1, source_prior_cell_index=2,
            display_row_label=field, source_row=f"{field}\t123.45\t100.00",
            pdf_sha256=digest,
        ) for field in required_fields)
        return SimpleNamespace(
            pit_eligible=False, candidates=candidates,
            by_field={item.field: item for item in candidates}, pdf_sha256=digest,
        )
    return extract


def _install_stubs(monkeypatch: pytest.MonkeyPatch, state: dict) -> None:
    _StubClient.state = state
    monkeypatch.setattr(batch, "CninfoClient", _StubClient)
    cover_names = {
        "300502": "成都新易盛技术股份有限公司",
        "300394": "天孚通信科技股份有限公司",
    }
    monkeypatch.setattr(
        batch, "_pdf_cover_text",
        lambda payload: cover_names[payload.rsplit(b" ", 1)[-1].decode("ascii")]
        + "\n2026年半年度报告",
    )
    monkeypatch.setattr(batch, "extract_cninfo_candidate_facts", _candidate_extractor(state))


def _rows() -> list[dict]:
    return [
        {"instrument_id": "SZ.300502", "code": "sz300502", "name": "新易盛",
         "industry": "C39计算机、通信和其他电子设备制造业"},
        {"instrument_id": "SZ.300394", "code": "sz300394", "name": "天孚通信",
         "industry": "C39计算机、通信和其他电子设备制造业"},
        {"instrument_id": "SZ.300059", "code": "sz300059", "name": "东方财富",
         "industry": "J67资本市场服务"},
    ]


def test_batch_extracts_reviewable_candidates_from_selected_manufacturing_stock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path / "pool.yaml", _rows())
    state = {"instances": 0, "index_calls": 0, "downloads": 0, "extract_calls": 0,
             "extract_args": [], "short_names": {"300502": "新易盛"}}
    _install_stubs(monkeypatch, state)

    result = batch.collect(
        config_path=config, archive_root=tmp_path / "archive",
        stocks=("300502",), periods=("2026-06-30",),
    )

    row = result["reports"][0]
    assert result["summary"] == {"reports": 1, "extracted": 1, "failed": 0}, row.get("error")
    assert result["pitEligible"] is False and result["formalFactCount"] == 0
    assert row["announcementId"] == "123456300502"
    assert row["announcedOn"] == "2026-08-25"
    assert row["documentUrl"].endswith("/123456300502.PDF")
    assert row["contentHash"].startswith("sha256:")
    assert row["coverCompanyName"] == "成都新易盛技术股份有限公司"
    assert row["fields"][0]["pdfPage"] == 3
    assert row["fields"][0]["amountUnit"] == "元"
    assert row["fields"][0]["sourceCurrentCell"] == "123.45"
    assert row["fields"][0]["reviewStatus"] == "PENDING_HUMAN_VISUAL"
    assert state["extract_args"] == [
        ("300502", "成都新易盛技术股份有限公司", batch.REQUIRED_PILOT_FIELDS_BY_PERIOD["2026-06-30"])
    ]


def test_resume_reuses_only_hash_verified_pdf_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path / "pool.yaml", _rows())
    state = {"instances": 0, "index_calls": 0, "downloads": 0, "extract_calls": 0,
             "extract_args": [], "short_names": {"300502": "新易盛"}}
    _install_stubs(monkeypatch, state)
    first = batch.collect(config_path=config, archive_root=tmp_path / "archive",
                          stocks=("300502",), periods=("2026-06-30",))
    second = batch.collect(config_path=config, archive_root=tmp_path / "archive",
                           stocks=("300502",), periods=("2026-06-30",), previous=first)

    assert second["reports"][0]["contentHash"] == first["reports"][0]["contentHash"]
    assert state["index_calls"] == 2
    assert state["downloads"] == 1
    assert state["extract_calls"] == 1


def test_failure_is_recorded_per_security_period_and_batch_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path / "pool.yaml", _rows())
    state = {"instances": 0, "index_calls": 0, "downloads": 0, "extract_calls": 0,
             "extract_args": [], "short_names": {"300502": "新易盛", "300394": "天孚通信"},
             "fail_index_for": {"300502"}}
    _install_stubs(monkeypatch, state)

    result = batch.collect(config_path=config, archive_root=tmp_path / "archive",
                           stocks=("300502", "300394"), periods=("2026-06-30",))

    assert result["summary"] == {"reports": 2, "extracted": 1, "failed": 1}
    assert "simulated index transport failure" in result["reports"][0]["error"]
    assert result["reports"][1]["status"] == "UNREVIEWED_PDF_CANDIDATE_NOT_PIT_ELIGIBLE"


def test_config_selection_excludes_financial_industry_and_cover_name_is_verified(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path / "pool.yaml", _rows())
    pool = batch._load_industrial_pool(config)
    assert set(pool) == {"300502", "300394"}
    with pytest.raises(ValueError, match="CSRC manufacturing pool"):
        batch.collect(config_path=config, archive_root=tmp_path / "archive",
                      stocks=("300059",), periods=("2026-06-30",))
    assert batch._company_name_from_cover_text(
        "成都新易盛通信技术股份有限公司\n2026年半年度报告", short_name="新易盛",
    ) == "成都新易盛通信技术股份有限公司"
    with pytest.raises(ValueError, match="official PDF cover"):
        batch._company_name_from_cover_text(
            "新易盛技术股份有限公司", short_name="新易盛",
            explicit_name="其他公司股份有限公司",
        )
    with pytest.raises(ValueError, match="stock alias"):
        batch._company_name_from_cover_text(
            "新易盛技术股份有限公司\n无关公司股份有限公司", short_name="新易盛",
            explicit_name="无关公司股份有限公司",
        )
