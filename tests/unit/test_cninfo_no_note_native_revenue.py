"""Prepared tests on receipt-bound native fixtures; not yet executed."""
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
import copy,hashlib,json,pytest
import aquant.adapters.providers.cninfo_statement_sections as module
NATIVE_FIXTURE_DIR=Path(__file__).resolve().parents[1]/'fixtures/cninfo_002602_native_revenue_no_note'

def _load_private_json(path):
    try:
        path.stat()
    except FileNotFoundError:
        pytest.skip(f"private native-layout fixture unavailable: {path}", allow_module_level=True)
    return json.loads(path.read_text(encoding='utf-8'))


FIX=_load_private_json(NATIVE_FIXTURE_DIR/'native_primary_p99.json')
PERIOD=date(2025,12,31)

def sample(mutate=None,**changes):
 page=copy.deepcopy(FIX['page']);s=FIX['section']
 section=replace(module.StatementSection('合并','利润表',s['startPage'],s['endPage'],s['unit'],s['currentHeader'],s['priorHeader'],(),()),**changes)
 if mutate:mutate(page)
 fake=SimpleNamespace(width=page['width'],height=page['height'],extract_words=lambda **kw:page['words'],extract_text=lambda:page['text'],chars=page['chars'])
 blank=SimpleNamespace(width=page['width'],height=page['height'],extract_words=lambda **kw:[],extract_text=lambda:'',chars=[])
 return SimpleNamespace(pages=[blank]*98+[fake,blank,blank,blank]),section,page

def test_actual_no_note_own_current_prior_and_native_citation():
 pdf,s,p=sample();rows=module._literal_no_note_revenue_rows(pdf,s,PERIOD);assert len(rows)==1;r=rows[0]
 assert (r.current_cell,r.prior_cell)==('37,898,316,450.40','22,619,694,342.95')
 assert (r.amount,r.prior_amount)==(Decimal('37898316450.40'),Decimal('22619694342.95'))
 assert r.page==99 and r.unit=='元' and r.label=='其中：营业收入'
 assert r.raw_line=='37,898,316,450.40 22,619,694,342.95\n其中：营业收入'
 # The new PRIMARY entry does not install a row into find/rows/revision.
 assert s.rows==() and s.find('revenue') is None

@pytest.mark.parametrize('changes',[{'role':'母公司'},{'statement':'现金流量表'},{'combined':True},{'scope_columns':4},{'unit':'美元'},{'current_header':'2024年度'},{'prior_header':'2023年度'},{'start_page':100}])
def test_scope_unit_period_guards(changes):
 pdf,s,_=sample(**changes);assert module._literal_no_note_revenue_rows(pdf,s,PERIOD)==()

@pytest.mark.parametrize('period',[date(2025,6,30),date(2024,12,31),date(2026,12,31)])
def test_no_general_bare_year_or_half_year_mode(period):
 pdf,s,_=sample();assert module._literal_no_note_revenue_rows(pdf,s,period)==()

@pytest.mark.parametrize('change',['extra_note','extra_year','swapped_year','missing_year','missing_unit','wrong_unit','duplicate_title','parent_title'])
def test_exact_no_note_header_identity_guards(change):
 def mutate(p):
  words=p['words'];item=next(w for w in words if w['text']=='项目' and w['top']>500)
  year=next(w for w in words if w['text']=='2025' and abs(w['top']-item['top'])<.2)
  if change=='extra_note':words.append(dict(text='附注',x0=200,x1=225,top=item['top'],bottom=item['bottom']))
  if change=='extra_year':words.append(dict(text='2023',x0=550,x1=570,top=item['top'],bottom=item['bottom']))
  if change=='swapped_year':year['text']='2024'
  if change=='missing_year':words.remove(year)
  unit=next(w for w in words if w['text']=='单位：元' and w['top']>500)
  if change=='missing_unit':words.remove(unit)
  if change=='wrong_unit':unit['text']='单位：美元'
  title=next(w for w in words if w['text']=='3、合并利润表')
  if change=='duplicate_title':words.append(copy.deepcopy(title))
  if change=='parent_title':title['text']='3、母公司利润表'
 pdf,s,_=sample(mutate);assert module._literal_no_note_revenue_rows(pdf,s,PERIOD)==()

