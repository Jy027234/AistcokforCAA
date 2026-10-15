from dataclasses import FrozenInstanceError
from types import SimpleNamespace

import pdfplumber
import pytest

from aquant.adapters.providers import cninfo_statement_sections as sections
from aquant.adapters.providers.cninfo_financial_candidate import _Heading


def test_cached_heading_geometry_is_bound_to_exact_bytes_and_immutable(monkeypatch):
    payloads = []

    class Document:
        pages = [SimpleNamespace()]

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

    def open_pdf(stream):
        payloads.append(stream.getvalue())
        return Document()

    heading = _Heading("合并", "利润表", 0, 10, 20, "元")
    monkeypatch.setattr(pdfplumber, "open", open_pdf)
    monkeypatch.setattr(sections, "_page_headings", lambda *_: [heading])
    sections._pdf_statement_headings.cache_clear()
    try:
        first = sections._pdf_statement_headings(b"%PDF-exact-first")
        assert sections._pdf_statement_headings(bytes(b"%PDF-exact-first")) is first
        assert sections._pdf_statement_headings(b"%PDF-different") == first
        assert payloads == [b"%PDF-exact-first", b"%PDF-different"]
        assert isinstance(first, tuple)
        with pytest.raises(FrozenInstanceError):
            first[0].top = 99
    finally:
        sections._pdf_statement_headings.cache_clear()
