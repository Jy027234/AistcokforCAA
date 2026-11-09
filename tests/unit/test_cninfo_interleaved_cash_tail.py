from dataclasses import replace,asdict
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
import copy,json,pytest
import aquant.adapters.providers.cninfo_statement_sections as m
from aquant.adapters.providers.s2_machine_verification import _check_identity,_section_evidence


def _load_private_json(path):
 try:
  path.stat()
 except FileNotFoundError:
  pytest.skip(f"private native-layout fixture unavailable: {path}",allow_module_level=True)
 return json.loads(path.read_text(encoding='utf-8'))


FIX=_load_private_json(Path(__file__).resolve().parents[1]/'fixtures/cninfo_002946_interleaved_cash/native_p103_p104_with_cover_chain.json')
PERIOD=date(2025,12,31)

def row(label,value,prior='0',page=104):
 return m.StatementRow('合并','现金流量表',label,Decimal(value),Decimal(prior),page,'元',value,prior,f'{label} {value} {prior}')

def sample(change=None):
 pages=copy.deepcopy(FIX['pages'])
 if change:change(pages)
 class FakePage:
  def __init__(self,p):self.p=p;self.width=p['width'];self.height=p['height'];self.chars=p['chars']
  def extract_words(self,**kw):
   assert kw==dict(x_tolerance=2,y_tolerance=3,keep_blank_chars=True,use_text_flow=False,extra_attrs=['fontname','size'])
   return self.p['words']
  def extract_text(self):return self.p['nativeText']
  def crop(self,bbox):return self
 blank=SimpleNamespace(width=595,height=842,extract_text=lambda:FIX['coverNativeTextFixture'])
 pdf=SimpleNamespace(pages=[blank]*102+[FakePage(p) for p in pages])
 title=next(w for w in pages[0]['words'] if w['text']=='、合并现金流量表')
 mother=next(w for w in pages[1]['words'] if w['text']=='、母公司现金流量表')
 start=SimpleNamespace(page_index=102,top=title['top'],bottom=title['bottom'],role='合并',statement='现金流量表')
 end=SimpleNamespace(page_index=103,top=mother['top'],bottom=mother['bottom'],role='母公司',statement='现金流量表')
 rows=(row('经营活动产生的现金流量净额','1510107598.95','1491248509.90'),row('投资活动产生的现金流量净额','-485265288.85'),row('筹资活动产生的现金流量净额','-1090172874.95'))
 s=m.StatementSection('合并','现金流量表',103,104,'元','2025年度','2024年度',rows,tuple((104,line) for line in pages[1]['nativeText'].splitlines()))
 return pdf,start,end,s,pages

def recover(args):return m._literal_interleaved_annual_cash_tail_rows(*args[:4],PERIOD)

def test_actual_native_split_owns_both_real_columns_keeps_original_citation():
 args=sample();s=args[3];assert s.component('fx_effect') is None and s.component('net_increase_cash') is None
 rows=recover(args);assert len(rows)==2
 assert[(r.label,str(r.amount),str(r.prior_amount)) for r in rows]==[
  ('四、汇率变动对现金及现金等价物的影响','-393565.01','207256.80'),
  ('五、现金及现金等价物净增加额','-65724129.86','-47257367.57')]
 assert rows[0].raw_line=='四、汇率变动对现金及现金等价物的 -393,565.01 207,256.80\n影五响、 现金及现金等价物净增加额 -65,724,129.86 -47,257,367.57'
 assert rows[1].raw_line==rows[0].raw_line.splitlines()[1]
 ready=replace(s,literal_cash_tail_rows=rows)
 assert ready.rows is s.rows and ready.lines is s.lines and ready.find('operating_cashflow') is s.find('operating_cashflow')
 assert ready.component('fx_effect') is rows[0] and ready.component('net_increase_cash') is rows[1]
 terms=[(ready.component('net_increase_cash').amount,1),(ready.component('investing_cashflow').amount,-1),(ready.component('financing_cashflow').amount,-1),(ready.component('fx_effect').amount,-1)]
 assert _check_identity('cashflow_identity',terms,expected=Decimal('1510107598.95'),evidence={}).holds
 assert _section_evidence(m.StatementSections('hash',(ready,)))==_section_evidence(m.StatementSections('hash',(s,)))
 assert isinstance(ready.literal_cash_tail_rows,tuple)

