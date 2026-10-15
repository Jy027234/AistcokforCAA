"""Warm pure parses cannot suppress original archive or relationship checks."""
from copy import deepcopy
from dataclasses import FrozenInstanceError
from datetime import date
import json
import sqlite3
from types import SimpleNamespace

import pypdf
import pytest

from aquant.adapters.providers import s2_revision_impact as impact
from aquant.adapters.providers.cninfo_statement_sections import (
    StatementRow, StatementSection, StatementSections,
)
from test_s2_revision_impact import sources, assess, replace_pdf, ISSUER

REAL_PAGES = impact._pages


@pytest.fixture(autouse=True)
def clean_caches():
    impact._page_parse_cache.clear()
    impact._section_parse_cache.clear()
    yield
    impact._page_parse_cache.clear()
    impact._section_parse_cache.clear()


def install_pages(monkeypatch, transport_json=False):
    calls = []

    def reader(stream, **kwargs):
        body = stream.read()
        calls.append(body)
        pages = json.loads(body[5:])["pages"] if transport_json else [body.decode()]
        return SimpleNamespace(pages=tuple(SimpleNamespace(extract_text=lambda s=text: s) for text in pages))

    monkeypatch.setattr(pypdf, 'PdfReader', reader)
    monkeypatch.setattr(impact, '_pages', REAL_PAGES)
    return calls


def test_fixed_68_sources_stay_warm_without_retaining_unbounded_memory(monkeypatch):
    calls = install_pages(monkeypatch)
    bodies = [b'%PDF-source-' + str(i).encode() for i in range(68)]
    before = [impact._pages(body, code_fingerprint='parser-v1') for body in bodies]
    after = [impact._pages(body, code_fingerprint='parser-v1') for body in bodies]
    assert before == after and len(calls) == 68
    info = impact._page_parse_cache.info()
    assert info['hits'] == 68 and info['entries'] == 68
    assert info['bytes'] <= info['maxBytes']
    assert all(isinstance(pages, tuple) for pages in before)


def test_actual_bytes_and_parser_code_changes_force_a_new_pure_parse(monkeypatch):
    calls = install_pages(monkeypatch)
    original = impact._pages(b'%PDF-source', code_fingerprint='v1')
    assert impact._pages(b'%PDF-source', code_fingerprint='v1') is original
    impact._pages(b'%PDF-changed', code_fingerprint='v1')
    impact._pages(b'%PDF-source', code_fingerprint='v2')
    assert len(calls) == 3


def test_code_fingerprint_uses_dependency_bytes_and_installed_parser_versions(tmp_path, monkeypatch):
    dependency = tmp_path / 'parser.py'
    dependency.write_bytes(b'first actual code')
    monkeypatch.setattr(impact, '_PARSER_CODE_INPUTS', (dependency,))
    monkeypatch.setattr(impact, 'version', lambda package: '1.0')
    first = impact._parser_code_fingerprint()
    dependency.write_bytes(b'changed actual code')
    assert impact._parser_code_fingerprint() != first
    second = impact._parser_code_fingerprint()
    monkeypatch.setattr(impact, 'version', lambda package: '2.0')
    assert impact._parser_code_fingerprint() != second


def test_sections_key_has_exact_period_and_outputs_are_frozen(monkeypatch):
    calls = []

    def extract(body, *, period_end, roles):
        calls.append((body, period_end, roles))
        return StatementSections('sha256:source', ())

    monkeypatch.setattr(impact, 'extract_statement_sections', extract)
    first = impact._sections(b'%PDF-source', '2025-06-30', code_fingerprint='v1')
    assert impact._sections(b'%PDF-source', '2025-06-30', code_fingerprint='v1') is first
    impact._sections(b'%PDF-source', '2024-06-30', code_fingerprint='v1')
    impact._sections(b'%PDF-source', '2025-06-30', code_fingerprint='v2')
    assert len(calls) == 3
    with pytest.raises(FrozenInstanceError):
        first.sections = ()


def test_cells_never_expose_cached_mutable_evidence(monkeypatch):
    row = StatementRow('合并', '利润表', '营业收入', impact.Decimal('20.00'),
                       impact.Decimal('19.00'), 2, '元', '20.00', '19.00', '营业收入 20.00 19.00')
    section = StatementSection('合并', '利润表', 2, 3, '元', '2025年半年度',
                               '2024年半年度', (row,))
    frozen = StatementSections('sha256:source', (section,))
    monkeypatch.setattr(impact, 'extract_statement_sections', lambda *a, **k: frozen)
    first = impact._cells(b'%PDF-source', '2025-06-30', ('revenue',), code_fingerprint='v1')
    first['revenue']['amountYuan'] = '999'
    first['revenue']['tablePages'][0] = 999
    again = impact._cells(b'%PDF-source', '2025-06-30', ('revenue',), code_fingerprint='v1')
    assert again['revenue']['amountYuan'] == '20.00'
    assert again['revenue']['tablePages'] == [2, 3]
    assert impact._section_parse_cache.info()['hits'] == 1


