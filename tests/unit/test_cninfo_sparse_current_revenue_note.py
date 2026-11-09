"""Exact native note160 fixture; no PDF, SQLite, provider or OCR in these tests."""
from copy import deepcopy
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
import io
import json

import pytest
from aquant.adapters.providers import cninfo_statement_sections as m


FIXTURE = Path(__file__).parents[1] / "fixtures/cninfo_002517_sparse_revenue_note/native_note160.json"


def _load_private_json(path):
    try:
        path.stat()
    except FileNotFoundError:
        pytest.skip(f"private native-layout fixture unavailable: {path}", allow_module_level=True)
    return json.loads(path.read_text(encoding="utf-8"))


DATA = _load_private_json(FIXTURE)
PERIOD = date(2024, 12, 31)
DIGEST = DATA["originalSourcePdf"]["contentHash"]
KWARGS = DATA["extractWordsKwargs"]


class Page:
    width = DATA["page"]["width"]
    height = DATA["page"]["height"]

    def __init__(self, data=None, text=""):
        self.data = deepcopy(data) if data is not None else None
        self.text = text

    @property
    def chars(self):
        return self.data["characters"] if self.data else []

    def extract_words(self, **kwargs):
        assert kwargs == KWARGS
        return self.data["words"] if self.data else []

    def extract_text(self):
        return self.data["nativeText"] if self.data else self.text


class Pdf:
    def __init__(self, page):
        self.pages = [Page() for _ in range(159)] + [page]
        self.pages[0].text = "七、合并财务报表项目注释"

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def setup(data=None):
    pdf = Pdf(Page(DATA["page"] if data is None else data))
    lines = [(n+1, line) for n, p in enumerate(pdf.pages)
             for line in (p.extract_text() or "").splitlines()]
    index, heading = m._consolidated_revenue_note_headings(lines)[0]
    return pdf, lines, index, heading


def literal(data=None, period=PERIOD):
    pdf, lines, index, heading = setup(data)
    return m._literal_sparse_current_revenue_note(
        pdf, lines, index, heading=heading, pdf_sha256=DIGEST, period_end=period)


def test_original_native_sparse_cells_remain_absent():
    note = literal()
    assert note is not None
    assert note.main_business.amount == Decimal("5118176418.27")
    assert note.other_business.amount == Decimal("16513.77")
    assert note.total.amount == Decimal("5118192932.04")
    assert note.current_revenue_yuan == Decimal("5118192932.04")
    assert note.other_business.prior_amount is None
    assert note.other_business.prior_cell == ""
    assert note.other_business.raw_line == "其他业务 16,513.77"
    assert note.main_business.prior_amount == Decimal("4295390346.58")
    assert note.total.prior_amount == Decimal("4295390346.58")
    evidence = note.as_evidence()
    assert evidence["note_section"]["raw_lines"][2] == "其他业务 16,513.77"
    assert "prior_amount" not in evidence["other_business"]
    assert "0" not in evidence["other_business"]["raw_line"]


def test_existing_consumer_accepts_real_current_only_proof_without_zero_fill():
    from aquant.domain.fundamentals.fact_repository import _machine_path_is_independent
    note = literal()
    evidence = {**note.as_evidence(), "income_statement_page": 97}
    assert _machine_path_is_independent(
        {"path": "revenue_note_decomposition", "evidence": evidence},
        SimpleNamespace(value=Decimal("5118192932.04")))
    assert note.other_business.prior_amount is None


def test_normal_public_entry_uses_local_pdfplumber_and_keeps_original_raw_order(monkeypatch):
    import pdfplumber
    pdf, _, _, _ = setup()
    monkeypatch.setattr(pdfplumber, "open", lambda *args, **kwargs: pdf)
    # This saved fixture only captures the new exact kwargs. The already
    # observed old zero-table path is stubbed, not given invented word cuts.
    monkeypatch.setattr(m, "_literal_amount_before_revenue_note", lambda *a, **k: None)
    result = m.extract_revenue_note_decomposition(
        b"%PDF-fixture", period_end=PERIOD, default_unit="元")
    assert result.other_business.raw_line == "其他业务 16,513.77"
    assert result.other_business.prior_amount is None


