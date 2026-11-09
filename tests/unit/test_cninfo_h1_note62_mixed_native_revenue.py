from contextlib import nullcontext
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
import copy,hashlib,json,pytest
import aquant.adapters.providers.cninfo_statement_sections as module
import test_cninfo_h1_note62_native_revenue as strict_tests


def _load_private_json(path):
    try:
        path.stat()
    except FileNotFoundError:
        pytest.skip(f"private native-layout fixture unavailable: {path}", allow_module_level=True)
    return json.loads(path.read_text(encoding='utf-8'))


FIX=_load_private_json(Path(__file__).resolve().parents[1]/'fixtures/cninfo_601021_h1_note62/native_mixed_income_p71.json')
PERIOD=date(2026,6,30)


def sample(mutate=None):
 p=copy.deepcopy(FIX['page'])
 if mutate:mutate(p)
 fake=SimpleNamespace(width=p['width'],height=p['height'],extract_words=lambda **kw:p['words'],extract_text=lambda:p['nativeText'],chars=p['chars'])
 blank=SimpleNamespace(width=p['width'],height=p['height'],extract_words=lambda **kw:[],extract_text=lambda:'',chars=[])
 section=module.StatementSection('合并','利润表',71,72,'元','2026年半年度','2025年半年度',(),())
 return SimpleNamespace(pages=[blank]*70+[fake,blank]),section,p


def test_real_mixed_font_own_current_prior_and_original_complete_unit_word():
 pdf,s,p=sample()
 assert module._literal_h1_parenthesized_note_revenue_rows(pdf,s,PERIOD)==()
 rows=module._literal_h1_mixed_font_note_revenue_rows(pdf,s,PERIOD)
 assert len(rows)==1
 row=rows[0]
 assert (row.amount,row.prior_amount)==(Decimal('12336145333'),Decimal('10304467333'))
 assert row.raw_line=='其中：营业收入 七（62） 12,336,145,333 10,304,467,333'
 assert row.current_cell=='12,336,145,333' and row.prior_cell=='10,304,467,333'
 assert row.page==71 and row.unit=='元' and s.rows==() and s.find('revenue') is None
 assert sum(w['text']=='单位：元币种：人民币' for w in p['words'])==1


def test_original_strict_h125_success_returns_same_tuple_and_row(monkeypatch):
 pdf,s,_=strict_tests.sample()
 original=module._literal_h1_parenthesized_note_revenue_rows;seen=[]
 def strict(*args):
  rows=original(*args);seen.append(rows);return rows
 monkeypatch.setattr(module,'_literal_h1_parenthesized_note_revenue_rows',strict)
 result=module._literal_h1_mixed_font_note_revenue_rows(pdf,s,strict_tests.PERIOD)
 assert result is seen[0] and result[0] is seen[0][0]
 assert result[0].amount==Decimal('10304467333')


@pytest.mark.parametrize('year',[2024,2025])
def test_previously_qualified_h124_h125_primary_success_never_enters_mixed_branch(monkeypatch,year):
 period=date(year,6,30);body=b'%PDF-previous-primary'
 row=module.StatementRow('合并','利润表','其中：营业收入 七（62）',Decimal(500),Decimal(400),65,'元','500','400','其中：营业收入 七（62） 500 400')
 s=module.StatementSection('合并','利润表',65,66,'元',f'{year}年半年度',f'{year-1}年半年度',(),())
 monkeypatch.setattr(module,'check_report_cover',lambda *a,**k:None)
 monkeypatch.setattr(module,'extract_statement_sections',lambda *a,**k:module.StatementSections('sha256:'+hashlib.sha256(body).hexdigest(),(s,)))
 monkeypatch.setattr(module,'_primary_no_note_revenue_row',lambda *a,**k:None)
 monkeypatch.setattr(module,'_primary_h1_parenthesized_note_revenue_row',lambda *a,**k:row)
 monkeypatch.setattr(module,'_primary_h1_mixed_font_note_revenue_row',lambda *a,**k:pytest.fail('old qualified primary replaced'))
 kwargs=strict_tests.kwargs();kwargs.update(period_end=period)
 assert module.extract_section_facts(body,**kwargs).by_field['revenue'].value_yuan==500