@pytest.mark.parametrize('change',['blank_current','extra_amount','extra_body','cross_divider','duplicate_char','wrong_char','missing_detail','duplicate_detail','raw_citation_changed'])
def test_never_borrow_prior_total_or_ambiguous_native_cells(change):
 def mutate(p):
  words=p['words'];detail=next(w for w in words if w['text']=='其中：营业收入');top=detail['top']-3.96
  amount=next(w for w in words if abs(w['top']-top)<.2 and w['text']=='37,898,316,450.40')
  if change=='blank_current':words.remove(amount)
  if change=='extra_amount':words.append(dict(text='1,000.00',x0=250,x1=285,top=top,bottom=top+9))
  if change=='extra_body':words.append(dict(text='说明',x0=230,x1=245,top=top,bottom=top+9))
  chars=[c for c in p['chars'] if abs(c['top']-top)<.2 and c['x0']>217]
  if change=='cross_divider':chars[0].update(x0=380,x1=390)
  if change=='duplicate_char':p['chars'].append(copy.deepcopy(chars[0]))
  if change=='wrong_char':chars[0]['text']='8'
  if change=='missing_detail':words.remove(detail)
  if change=='duplicate_detail':words.append(copy.deepcopy(detail))
  if change=='raw_citation_changed':p['text']=p['text'].replace('其中：营业收入','其中：营业收入（说明）')
 pdf,s,_=sample(mutate);assert module._literal_no_note_revenue_rows(pdf,s,PERIOD)==()

def test_total_amounts_are_only_structure_and_do_not_deduplicate_or_supply_value():
 pdf,s,p=sample();expected=module._literal_no_note_revenue_rows(pdf,s,PERIOD)
 total=next(w for w in p['words'] if w['text']=='一、营业总收入')
 for w in p['words']:
  if w['x0']>217 and 3.7<=total['top']-w['top']<=4.2:w['text']='999,999,999,999.99'
 assert module._literal_no_note_revenue_rows(pdf,s,PERIOD)==expected


def test_existing_primary_success_never_enters_new_fallback(monkeypatch):
 b=b'%PDF-test';row=module.StatementRow('合并','利润表','营业收入',Decimal(500),Decimal(400),99,'元','500.00','400.00','营业收入 500.00 400.00')
 section=module.StatementSection('合并','利润表',99,102,'元','2025年度','2024年度',(row,),())
 sections=module.StatementSections('sha256:'+hashlib.sha256(b).hexdigest(),(section,))
 monkeypatch.setattr(module,'check_report_cover',lambda *a,**k:None)
 monkeypatch.setattr(module,'extract_statement_sections',lambda *a,**k:sections)
 monkeypatch.setattr(module,'_primary_no_note_revenue_row',lambda *a,**k:pytest.fail('old success replaced'))
 c=module.extract_section_facts(b,instrument_id='002602',company='浙江世纪华通集团股份有限公司',period_end=PERIOD,announcement_id='1225261915',document_url='https://static.cninfo.com.cn/finalpage/2026-04-30/1225261915.PDF',required_fields=('revenue',))
 assert c.by_field['revenue'].value_yuan==500 and c.by_field['revenue'].source_row==row.raw_line

@pytest.mark.parametrize('text',['会计政策说明','以下为报表示例，不代表实际财务数据'])
def test_no_note_title_to_header_context_does_not_accept_narrative_or_template(text):
 def mutate(p):
  p['words'].append(dict(text=text,x0=200,x1=400,top=557,bottom=565))
 pdf,s,_=sample(mutate);assert module._literal_no_note_revenue_rows(pdf,s,PERIOD)==()


@pytest.mark.parametrize('valid_header', [True, False])
def test_actual_primary_entry_import_and_current_only_fixture_path(monkeypatch, valid_header):
 from contextlib import nullcontext
 import pdfplumber
 pdf, section, _ = sample(current_header='2025年度' if valid_header else '2024年度')
 body=b'%PDF-fixture-only'
 sections=module.StatementSections('sha256:'+hashlib.sha256(body).hexdigest(),(section,))
 opened=[]
 def mocked_open(stream):
  opened.append(stream.getvalue())
  return nullcontext(pdf)
 monkeypatch.setattr(pdfplumber,'open',mocked_open)
 monkeypatch.setattr(module,'check_report_cover',lambda *a,**k:None)
 monkeypatch.setattr(module,'extract_statement_sections',lambda *a,**k:sections)
 kwargs=dict(instrument_id='002602',company='浙江世纪华通集团股份有限公司',period_end=PERIOD,announcement_id='1225261915',document_url='https://static.cninfo.com.cn/finalpage/2026-04-30/1225261915.PDF',required_fields=('revenue',))
 if valid_header:
  result=module.extract_section_facts(body,**kwargs)
  assert result.by_field['revenue'].value_yuan==Decimal('37898316450.40')
  assert result.by_field['revenue'].source_row=='37,898,316,450.40 22,619,694,342.95\n其中：营业收入'
 else:
  with pytest.raises(module.CninfoFinancialPdfError,match='not uniquely locatable'):
   module.extract_section_facts(body,**kwargs)
 assert opened==[body]
 assert section.rows==() and section.find('revenue') is None


NOTE_FIX = _load_private_json(NATIVE_FIXTURE_DIR/'native_note_p188.json')


