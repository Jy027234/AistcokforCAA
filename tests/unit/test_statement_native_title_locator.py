"""Fast title recall preserves canonical proof strings and ambiguity scope."""
from datetime import date
import sys
import types

import pytest

from aquant.adapters.providers import cninfo_statement_geometry as grid


def _reader(monkeypatch, texts):
    seen = []
    pages = []
    for index, text in enumerate(texts):
        def read(index=index, text=text):
            seen.append(index)
            return text
        pages.append(types.SimpleNamespace(extract_text=read))
    monkeypatch.setitem(sys.modules, "pypdf", types.SimpleNamespace(
        PdfReader=lambda _: types.SimpleNamespace(pages=pages)))
    return seen


def _native(monkeypatch, texts, fail_at=None):
    events = []
    class TextPage:
        def __init__(self, index): self.index = index
        def get_text_range(self, *, errors):
            assert errors == "strict"
            events.append(("read", self.index))
            if self.index == fail_at: raise ValueError("native text unavailable")
            return texts[self.index]
        def close(self): events.append(("text-close", self.index))
    class Page:
        def __init__(self, index): self.index = index
        def get_textpage(self): return TextPage(self.index)
        def close(self): events.append(("page-close", self.index))
    class Document:
        def __init__(self, payload): assert payload == b"%PDF-original"
        def __enter__(self): return self
        def __exit__(self, *args): events.append(("doc-close",))
        def __len__(self): return len(texts)
        def __getitem__(self, index): return Page(index)
    monkeypatch.setitem(sys.modules, "pypdfium2", types.SimpleNamespace(PdfDocument=Document))
    return events


def test_native_recalls_all_scopes_continuations_and_uncertain_pages(monkeypatch):
    texts = ["经营情况", "财务附注涉及利 润 表", "母公司现金流量表（续）",
             "合并资产负债表", "", "unknown", "\ue000损坏文本"]
    events = _native(monkeypatch, texts)
    assert grid._native_title_pages(b"%PDF-original") == (7, (1, 2, 3, 4, 5, 6))
    assert [e[1] for e in events if e[0] == "read"] == list(range(7))
    assert sum(e[0] == "text-close" for e in events) == 7
    assert sum(e[0] == "page-close" for e in events) == 7
    assert events[-1] == ("doc-close",)


def test_native_error_closes_resources_and_uses_original_full_scan(monkeypatch):
    events = _native(monkeypatch, ["经营情况", "合并利润表"], fail_at=1)
    seen = _reader(monkeypatch, ["经营情况", " 合 并 利 润 表 "])
    assert grid._canonical_statement_titles(b"%PDF-original") == [
        (1, " 合 并 利 润 表 ", "合并", "利润表")]
    assert seen == [0, 1]
    assert events[-3:] == [("text-close", 1), ("page-close", 1), ("doc-close",)]


def test_native_locator_preserves_original_heading_and_duplicate_detection(monkeypatch):
    _native(monkeypatch, ["概况", "合并利润表", "公司利润表", "合并利润表（续）", "合并利润表"])
    seen = _reader(monkeypatch, ["概况", " 合 并 利 润 表 ", "公司利润表",
                                 "合并利润表（续）", "合并利润表"])
    assert grid._canonical_statement_titles(b"%PDF-original") == [
        (1, " 合 并 利 润 表 ", "合并", "利润表"),
        (2, "公司利润表", "公司", "利润表"),
        (3, "合并利润表（续）", "合并", "利润表"),
        (4, "合并利润表", "合并", "利润表")]
    assert seen == [1, 2, 3, 4]


@pytest.mark.parametrize("native", [None, (3, (1,)), (2, ())])
def test_unavailable_mismatched_or_empty_native_recall_keeps_full_scan(monkeypatch, native):
    monkeypatch.setattr(grid, "_native_title_pages", lambda _: native)
    seen = _reader(monkeypatch, ["概况", "合并现金流量表"])
    assert grid._canonical_statement_titles(b"%PDF-original") == [
        (1, "合并现金流量表", "合并", "现金流量表")]
    assert seen == [0, 1]


def test_no_canonical_title_in_candidates_falls_back_to_whole_document(monkeypatch):
    monkeypatch.setattr(grid, "_native_title_pages", lambda _: (2, (0,)))
    seen = _reader(monkeypatch, ["目录：利润表", "合并利润表"])
    assert grid._canonical_statement_titles(b"%PDF-original") == [(1, "合并利润表", "合并", "利润表")]
    assert seen == [0, 0, 1]


def test_cached_cells_bind_locator_version(monkeypatch):
    grid._CELL_CACHE.clear()
    seen = []
    def extract(payload, **kwargs):
        seen.append(payload)
        return ()
    monkeypatch.setattr(grid, "_extract_grid_statement_cells", extract)
    args = {"instrument_id": "000651", "period_end": date(2026, 6, 30)}
    grid.extract_grid_statement_cells(b"%PDF-original", **args)
    monkeypatch.setattr(grid, "_TITLE_LOCATOR_VERSION", "test-new-version")
    grid.extract_grid_statement_cells(b"%PDF-original", **args)
    assert len(seen) == 2
    grid._CELL_CACHE.clear()
