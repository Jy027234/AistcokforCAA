"""Independent cell selection and reader replay, with no primary amounts."""
from dataclasses import replace
from datetime import date
from decimal import Decimal
import io
import json
import sys
import types

import pytest

from aquant.adapters.providers import cninfo_statement_geometry as grid
from aquant.domain.data.forward_archive import ForwardArchive
from aquant.domain.data.db import connect
from aquant.domain.fundamentals.fact_repository import _replay_grid_cell
from test_machine_verified_fact_repository import _facts, FIRST_SEEN, ANNOUNCEMENT


def _cell():
    return grid.GridStatementCell("revenue", "利润表", date(2026,6,30), 2, 2,
        "合并利润表", "万元", "单位：万元 币种：人民币", "2026年半年度", "2025年半年度",
        "营业收入", "1,004.00", Decimal("10040000"), (0,20,100,100),
        (30,40,60,50), ("项目","2026年半年度","2025年半年度"), 1,
        ((1,"公司代码：000651"),), ((1,"2026年半年度报告"),))


def test_grid_cell_evidence_is_fresh_and_empty_is_not_zero():
    cell = _cell()
    first = cell.as_evidence()
    first["cell_bbox"][0] = 999
    assert cell.as_evidence()["cell_bbox"][0] == 30
    assert grid._amount("") is None
    assert grid._amount("—") is None
    assert grid._amount("0.00") == Decimal(0)
    assert grid._field_label("net_profit_attributable", "归属于母公司所有者的净利润")
    assert not grid._field_label("net_profit_attributable", "少数股东损益")


@pytest.mark.parametrize('report', ['2024年度报告','2024年年度报告','2024年半年度报告'])
def test_front_matter_keeps_exact_year_type_and_code(report):
    pdf=types.SimpleNamespace(pages=[types.SimpleNamespace(extract_text=lambda:f'公司代码：000550\n江铃汽车股份有限公司\n{report}')])
    period=date(2024,6,30) if '半' in report else date(2024,12,31)
    assert grid._frontmatter(pdf,'000550',period)
    assert grid._frontmatter(pdf,'000899',period) is None
    assert grid._frontmatter(pdf,'000550',date(2023,12,31)) is None


@pytest.mark.parametrize("labels, expected", [
    (["项目","2025年半年度","2026年半年度"], 2),
    (["项目","2026年半年度","2026年半年度"], None),
    (["项目","2025年半年度","2024年半年度"], None),
    (["项目","本期发生额","上期发生额"], 1),
])
def test_header_selects_declared_current_column_without_candidate(labels, expected):
    table = types.SimpleNamespace(extract=lambda:[labels], rows=[types.SimpleNamespace(
        cells=[(0,0,20,10),(20,0,40,10),(40,0,60,10)])])
    header = grid._header(table,date(2026,6,30),"利润表","",date(2026,6,30))
    assert (None if header is None else header["column"]) == expected


def _archive_and_fact(tmp_path):
    con = connect(tmp_path / "archive.sqlite")
    archive = ForwardArchive(con, tmp_path / "blobs")
    payload = b"%PDF-original-bytes"
    digest, _ = archive.store_bytes(payload, media_type="application/pdf")
    con.execute("INSERT INTO fetch_receipt (receipt_id,source_id,url,requested_at,responded_at,http_status,outcome,content_hash,byte_size,first_seen_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("pdf", "cninfo", f"https://static.cninfo.com.cn/finalpage/2026-08-25/{ANNOUNCEMENT}.PDF",
         FIRST_SEEN.isoformat(),FIRST_SEEN.isoformat(),200,"OK",digest,len(payload),FIRST_SEEN.isoformat()))
    con.commit()
    raw = con.execute("SELECT stored_path FROM raw_artifact WHERE content_hash=?",(digest,)).fetchone()[0]
    con.close()
    fact = replace(_facts()[-1], value=10040000, content_hash=digest)
    bundle = {"pdf_receipt_id":"pdf", "geometry_archive":{
        "database_path":str(tmp_path/"archive.sqlite"),"root":str(tmp_path/"blobs"),
        "pdf_receipt_id":"pdf","document_url":f"https://static.cninfo.com.cn/finalpage/2026-08-25/{ANNOUNCEMENT}.PDF"}}
    return payload, fact, bundle, tmp_path/"blobs"/raw


def test_consumer_replays_original_hash_and_exact_geometry(tmp_path, monkeypatch):
    payload, fact, bundle, path = _archive_and_fact(tmp_path)
    seen = []
    def extract(raw, *, instrument_id, period_end):
        seen.append((raw,instrument_id,period_end))
        return (_cell(),)
    monkeypatch.setattr(grid,"extract_grid_statement_cells",extract)
    assert _replay_grid_cell(_cell().as_evidence(),fact,bundle)
    assert seen == [(payload,"000651",date(2026,6,30))]
    path.write_bytes(b"%PDF-replaced-bytes")
    assert not _replay_grid_cell(_cell().as_evidence(),fact,bundle)
    assert len(seen) == 1  # changed bytes never reach a cached extractor


@pytest.mark.parametrize("key,value", [
    ("amount_unit","元"),("amount_yuan","1004"),("current_column",2),
    ("period_end","2025-06-30"),("role","母公司"),("cell_bbox",[0,0,0,0]),
    ("issuer_evidence",[[1,"公司代码：600938"]]),("currency_basis","USD"),
])
def test_consumer_rejects_typed_unit_scope_date_column_position(tmp_path,monkeypatch,key,value):
    _,fact,bundle,_ = _archive_and_fact(tmp_path)
    monkeypatch.setattr(grid,"extract_grid_statement_cells",lambda *a,**k:(_cell(),))
    evidence = _cell().as_evidence()
    evidence[key] = value
    assert not _replay_grid_cell(evidence,fact,bundle)


def test_new_path_requires_archive_even_with_true_flag():
    assert not _replay_grid_cell(_cell().as_evidence(),_facts()[-1],None)


def test_cache_binds_actual_bytes_identity_and_returns_immutable_cells(monkeypatch):
    grid._CELL_CACHE.clear()
    seen=[]
    def parse(payload,**kwargs):
        seen.append(payload)
        return (_cell(),)
    monkeypatch.setattr(grid,'_extract_grid_statement_cells',parse)
    first=grid.extract_grid_statement_cells(b'%PDF-a',instrument_id='000651',period_end=date(2026,6,30))
    assert grid.extract_grid_statement_cells(b'%PDF-a',instrument_id='000651',period_end=date(2026,6,30)) is first
    grid.extract_grid_statement_cells(b'%PDF-b',instrument_id='000651',period_end=date(2026,6,30))
    assert seen == [b'%PDF-a',b'%PDF-b']
    assert isinstance(first,tuple)
    grid._CELL_CACHE.clear()