def test_memory_and_entry_bounds_evict_old_parses_and_skip_oversized_one():
    cache = impact._PureParseCache(2, 2048)
    cache.put((b'first',), ('first parse',))
    cache.put((b'second',), ('second parse',))
    assert cache.get((b'first',)) == ('first parse',)
    cache.put((b'third',), ('third parse',))
    assert cache.get((b'second',)) is impact._MISSING
    assert cache.info()['entries'] == 2 and cache.info()['bytes'] <= 2048
    cache.put((b'oversized' * 1024,), ('very large parse',))
    assert cache.info()['entries'] == 2 and cache.info()['bytes'] <= 2048
    cache.clear()
    assert cache.info()['entries'] == 0 and cache.info()['bytes'] == 0


def test_warm_archive_rechecks_all_index_and_pdf_receipts_and_completion(sources, monkeypatch):
    calls = install_pages(monkeypatch, transport_json=True)
    receipts = []
    original = impact._receipt

    def receipt(*args, **kwargs):
        receipts.append(args[2])
        return original(*args, **kwargs)

    monkeypatch.setattr(impact, '_receipt', receipt)
    first = assess(sources).as_dict()
    cold_calls, cold_receipts = len(calls), list(receipts)
    second = assess(sources).as_dict()
    assert len(calls) == cold_calls
    assert receipts[len(cold_receipts):] == cold_receipts
    assert second['completedAt'] >= first['completedAt']
    for report in (first, second):
        report.pop('completedAt')
        report.pop('evidenceHash')
    assert first == second


@pytest.mark.parametrize('mutation', ['pdf_bytes', 'receipt_outcome', 'raw_size', 'org_request', 'request_bytes'])
def test_cached_pdf_parse_cannot_hide_sql_or_raw_tampering(sources, monkeypatch, mutation):
    install_pages(monkeypatch, transport_json=True)
    assess(sources)
    company, docs, root = sources
    doc = docs[0]
    with sqlite3.connect(root / 'meta.sqlite') as con:
        # This isolated fixture simulates externally corrupted archive SQL;
        # ordinary append-only repository updates are already forbidden.
        if mutation in {'receipt_outcome', 'org_request'}:
            con.execute('DROP TRIGGER fetch_receipt_no_update')
        elif mutation == 'raw_size':
            con.execute('DROP TRIGGER raw_artifact_no_update')
        if mutation == 'receipt_outcome':
            con.execute('UPDATE fetch_receipt SET outcome=? WHERE receipt_id=?',
                        ('HTTP_ERROR', doc['receipt']['receipt_id']))
        elif mutation == 'raw_size':
            con.execute('UPDATE raw_artifact SET byte_size=byte_size+1 WHERE content_hash=?',
                        (doc['contentHash'],))
        elif mutation == 'org_request':
            con.execute('UPDATE fetch_receipt SET url=? WHERE receipt_id=?',
                        ('https://www.cninfo.com.cn/new/information/topSearch/query?keyWord=600000&maxNum=10',
                         company['orgLookupReceiptId']))
        else:
            digest = doc['contentHash'] if mutation == 'pdf_bytes' else company['allCategoryPages'][0]['requestBodyHash']
            stored = con.execute('SELECT stored_path FROM raw_artifact WHERE content_hash=?', (digest,)).fetchone()[0]
            path = impact.resolve_archive_member(root, stored)
            path.write_bytes(path.read_bytes() + b'tampered')
    with pytest.raises(impact.RevisionImpactError):
        assess(sources)


def test_warm_pure_parse_does_not_cache_effect_verdict_or_caller_proof(sources, monkeypatch):
    install_pages(monkeypatch, transport_json=True)
    original = assess(sources).as_dict()
    saved = deepcopy(original)
    saved['outcomes'][0]['status'] = 'AFFECTS'
    saved['evidenceHash'] = impact._hash(impact._canonical({k: v for k, v in saved.items() if k != 'evidenceHash'}).encode())
    company, docs, root = sources
    with pytest.raises(impact.RevisionImpactError, match='differs from original'):
        impact.replay_revision_impact(saved, archive_root=root, company=company,
            target_period_end=date(2025, 6, 30), target_metrics=('revenue',), issuer_name=ISSUER,
            documents=docs)
    later = next(d for d in docs if d['periodEnd'] == '2026-06-30')
    replace_pdf(sources, later, lambda v: v['cells']['revenue'].update(priorAmountYuan='21.00'))
    changed = assess(sources).as_dict()
    assert changed['outcomes'][0]['status'] == 'AFFECTS'
    assert original['outcomes'][0]['status'] == 'NO_EFFECT'
