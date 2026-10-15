"""A blank FX row cannot become zero in either cash identity location."""
from dataclasses import replace
from datetime import date
from decimal import Decimal
import hashlib
from types import SimpleNamespace

import pytest

from aquant.adapters.providers import s2_machine_verification as verifier
from aquant.adapters.providers.cninfo_financial_pdf import CninfoS2CandidateFacts, PdfCandidateFact
from aquant.adapters.providers.cninfo_statement_sections import StatementRow, StatementSection, StatementSections

BODY=b"%PDF-strict-missing-FX-fixture"
DIGEST="sha256:"+hashlib.sha256(BODY).hexdigest()


def row(statement,label,value):
    return StatementRow(role="合并",statement=statement,label=label,amount=Decimal(value),
        prior_amount=Decimal(value),page=10,unit="元",current_cell=value,prior_cell=value,
        raw_line=f"{label} {value} {value}")


def setup(monkeypatch, *, fx=None, grid=None, field="operating_cashflow"):
    cash=[row("现金流量表",label,value) for label,value in (
        ("经营活动产生的现金流量净额","100"),
        ("投资活动产生的现金流量净额","-20"),
        ("筹资活动产生的现金流量净额","10"),
        ("五、现金及现金等价物净增加额","90"))]
    if fx is not None:cash.append(row("现金流量表","四、汇率变动对现金及现金等价物的影响",fx))
    balance=[row("资产负债表",label,value) for label,value in (
        ("所有者权益合计","1000"),("少数股东权益","100"),
        ("归属于母公司所有者权益合计","900"))]
    sections=StatementSections(pdf_sha256=DIGEST,sections=tuple(
        StatementSection(role="合并",statement=name,start_page=10,end_page=11,
            unit="元",current_header="2026年半年度",prior_header="2025年半年度",rows=tuple(rows))
        for name,rows in (("资产负债表",balance),("利润表",[]),("现金流量表",cash))))
    monkeypatch.setattr(verifier,"extract_statement_sections",lambda *a,**k:sections)
    def grid_cells(*a,**k):
        if grid is None:return ()
        return (SimpleNamespace(field="operating_cashflow",amount_yuan=Decimal(grid),
            as_evidence=lambda:{"field":"operating_cashflow","amount_yuan":grid,
                "source":"independent test grid, never a primary candidate"}),)
    monkeypatch.setattr(verifier,"extract_grid_statement_cells",grid_cells)
    value="900" if field=="parent_equity" else "100"
    candidate=PdfCandidateFact(field=field,value_yuan=Decimal(value),period_end=date(2026,6,30),
        report_period_text="2026年半年度",statement="cashflow",pdf_page=10,
        column_header="2026年半年度",amount_unit="元",currency="CNY",
        source_row=f"{field} {value}",source_cells=(field,value),
        source_current_cell_index=1,source_prior_cell_index=1,pdf_sha256=DIGEST,
        display_row_label=field)
    bundle=CninfoS2CandidateFacts(instrument_id="600236",period_end=date(2026,6,30),
        version_label="test",document_url="https://static.cninfo.com.cn/finalpage/2026-07-31/1225449300.PDF",
        pdf_sha256=DIGEST,candidates=(candidate,))
    return verifier.verify_machine_evidence(BODY,candidate=bundle,expected_fields=(field,))


def assert_missing_fx_identity_false(result):
    identity=next(x for x in result.identities if x.path=="cashflow_identity")
    assert identity.holds is False and identity.evidence["fx_effect"] is None
    for field in result.fields:
        for check in field.checks:
            if check.path=="cashflow_identity":
                assert check.holds is False and check.evidence["fx_effect"] is None


def test_missing_fx_without_a_real_independent_grid_remains_unverified(monkeypatch):
    result=setup(monkeypatch)
    assert not result.verified
    assert_missing_fx_identity_false(result)


def test_missing_fx_uses_independent_grid_and_keeps_identity_false(monkeypatch):
    result=setup(monkeypatch,grid="100")
    assert result.verified
    assert result.fields[0].satisfied_paths==("ruled_grid_cell_reextraction",)
    assert_missing_fx_identity_false(result)


def test_explicit_original_zero_fx_is_a_real_complete_component(monkeypatch):
    result=setup(monkeypatch,fx="0.00")
    assert result.verified and result.fields[0].satisfied_paths==("cashflow_identity",)
    identity=next(x for x in result.identities if x.path=="cashflow_identity")
    assert identity.holds and identity.evidence["fx_effect"]["current_cell"]=="0.00"


def test_conflicting_grid_is_rejected_when_fx_is_missing(monkeypatch):
    with pytest.raises(verifier.MachineVerificationError,match="differs from primary"):
        setup(monkeypatch,grid="99")


def test_informational_identity_is_also_false_for_non_cashflow_cohort(monkeypatch):
    result=setup(monkeypatch,field="parent_equity")
    assert result.verified and result.fields[0].satisfied_paths==("equity_identity",)
    assert_missing_fx_identity_false(result)