def note_sample(mutate=None, *, role='合并'):
 from contextlib import nullcontext
 page=copy.deepcopy(NOTE_FIX['page'])
 if mutate:mutate(page)
 fake=SimpleNamespace(width=page['width'],height=page['height'],
     extract_words=lambda **kw:page['words'],extract_text=lambda:page['text'],chars=page['chars'])
 blank=SimpleNamespace(width=page['width'],height=page['height'],extract_words=lambda **kw:[],extract_text=lambda:'',chars=[])
 prefix=SimpleNamespace(width=page['width'],height=page['height'],extract_words=lambda **kw:[],
     extract_text=lambda:f'七、{role}财务报表项目注释',chars=[])
 pdf=SimpleNamespace(pages=[prefix]+[blank]*186+[fake])
 lines=[(1,f'七、{role}财务报表项目注释')]+[(188,line) for line in page['text'].splitlines()]
 index,heading=module._consolidated_revenue_note_headings(lines)[0] if role=='合并' else (-1,'')
 return pdf,lines,index,heading,page


def literal_note(mutate=None):
 pdf,lines,index,heading,p=note_sample(mutate)
 return module._literal_amount_before_revenue_note(pdf,lines,index,heading=heading,
       pdf_sha256='sha256:'+hashlib.sha256(b'%PDF-fixture').hexdigest(),period_end=PERIOD)


def test_actual_independent_p188_both_revenue_periods_and_four_raw_columns():
 note=literal_note()
 assert note is not None
 assert note.heading_page==188 and note.role=='合并' and note.unit=='元' and note.unit_source=='note'
 assert (note.current_header,note.prior_header)==('本期发生额','上期发生额')
 assert (note.main_business.amount,note.other_business.amount,note.total.amount)==tuple(map(Decimal,('37673812341.91','224504108.49','37898316450.40')))
 assert (note.main_business.prior_amount,note.other_business.prior_amount,note.total.prior_amount)==tuple(map(Decimal,('22409062019.59','210632323.36','22619694342.95')))
 assert note.current_revenue_yuan==note.total.amount
 assert note.main_business.raw_line=='37,673,812,341.91 11,198,354,385.87 22,409,062,019.59 7,569,871,346.23\n主营业务'
 assert note.other_business.raw_line.endswith('\n其他业务') and note.total.raw_line.endswith('\n合计')
 assert all(row.page==188 and row.statement==module.REVENUE_NOTE_STATEMENT for row in (note.main_business,note.other_business,note.total))


@pytest.mark.parametrize('change',[
 'missing_main','missing_other','missing_total','duplicate_main','missing_unit',
 'wrong_currency','conflicting_unit','swapped_periods','swapped_subcolumns',
 'missing_period','extra_amount','crossed_glyph','duplicate_glyph','missing_glyph',
 'extra_narrative','broken_native_order','wrong_raw_cell','wrong_prior_equation',
])
def test_independent_amount_before_note_rejects_missing_or_ambiguous_original_cells(change):
 def mutate(p):
  words=p['words'];main=next(w for w in words if w['text']=='主营业务')
  other=next(w for w in words if w['text']=='其他业务')
  total=next(w for w in words if w['text']=='合计' and 580<w['top']<610)
  unit=next(w for w in words if w['text']=='单位：元' and 490<w['top']<515)
  current=next(w for w in words if w['text']=='本期发生额')
  prior=next(w for w in words if w['text']=='上期发生额')
  chars=[c for c in p['chars'] if abs(c['top']-(main['top']-3.96))<.2 and c['x0']>158]
  if change=='missing_main':words.remove(main)
  if change=='missing_other':words.remove(other)
  if change=='missing_total':words.remove(total)
  if change=='duplicate_main':words.append(copy.deepcopy(main))
  if change=='missing_unit':words.remove(unit)
  if change=='wrong_currency':unit['text']='单位：美元'
  if change=='conflicting_unit':words.append(dict(text='单位：千元',x0=400,x1=460,top=506,bottom=515))
  if change=='swapped_periods':current['text'],prior['text']=prior['text'],current['text']
  if change=='swapped_subcolumns':
   sub=sorted((w for w in words if abs(w['top']-533.899)<.2),key=lambda w:w['x0'])
   sub[0]['text'],sub[1]['text']=sub[1]['text'],sub[0]['text']
  if change=='missing_period':words.remove(prior)
  if change=='extra_amount':words.append(dict(text='1,000.00',x0=160,x1=170,top=main['top']-3.96,bottom=main['bottom']-3.96))
  if change=='crossed_glyph':chars[0].update(x0=250,x1=265)
  if change=='duplicate_glyph':p['chars'].append(copy.deepcopy(chars[0]))
  if change=='missing_glyph':p['chars'].remove(chars[0])
  if change=='extra_narrative':words.append(dict(text='会计政策说明',x0=200,x1=260,top=540,bottom=549))
  if change=='broken_native_order':p['text']=p['text'].replace('\n其他业务\n','\n说明\n其他业务\n')
  if change=='wrong_raw_cell':p['text']=p['text'].replace('224,504,108.49','224,504,108.48')
  if change=='wrong_prior_equation':
   w=next(w for w in words if w['text']=='210,632,323.36')
   target=[c for c in p['chars'] if abs(c['top']-w['top'])<.2 and w['x0']-.01<=c['x0']<=w['x1']]
   sorted(target,key=lambda c:c['x0'])[-1]['text']='7'
   w['text']='210,632,323.37'
   p['text']=p['text'].replace('210,632,323.36','210,632,323.37')
 assert literal_note(mutate) is None