def mutate(kind):
    p = deepcopy(DATA["page"])
    words = p["words"]
    one = lambda text: next(w for w in words if w["text"].strip() == text
                            and (text != "单位：元" or 390 < w["top"] < 410))
    if kind == "missing_unit":
        words.remove(one("单位：元"))
    elif kind == "wrong_unit":
        one("单位：元")["text"] = "单位：千元"
    elif kind == "foreign_currency":
        extra = deepcopy(one("单位：元")); extra["text"] = "币种：美元"; words.append(extra)
    elif kind == "wrong_report_year":
        p["nativeText"] = p["nativeText"].replace("2024年年度报告全文", "2023年年度报告全文")
    elif kind == "duplicate_title":
        words.append(deepcopy(one("40、营业收入和营业成本")))
    elif kind == "wrong_period_order":
        one("本期发生额")["text"] = "上期发生额 "
        next(w for w in words if w["text"].strip() == "上期发生额" and w["x0"] > 400)["text"] = "本期发生额 "
    elif kind == "wrong_subheader":
        next(w for w in words if w["text"].strip() == "收入" and w["x0"] > 350)["text"] = "成本 "
    elif kind == "current_is_cost_column":
        w = one("16,513.77"); w["x0"] += 96; w["x1"] += 96
    elif kind == "amount_wrong_row":
        w = one("16,513.77"); w["top"] += 2; w["bottom"] += 2
    elif kind == "extra_other_amount":
        w = deepcopy(one("16,513.77")); w["x0"] += 96; w["x1"] += 96; words.append(w)
    elif kind == "hidden_blank_cell_dash":
        ch = deepcopy(next(c for c in p["characters"] if c["text"] == "1" and abs(c["top"]-461.419)<.01))
        ch["text"] = "-"; ch["x0"] = 290; ch["x1"] = 294; p["characters"].append(ch)
    elif kind == "char_amount_tamper":
        next(c for c in p["characters"] if c["text"] == "1" and abs(c["top"]-461.419)<.01)["text"] = "2"
    elif kind == "raw_quote_tamper":
        p["nativeText"] = p["nativeText"].replace("其他业务 16,513.77", "其他业务 16,513.78")
    elif kind == "duplicate_other":
        words.append(deepcopy(one("其他业务")))
    elif kind == "row_jump":
        w = one("其他业务"); w["top"] += 20; w["bottom"] += 20
    elif kind == "extra_body_text":
        w = deepcopy(one("其他业务")); w["text"] = "另见调整说明"; w["top"] += 3; w["bottom"] += 3; words.append(w)
    elif kind == "sum_mismatch":
        p["nativeText"] = p["nativeText"].replace("16,513.77", "16,513.78")
        one("16,513.77")["text"] = "16,513.78 "
        selected = sorted((c for c in p["characters"] if abs(c["top"]-461.419)<.01 and 198 <= c["x0"] < 243), key=lambda c:c["x0"])
        next(c for c in reversed(selected) if c["text"] == "7")["text"] = "8"
    return p


@pytest.mark.parametrize("kind", [
    "missing_unit", "wrong_unit", "foreign_currency", "wrong_report_year", "duplicate_title",
    "wrong_period_order", "wrong_subheader", "current_is_cost_column", "amount_wrong_row",
    "extra_other_amount", "hidden_blank_cell_dash", "char_amount_tamper", "raw_quote_tamper",
    "duplicate_other", "row_jump", "extra_body_text", "sum_mismatch",
])
def test_sparse_native_rejects_ambiguous_or_incomplete_source(kind):
    assert literal(mutate(kind)) is None


def test_wrong_halfyear_period_rejected():
    assert literal(period=date(2024, 6, 30)) is None


def test_parent_or_missing_scope_rejected():
    pdf, lines, _, _ = setup()
    for scope in ("十八、母公司财务报表项目注释", "报表附注"):
        changed = [(1, scope), *lines[1:]]
        with pytest.raises(m.RevenueNoteNotLocatedError):
            m._select_sparse_current_revenue_note(
                pdf, changed, pdf_sha256=DIGEST, period_end=PERIOD,
                default_unit="元", original_error=m.RevenueNoteNotLocatedError("original"))


