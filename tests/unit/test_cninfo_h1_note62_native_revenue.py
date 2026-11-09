"""Saved native H1 words/chars, plus clearly mocked PDF containers only."""
from contextlib import nullcontext
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
import copy
import hashlib
import json
import pytest
import aquant.adapters.providers.cninfo_statement_sections as module

FIXTURE_DIR=Path(__file__).resolve().parents[1]/'fixtures/cninfo_601021_h1_note62'

def _load_private_json(path):
    try:
        path.stat()
    except FileNotFoundError:
        pytest.skip(f"private native-layout fixture unavailable: {path}", allow_module_level=True)
    return json.loads(path.read_text(encoding='utf-8'))


PRIMARY=_load_private_json(FIXTURE_DIR/'native_income_p65_p66.json')
NOTE=_load_private_json(FIXTURE_DIR/'native_note_p134_p135.json')
PERIOD=date(2025,6,30)


def sample(mutate=None, **changes):
 page=copy.deepcopy(PRIMARY['pages'][0])
 if mutate:mutate(page)
 fake=SimpleNamespace(width=page['width'],height=page['height'],
     extract_words=lambda **kw:page['words'],extract_text=lambda:page['nativeText'],chars=page['chars'])
 blank=SimpleNamespace(width=page['width'],height=page['height'],
     extract_words=lambda **kw:[],extract_text=lambda:'',chars=[])
 pdf=SimpleNamespace(pages=[blank]*64+[fake]+[blank]*113)
 section=replace(module.StatementSection('合并','利润表',65,66,'元','2025年半年度','2024年半年度',(),()),**changes)
 return pdf,section,page


def test_original_h1_detail_cells_literal_note_and_current_prior_native_citation():
 pdf,section,_=sample()
 rows=module._literal_h1_parenthesized_note_revenue_rows(pdf,section,PERIOD)
 assert len(rows)==1
 row=rows[0]
 assert (row.amount,row.prior_amount)==(Decimal('10304467333'),Decimal('9875103007'))
 assert row.raw_line=='其中：营业收入 七（62） 10,304,467,333 9,875,103,007'
 assert row.label=='其中：营业收入 七（62）' and row.page==65 and row.unit=='元'
 assert section.rows==() and section.find('revenue') is None


@pytest.mark.parametrize('changes',[
 {'role':'母公司'},{'statement':'现金流量表'},{'combined':True},{'scope_columns':4},
 {'unit':'美元'},{'current_header':'2024年半年度'},{'prior_header':'2023年半年度'},
 {'start_page':66},
])
def test_only_completed_consolidated_h1_current_and_prior_scope(changes):
 pdf,section,_=sample(**changes)
 assert module._literal_h1_parenthesized_note_revenue_rows(pdf,section,PERIOD)==()


@pytest.mark.parametrize('period',[date(2025,12,31),date(2025,3,31),date(2024,6,30)])
def test_other_period_does_not_borrow_h1_header(period):
 pdf,section,_=sample()
 assert module._literal_h1_parenthesized_note_revenue_rows(pdf,section,period)==()


@pytest.mark.parametrize('change',[
 'wrong_note','missing_closing','wrong_note_column','missing_current','extra_amount',
 'crossed_glyph','duplicate_glyph','missing_glyph','wrong_raw_citation',
 'duplicate_detail','parent_title','wrong_date','wrong_currency','missing_currency',
 'missing_unit','extra_header_text','wrong_current_year','swapped_header_order',
])
def test_literal_original_identity_columns_and_cell_guards(change):
 def mutate(page):
  words=page['words'];detail=next(w for w in words if w['text']=='其中：营业收入')
  note=next(w for w in words if w['text']=='七（62）' and abs(w['top']-detail['top'])<.2)
  cells=sorted((w for w in words if abs(w['top']-detail['top'])<.2 and w['x0']>340),key=lambda w:w['x0'])
  chars=[c for c in page['chars'] if abs(c['top']-detail['top'])<.2 and c['x0']>340]
  title=next(w for w in words if w['text']=='合并利润表')
  currency=next(w for w in words if w['text']=='币种：人民币' and 170<w['top']<195)
  if change=='wrong_note':note['text']='七（63）'
  if change=='missing_closing':note['text']='七（62'
  if change=='wrong_note_column':note.update(x0=360,x1=400)
  if change=='missing_current':words.remove(cells[0])
  if change=='extra_amount':words.append(dict(text='1,234',x0=320,x1=339,top=detail['top'],bottom=detail['bottom']))
  if change=='crossed_glyph':chars[0].update(x0=430,x1=440)
  if change=='duplicate_glyph':page['chars'].append(copy.deepcopy(chars[0]))
  if change=='missing_glyph':page['chars'].remove(chars[0])
  if change=='wrong_raw_citation':page['nativeText']=page['nativeText'].replace('其中：营业收入 七（62）','其中：营业收入 七（63）')
  if change=='duplicate_detail':words.append(copy.deepcopy(detail))
  if change=='parent_title':title['text']='母公司利润表'
  if change=='wrong_date':next(w for w in words if w['text']=='1—6' and 160<w['top']<180)['text']='7—12'
  if change=='wrong_currency':currency['text']='币种：美元'
  if change=='missing_currency':words.remove(currency)
  if change=='missing_unit':words.remove(next(w for w in words if w['text']=='单位：元' and 170<w['top']<195))
  if change=='extra_header_text':words.append(dict(text='会计政策说明',x0=100,x1=180,top=180,bottom=190))
  if change in ('wrong_current_year','swapped_header_order'):
   year=next(w for w in words if w['text']=='2025' and 190<w['top']<205)
   year['text']='2024'
 pdf,section,_=sample(mutate)
 assert module._literal_h1_parenthesized_note_revenue_rows(pdf,section,PERIOD)==()