@pytest.mark.parametrize('change',[
 'header_shift_over_bound','amount_on_other_row','low_vertical_overlap','missing_unit',
 'foreign_currency','incomplete_unit','duplicate_unit','extra_header_narrative',
 'wrong_year','wrong_note','missing_current','third_amount','crossed_char',
 'duplicate_char','parent_title','wrong_period_title','raw_row_changed',
])
def test_mixed_physical_line_never_crosses_rows_identity_units_or_columns(change):
 def mutate(p):
  words=p['words'];detail=next(w for w in words if w['text']=='其中：营业收入')
  header=next(w for w in words if w['text']=='项目' and 270<w['top']<280)
  year=next(w for w in words if w['text']=='2026' and 270<w['top']<280)
  current=next(w for w in words if w['text']=='12,336,145,333' and 300<w['top']<310)
  joined=next(w for w in words if w['text']=='单位：元币种：人民币' and 250<w['top']<270)
  chars=[c for c in p['chars'] if abs(c['top']-current['top'])<.2 and c['x0']>350]
  if change=='header_shift_over_bound':year.update(top=header['top']+2.2,bottom=header['bottom']+2.2)
  if change=='amount_on_other_row':current.update(top=current['top']-14.16,bottom=current['bottom']-14.16)
  if change=='low_vertical_overlap':year.update(top=header['top']+2.095,bottom=header['bottom']+2.095)
  if change=='missing_unit':words.remove(joined)
  if change=='foreign_currency':joined['text']='单位：元币种：美元'
  if change=='incomplete_unit':joined['text']='单位：元币种：'
  if change=='duplicate_unit':words.append(copy.deepcopy(joined))
  if change=='extra_header_narrative':words.append(dict(text='会计政策说明',x0=90,x1=180,top=259,bottom=269))
  if change=='wrong_year':year['text']='2025'
  if change=='wrong_note':next(w for w in words if w['text']=='七（62）' and 300<w['top']<310)['text']='七（63）'
  if change=='missing_current':words.remove(current)
  if change=='third_amount':words.append(dict(text='1,234',x0=320,x1=345,top=current['top'],bottom=current['bottom']))
  if change=='crossed_char':chars[0].update(x0=430,x1=440)
  if change=='duplicate_char':p['chars'].append(copy.deepcopy(chars[0]))
  if change=='parent_title':next(w for w in words if w['text']=='合并利润表')['text']='母公司利润表'
  if change=='wrong_period_title':next(w for w in words if w['text']=='1—6' and 240<w['top']<255)['text']='7—12'
  if change=='raw_row_changed':p['nativeText']=p['nativeText'].replace('其中：营业收入 七（62）','其中：营业收入 七（63）')
 pdf,s,_=sample(mutate)
 assert module._literal_h1_mixed_font_note_revenue_rows(pdf,s,PERIOD)==()


def test_mixed_total_equal_value_is_neither_source_nor_selection_criterion():
 pdf,s,p=sample();expected=module._literal_h1_mixed_font_note_revenue_rows(pdf,s,PERIOD)
 total=next(w for w in p['words'] if w['text']=='一、营业总收入')
 for w in p['words']:
  if 0<=w['top']-total['top']<2.1 and w['x0']>350:w['text']='999,999,999,999'
 assert module._literal_h1_mixed_font_note_revenue_rows(pdf,s,PERIOD)==expected


def test_full_primary_entry_mixed_layout_uses_real_import_and_native_mock(monkeypatch):
 import pdfplumber
 pdf,s,_=sample();body=b'%PDF-mixed-fixture'
 monkeypatch.setattr(pdfplumber,'open',lambda *a,**k:nullcontext(pdf))
 monkeypatch.setattr(module,'check_report_cover',lambda *a,**k:None)
 monkeypatch.setattr(module,'extract_statement_sections',lambda *a,**k:module.StatementSections('sha256:'+hashlib.sha256(body).hexdigest(),(s,)))
 kwargs=strict_tests.kwargs();kwargs.update(period_end=PERIOD,announcement_id='1225524501',document_url='https://static.cninfo.com.cn/finalpage/2026-08-29/1225524501.PDF')
 result=module.extract_section_facts(body,**kwargs)
 assert result.by_field['revenue'].value_yuan==Decimal('12336145333')
 assert result.by_field['revenue'].source_row=='其中：营业收入 七（62） 12,336,145,333 10,304,467,333'
