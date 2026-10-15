"""Source-observed sub-point continuation strokes cannot change column identity."""
import pytest

from aquant.adapters.providers.cninfo_statement_geometry import _amount_columns_match_header


HEADER = {'column': 2, 'prior_column': 3,
          'bounds': ((84.45,316.85),(316.85,360.45),(360.45,447.6),(447.6,536.625))}
ROW = ((84.45,316.85),(316.85,360.45),(360.45,447.6),(447.6,536.9))
BOX = (84.45,76.49,536.9,766.24)


def test_actual_601799_continuation_stroke_keeps_dated_columns():
    assert _amount_columns_match_header(ROW, HEADER, BOX, continuation=True)
    assert not _amount_columns_match_header(ROW, HEADER, BOX, continuation=False)


@pytest.mark.parametrize('row,box', [
    (ROW[:-1] + ((447.6,538.1),), (84.45,76.49,538.1,766.24)),
    (ROW[:2] + (ROW[3],ROW[2]), BOX),
    (ROW[:2] + (None,ROW[3]), BOX),
    (ROW, (82.0,76.49,536.9,766.24)),
    (ROW[:2] + ((440.0,447.6),ROW[3]), BOX),
])
def test_wrong_geometry_missing_or_swapped_columns_are_rejected(row,box):
    assert not _amount_columns_match_header(row, HEADER, box, continuation=True)


def test_actual_002192_internal_stroke_keeps_both_amount_bands():
    header={'column':1,'prior_column':2,'bounds':((84.5,311.945),(311.945,425.37),(425.37,536.745))}
    row=((84.5,311.945),(311.945,425.045),(425.045,536.9))
    assert _amount_columns_match_header(row,header,(84.5,40,536.9,760),continuation=True)


def test_existing_exact_cells_keep_old_same_page_behavior():
    assert _amount_columns_match_header(HEADER['bounds'],HEADER,BOX,continuation=False)