def test_new_note_selector_cannot_borrow_parent_scope_or_primary_page(monkeypatch):
 pdf,lines,index,heading,_=note_sample(role='母公司')
 with pytest.raises(module.RevenueNoteNotLocatedError):
  module._select_amount_before_revenue_note(pdf,lines,pdf_sha256='sha256:'+'0'*64,
      period_end=PERIOD,default_unit='元',original_error=module.RevenueNoteNotLocatedError('original'))
 pdf,lines,index,heading,_=note_sample()
 pdf.pages[98].extract_words=lambda **kw:pytest.fail('primary p99 words used by independent note')
 note=module._literal_amount_before_revenue_note(pdf,lines,index,heading=heading,pdf_sha256='sha256:'+'0'*64,period_end=PERIOD)
 assert note is not None and note.main_business.page==188


def old_note_lines(page=180, *, multiplier=1):
 d=Decimal(multiplier)
 return [(page,line) for line in [
  '七、合并财务报表项目注释','45、营业收入和营业成本','单位：元',
  '本期发生额 上期发生额','项目','收入 成本 收入 成本',
  f'主营业务 {900*d:,.2f} {400*d:,.2f} {800*d:,.2f} {300*d:,.2f}',
  f'其他业务 {100*d:,.2f} {50*d:,.2f} {90*d:,.2f} {40*d:,.2f}',
  f'合计 {1000*d:,.2f} {450*d:,.2f} {890*d:,.2f} {340*d:,.2f}']]


def test_complete_old_selector_success_precedes_new_note_even_with_old_failed_heading(monkeypatch):
 from contextlib import nullcontext
 import pdfplumber
 pdf,lines,index,heading,_=note_sample()
 extra=old_note_lines()
 pdf.pages[179]=SimpleNamespace(extract_text=lambda:'\n'.join(line for _,line in extra))
 full=[(n+1,line) for n,page in enumerate(pdf.pages) for line in page.extract_text().splitlines()]
 expected=module._select_revenue_note_decomposition(full,pdf_sha256='sha256:'+hashlib.sha256(b'%PDF-fixture').hexdigest(),period_end=PERIOD,default_unit='元')
 monkeypatch.setattr(pdfplumber,'open',lambda *a,**k:nullcontext(pdf))
 monkeypatch.setattr(module,'_select_amount_before_revenue_note',lambda *a,**k:pytest.fail('old success replaced'))
 actual=module.extract_revenue_note_decomposition(b'%PDF-fixture',period_end=PERIOD,default_unit='元')
 assert actual.as_evidence()==expected.as_evidence() and actual.main_business.amount==1000-100


def test_old_true_multi_table_ambiguity_is_not_rescued_by_new_note(monkeypatch):
 from contextlib import nullcontext
 import pdfplumber
 pdf,lines,index,heading,_=note_sample()
 for page,mul in [(179,1),(180,2)]:
  data=old_note_lines(page+1,multiplier=mul)
  pdf.pages[page]=SimpleNamespace(extract_text=lambda data=data:'\n'.join(line for _,line in data))
 monkeypatch.setattr(pdfplumber,'open',lambda *a,**k:nullcontext(pdf))
 monkeypatch.setattr(module,'_literal_amount_before_revenue_note',lambda *a,**k:pytest.fail('old ambiguity replaced'))
 with pytest.raises(module.RevenueNoteNotLocatedError,match='2 张数值不同'):
  module.extract_revenue_note_decomposition(b'%PDF-fixture',period_end=PERIOD,default_unit='元')


def test_actual_note_full_entry_from_mocked_native_pdf_only(monkeypatch):
 from contextlib import nullcontext
 import pdfplumber
 pdf,_,_,_,_=note_sample()
 monkeypatch.setattr(pdfplumber,'open',lambda *a,**k:nullcontext(pdf))
 result=module.extract_revenue_note_decomposition(b'%PDF-fixture',period_end=PERIOD,default_unit='元')
 assert result.current_revenue_yuan==Decimal('37898316450.40')
 assert result.total.prior_amount==Decimal('22619694342.95')