def old_lines(second=False):
    lines = [(1, "七、合并财务报表项目注释"),
             (2, "40、营业收入和营业成本"), (2, "单位：元"),
             (2, "项目 本期发生额 上期发生额"), (2, "收入 成本 收入 成本"),
             (2, "主营业务 10.00 8.00 9.00 7.00"),
             (2, "其他业务 2.00 1.00 1.00 1.00"),
             (2, "合计 12.00 9.00 10.00 8.00")]
    if second:
        lines += [(3, "41、营业收入和营业成本"), (3, "单位：元"),
                  (3, "项目 本期发生额 上期发生额"), (3, "收入 成本 收入 成本"),
                  (3, "主营业务 20.00 8.00 9.00 7.00"),
                  (3, "其他业务 2.00 1.00 1.00 1.00"),
                  (3, "合计 22.00 9.00 10.00 8.00")]
    return lines


def test_any_old_valid_table_prevents_new_fallback_even_after_old_ambiguity(monkeypatch):
    pdf, _, _, _ = setup()
    for lines in (old_lines(), old_lines(True)):
        if len(lines) > 8:
            with pytest.raises(m.RevenueNoteNotLocatedError, match="2 张"):
                m._select_revenue_note_decomposition(
                    lines, pdf_sha256=DIGEST, period_end=PERIOD, default_unit="元")
        else:
            assert m._select_revenue_note_decomposition(
                lines, pdf_sha256=DIGEST, period_end=PERIOD, default_unit="元").current_revenue_yuan == 12
        monkeypatch.setattr(m, "_literal_sparse_current_revenue_note", lambda *a, **k: pytest.fail("old table must win"))
        original = m.RevenueNoteNotLocatedError("keep old error")
        with pytest.raises(m.RevenueNoteNotLocatedError) as exc:
            m._select_sparse_current_revenue_note(
                pdf, lines, pdf_sha256=DIGEST, period_end=PERIOD,
                default_unit="元", original_error=original)
        assert exc.value is original


def test_old_complete_selector_return_is_same_object(monkeypatch):
    import pdfplumber
    pdf, _, _, _ = setup()
    sentinel = object()
    monkeypatch.setattr(pdfplumber, "open", lambda *args, **kwargs: pdf)
    monkeypatch.setattr(m, "_select_revenue_note_decomposition", lambda *a, **k: sentinel)
    monkeypatch.setattr(m, "_select_sparse_current_revenue_note", lambda *a, **k: pytest.fail("new branch called"))
    assert m.extract_revenue_note_decomposition(b"%PDF-fixture", period_end=PERIOD) is sentinel


def test_old_amount_before_selector_return_is_same_object(monkeypatch):
    import pdfplumber
    pdf, _, _, _ = setup()
    sentinel = object()
    monkeypatch.setattr(pdfplumber, "open", lambda *args, **kwargs: pdf)
    def fail(*args, **kwargs):
        raise m.RevenueNoteNotLocatedError("old zero tables")
    monkeypatch.setattr(m, "_select_revenue_note_decomposition", fail)
    monkeypatch.setattr(m, "_select_amount_before_revenue_note", lambda *a, **k: sentinel)
    monkeypatch.setattr(m, "_select_sparse_current_revenue_note", lambda *a, **k: pytest.fail("new branch called"))
    assert m.extract_revenue_note_decomposition(b"%PDF-fixture", period_end=PERIOD) is sentinel


def test_old_amount_before_valid_source_blocks_sparse_competition(monkeypatch):
    pdf, lines, _, _ = setup()
    monkeypatch.setattr(m, "_literal_amount_before_revenue_note", lambda *a, **k: object())
    monkeypatch.setattr(m, "_literal_sparse_current_revenue_note", lambda *a, **k: pytest.fail("old literal wins"))
    original = m.RevenueNoteNotLocatedError("old ambiguity")
    with pytest.raises(m.RevenueNoteNotLocatedError) as exc:
        m._select_sparse_current_revenue_note(
            pdf, lines, pdf_sha256=DIGEST, period_end=PERIOD,
            default_unit="元", original_error=original)
    assert exc.value is original


def test_two_sparse_sources_rejected_without_amount_deduplication(monkeypatch):
    pdf, lines, _, heading = setup()
    monkeypatch.setattr(m, "_literal_amount_before_revenue_note", lambda *a, **k: None)
    lines += [(160, heading)]
    with pytest.raises(m.RevenueNoteNotLocatedError, match="多张"):
        m._select_sparse_current_revenue_note(
            pdf, lines, pdf_sha256=DIGEST, period_end=PERIOD,
            default_unit="元", original_error=m.RevenueNoteNotLocatedError("old"))
