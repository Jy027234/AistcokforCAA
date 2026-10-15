from datetime import date
from types import SimpleNamespace
from aquant.adapters.providers import cninfo_statement_geometry as grid


DAY=date(2025,6,30)


def _table(labels,bbox=(0,0,400,100)):
    cells=[];left=0
    for label in labels:
        width=80 if label else 5.4
        cells.append((left,0,left+width,10));left+=width
    return SimpleNamespace(extract=lambda:[labels],rows=[SimpleNamespace(cells=cells)],bbox=bbox)


def _fragment():
    return _table(['','项目','','','附注','','','2025年半年度','','','2024年半年度',''])


def test_fragmented_shading_has_many_empty_thin_cells_and_explicit_headers():
    assert grid._fragmented_header(_fragment(),DAY)
    assert not grid._fragmented_header(_table(['项目','附注','2025年半年度','2024年半年度']),DAY)
    assert not grid._fragmented_header(_table(['','项目','','','附注','','','金额','','','金额','']),DAY)


def test_local_retry_uses_same_outer_table_only_and_preserves_normal_default():
    fragmented=_fragment();normal=_table(['项目','2025年半年度','2024年半年度'],bbox=(0,200,400,300))
    recovered=_table(['项目','附注','2025年半年度','2024年半年度'])
    calls=[]
    def find(options):
        calls.append(options)
        return [fragmented,normal] if 'snap_x_tolerance' not in options else [recovered]
    result=grid._ruled_page_tables(SimpleNamespace(find_tables=find),DAY,'利润表','')
    assert result==(recovered,normal) and len(calls)==2
    assert calls[1]['snap_x_tolerance']==calls[1]['join_x_tolerance']==8


def test_wrong_outer_table_or_ambiguous_recovery_does_not_select_by_amount():
    default=_fragment();valid=_table(['项目','2025年半年度','2024年半年度'])
    wrong=_table(['项目','2025年半年度','2024年半年度'],bbox=(10,0,410,100))
    for alternatives in ([wrong],[valid,valid]):
        page=SimpleNamespace(find_tables=lambda options:alternatives if 'snap_x_tolerance' in options else [default])
        assert grid._ruled_page_tables(page,DAY,'利润表','')==(default,)