@pytest.mark.parametrize('key,label,value',[
 ('fx_effect','四、汇率变动对现金及现金等价物的影响','1.23'),
 ('net_increase_cash','五、现金及现金等价物净增加额','4.56'),
])
def test_old_known_component_exact_object_wins_over_literal_rows(key,label,value):
 args=sample();old=row(label,value);s=replace(args[3],rows=(*args[3].rows,old));args=list(args);args[3]=s
 assert recover(args)==()
 # Even caller-supplied fallback metadata cannot displace a known old row.
 ready=replace(s,literal_cash_tail_rows=(row('四、汇率变动对现金及现金等价物的影响','999'),row('五、现金及现金等价物净增加额','888')))
 assert ready.component(key) is old

@pytest.mark.parametrize('change',[
 'missing_tail','wrong_tail','wrong_head','wrong_net','duplicate_tail','duplicate_head',
 'wrong_year','wrong_report_year','wrong_issuer','header_column_drift','missing_unit','other_unit','foreign_currency',
 'extra_body','extra_amount','fx_dash','missing_fx_char','crossed_fx_char','duplicate_net_char',
 'fx_amount_other_row','net_amount_other_row','suffix_wrong_column','suffix_far',
 'parent_inside_block','wrong_native_quote',
 'wrong_statement_number','wrong_mother_number',
])
def test_exact_word_and_character_block_rejects_bad_or_ambiguous_source(change):
 def mutate(pages):
  first,last=pages;words=last['words'];head=next(w for w in words if w['text']=='四、汇率变动对现金及现金等价物的');tail=next(w for w in words if w['text']=='影响');net=next(w for w in words if w['text']=='五、现金及现金等价物净增加额');fx=next(w for w in words if w['text']=='-393,565.01');nc=next(w for w in words if w['text']=='-65,724,129.86')
  if change=='missing_tail':words.remove(tail)
  if change=='wrong_tail':tail['text']='影响额说明'
  if change=='wrong_head':head['text']='四、汇率变动对其他资产的'
  if change=='wrong_net':net['text']='五、现金及现金等价物合计'
  if change=='duplicate_tail':words.append(copy.deepcopy(tail))
  if change=='duplicate_head':words.append(copy.deepcopy(head))
  if change=='wrong_year':next(w for w in words if w['text']=='2025' and 70<w['top']<90)['text']='2023'
  if change=='wrong_report_year':next(w for w in words if w['text']=='2025' and w['top']<50)['text']='2023'
  if change=='wrong_issuer':next(w for w in words if w['text']=='新希望乳业股份有限公司' and w['top']<50)['text']='另一股份有限公司'
  if change=='header_column_drift':next(w for w in words if w['text']=='2025' and 70<w['top']<90)['x0']+=2
  if change=='missing_unit':first['words']=[w for w in first['words'] if w['text']!='单位：元']
  if change=='other_unit':next(w for w in first['words'] if w['text']=='单位：元')['text']='单位：千元'
  if change=='foreign_currency':first['words'].append(dict(text='币种：美元',x0=300,x1=380,top=526,bottom=535))
  if change=='extra_body':words.append(dict(text='会计政策说明',x0=90,x1=170,top=548,bottom=557))
  if change=='extra_amount':words.append(dict(text='1.00',x0=230,x1=255,top=fx['top'],bottom=fx['bottom']))
  if change=='fx_dash':fx['text']='-'
  if change=='missing_fx_char':last['chars'].remove(next(c for c in last['chars'] if c['text']=='-' and abs(c['top']-fx['top'])<.1 and 320<c['x0']<330))
  if change=='crossed_fx_char':next(c for c in last['chars'] if c['text']=='-' and abs(c['top']-fx['top'])<.1 and 320<c['x0']<330).update(x0=375,x1=385)
  if change=='duplicate_net_char':last['chars'].append(copy.deepcopy(next(c for c in last['chars'] if c['text']=='-' and abs(c['top']-nc['top'])<.1 and 310<c['x0']<320)))
  if change=='fx_amount_other_row':fx.update(top=fx['top']+12,bottom=fx['bottom']+12)
  if change=='net_amount_other_row':nc.update(top=nc['top']-12,bottom=nc['bottom']-12)
  if change=='suffix_wrong_column':tail.update(x0=280,x1=298)
  if change=='suffix_far':tail.update(top=tail['top']+4,bottom=tail['bottom']+4)
  if change=='parent_inside_block':words.append(dict(text='母公司现金流量表',x0=50,x1=200,top=550,bottom=559))
  if change=='wrong_native_quote':last['nativeText']=last['nativeText'].replace('影五响、','影五响，')
  if change=='wrong_statement_number':next(w for w in first['words'] if w['text']=='5' and 490<w['top']<510)['text']='附注'
  if change=='wrong_mother_number':next(w for w in words if w['text']=='6' and 607<w['top']<625)['text']='附注'
 assert recover(sample(mutate))==()