def test_total_amount_is_not_a_selection_key_or_primary_value():
 pdf,section,page=sample()
 expected=module._literal_h1_parenthesized_note_revenue_rows(pdf,section,PERIOD)
 total=next(w for w in page['words'] if w['text']=='一、营业总收入')
 for word in page['words']:
  if abs(word['top']-total['top'])<.2 and word['x0']>340:
   word['text']='999,999,999,999'
 assert module._literal_h1_parenthesized_note_revenue_rows(pdf,section,PERIOD)==expected


def kwargs():
 return dict(instrument_id='601021',company='春秋航空股份有限公司',period_end=PERIOD,
   announcement_id='1224612122',document_url='https://static.cninfo.com.cn/finalpage/2025-08-29/1224612122.PDF',required_fields=('revenue',))


def test_normal_primary_entry_uses_only_new_current_fallback_with_mock_native_pdf(monkeypatch):
 import pdfplumber
 pdf,section,_=sample();body=b'%PDF-native-fixture'
 monkeypatch.setattr(pdfplumber,'open',lambda *a,**k:nullcontext(pdf))
 monkeypatch.setattr(module,'check_report_cover',lambda *a,**k:None)
 monkeypatch.setattr(module,'extract_statement_sections',lambda *a,**k:module.StatementSections('sha256:'+hashlib.sha256(body).hexdigest(),(section,)))
 result=module.extract_section_facts(body,**kwargs())
 assert result.by_field['revenue'].value_yuan==Decimal('10304467333')
 assert result.by_field['revenue'].source_row=='其中：营业收入 七（62） 10,304,467,333 9,875,103,007'
 assert section.rows==() and section.find('revenue') is None


@pytest.mark.parametrize('prior_success',['direct','previous_annual_fallback'])
def test_all_preexisting_primary_success_paths_remain_first(monkeypatch,prior_success):
 body=b'%PDF-existing';pdf,section,_=sample()
 row=module.StatementRow('合并','利润表','营业收入',Decimal(500),Decimal(400),65,'元','500','400','营业收入 500 400')
 if prior_success=='direct':section=replace(section,rows=(row,))
 else:monkeypatch.setattr(module,'_primary_no_note_revenue_row',lambda *a,**k:row)
 monkeypatch.setattr(module,'check_report_cover',lambda *a,**k:None)
 monkeypatch.setattr(module,'extract_statement_sections',lambda *a,**k:module.StatementSections('sha256:'+hashlib.sha256(body).hexdigest(),(section,)))
 monkeypatch.setattr(module,'_primary_h1_parenthesized_note_revenue_row',lambda *a,**k:pytest.fail('old success replaced'))
 assert module.extract_section_facts(body,**kwargs()).by_field['revenue'].value_yuan==500


def test_original_independent_note_parser_already_reads_actual_p134_table(monkeypatch):
 import pdfplumber
 pages={p['physicalPage']:p for p in NOTE['pages']}
 empty=SimpleNamespace(extract_text=lambda:'')
 fake=[empty]*178
 # The scope prefix is an explicit mock: real whole-PDF scope is deferred to
 # the later normal source gate, not fabricated as captured page evidence.
 fake[0]=SimpleNamespace(extract_text=lambda:'七、合并财务报表项目注释')
 for number,p in pages.items():fake[number-1]=SimpleNamespace(extract_text=lambda p=p:p['nativeText'])
 monkeypatch.setattr(pdfplumber,'open',lambda *a,**k:nullcontext(SimpleNamespace(pages=fake)))
 monkeypatch.setattr(module,'_select_amount_before_revenue_note',lambda *a,**k:pytest.fail('original note success replaced'))
 result=module.extract_revenue_note_decomposition(b'%PDF-native-note-fixture',period_end=PERIOD,default_unit='元')
 assert result.current_revenue_yuan==Decimal('10304467333')
 assert result.main_business.page==result.other_business.page==result.total.page==134
 assert (result.main_business.amount,result.other_business.amount)==(Decimal('10058776115'),Decimal('245691218'))
 assert (result.main_business.prior_amount,result.other_business.prior_amount,result.total.prior_amount)==tuple(map(Decimal,('9672536514','202566493','9875103007')))
 assert result.main_business.raw_line=='主营业务 10,058,776,115 8,757,039,437 9,672,536,514 8,302,460,223'


def test_actual_minority_dashes_remain_missing_not_zero():
 page=PRIMARY['pages'][1]
 lines=[]
 for line in page['nativeText'].splitlines():
  if line=='母公司利润表':break
  lines.append((66,line))
 section=module.StatementSection('合并','利润表',65,66,'元','2025年半年度','2024年半年度',(),tuple(lines))
 assert section.component('minority_profit') is None
