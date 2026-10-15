from datetime import date
from types import SimpleNamespace

import pytest

from aquant.adapters.providers.cninfo_statement_sections import check_report_cover
from aquant.adapters.providers.cninfo_financial_pdf import CninfoFinancialPdfError


class _Document:
    def __init__(self, texts):
        self.pages = [SimpleNamespace(extract_text=lambda text=text: text) for text in texts]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


def _check(monkeypatch, texts):
    import pdfplumber
    monkeypatch.setattr(pdfplumber, "open", lambda _: _Document(texts))
    return check_report_cover(b"%PDF-test", company="陕西能源投资股份有限公司",
                              period_end=date(2024, 12, 31))


@pytest.mark.parametrize("texts", [
    ["陕西能源投资股份有限公司 2024年年度报告"],
    ["", "陕西能源投资股份有限公司 2024年年度报告"],
    ["", "", "陕西能源投资股份有限公司2024年年度报告全文 重要提示"],
    ["2024年年度报告", "陕西能源投资股份有限公司 2024年年度报告"],
])
def test_front_matter_identity_after_graphic_title_pages(monkeypatch, texts):
    assert "陕西能源投资股份有限公司" in _check(monkeypatch, texts)


@pytest.mark.parametrize("texts", [
    ["其他股份有限公司2024年年度报告", "陕西能源投资股份有限公司2024年年度报告"],
    ["陕西能源投资股份有限公司2023年年度报告", "陕西能源投资股份有限公司2024年年度报告"],
    ["陕西能源投资股份有限公司", "2024年年度报告"],
    ["", "", "", "陕西能源投资股份有限公司2024年年度报告"],
])
def test_wrong_or_unbounded_identity_is_rejected(monkeypatch, texts):
    with pytest.raises(CninfoFinancialPdfError, match="company or period mismatch"):
        _check(monkeypatch, texts)