@pytest.mark.parametrize('changes',[
 {'role':'母公司'},{'statement':'利润表'},{'combined':True},{'scope_columns':4},
 {'unit':'千元'},{'current_header':'2024年度'},{'prior_header':'2023年度'},
])
def test_no_role_period_unit_or_column_inference(changes):
 args=list(sample());args[3]=replace(args[3],**changes);assert recover(args)==()

def test_page_and_mother_boundary_must_remain_exact():
 args=list(sample());args[2]=SimpleNamespace(**vars(args[2]));args[2].page_index+=1
 assert recover(args)==()
 args=list(sample());args[2]=SimpleNamespace(**vars(args[2]));args[2].role='合并';assert recover(args)==()

def test_gansu_h1_unknown_fx_stays_missing_and_strict_identity_false():
 saved=_load_private_json(Path(__file__).resolve().parents[1]/'fixtures/cninfo_002946_interleaved_cash/gansu_h125_unknown_fx.json')
 evidence=saved['identity']['evidence'];rows=[]
 for q in evidence.values():
  if q is None:continue
  parsed=m._row_amounts(q['raw_line']);assert parsed and parsed[2]==Decimal(q['amount_yuan'])
  rows.append(m.StatementRow(q['role'],q['statement'],q['label'],parsed[2],parsed[3],q['page'],q['amount_unit'],q['current_cell'],str(parsed[3]),q['raw_line']))
 s=m.StatementSection('合并','现金流量表',54,56,'元','2025年半年度','2024年半年度',tuple(rows))
 assert s.component('fx_effect') is None and s.component('net_increase_cash') is not None
 terms=[(s.component('net_increase_cash').amount,1),(s.component('investing_cashflow').amount,-1),(s.component('financing_cashflow').amount,-1),(None,-1)]
 assert not _check_identity('cashflow_identity',terms,expected=Decimal('2224939246.42'),evidence=evidence).holds
 args=list(sample());args[3]=s;assert m._literal_interleaved_annual_cash_tail_rows(*args[:4],date(2025,6,30))==()

def test_new_dataclass_field_is_not_in_machine_section_evidence():
 args=sample();s=args[3]
 assert asdict(s)['literal_cash_tail_rows']==()
 output=_section_evidence(m.StatementSections('hash',(s,)))[0]
 assert set(output)=={'role','statement','start_page','end_page','amount_unit','current_column','prior_column','located_row_count'}

OLD=_load_private_json(Path(__file__).resolve().parents[1]/'fixtures/cninfo_002946_interleaved_cash/old3_known_components.json')

@pytest.mark.parametrize('saved',OLD['records'],ids=lambda r:r['periodEnd'])
def test_actual_saved_supplement_known_components_return_old_objects(saved):
 def original(q):
  parsed=m._row_amounts(q['raw_line']);assert parsed and parsed[2]==Decimal(q['amount_yuan'])
  return m.StatementRow(q['role'],q['statement'],q['label'],parsed[2],parsed[3],q['page'],q['amount_unit'],q['current_cell'],str(parsed[3]),q['raw_line'])
 fx,net=original(saved['fx']),original(saved['netIncrease'])
 s=m.StatementSection('合并','现金流量表',fx.page,fx.page,'元','原当期','原比较',(fx,net))
 candidate=replace(s,literal_cash_tail_rows=(row('四、汇率变动对现金及现金等价物的影响','999'),row('五、现金及现金等价物净增加额','888')))
 assert candidate.component('fx_effect') is fx and candidate.component('net_increase_cash') is net
 assert candidate.component('fx_effect').as_evidence()==s.component('fx_effect').as_evidence()
 assert candidate.component('net_increase_cash').as_evidence()==s.component('net_increase_cash').as_evidence()
 assert _section_evidence(m.StatementSections('hash',(candidate,)))==_section_evidence(m.StatementSections('hash',(s,)))

def test_two_explicit_cover_issuers_never_form_one_chain(monkeypatch):
 monkeypatch.setitem(FIX,'coverNativeTextFixture',FIX['coverNativeTextFixture']+'\n另一股份有限公司2025年年度报告全文')
 assert recover(sample())==()

