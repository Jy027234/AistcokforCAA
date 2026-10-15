from dataclasses import replace
import pytest
from aquant.adapters.providers import pdf_promotion as promotion
from test_pdf_promotion import _setup, _record, _at


def test_real_nssc_namespace_still_requires_exact_official_lookup_and_request(tmp_path,monkeypatch):
    con,archive,_,candidate,_,review=_setup(tmp_path,monkeypatch)
    try:
        receipt=_record(archive,b'[{"code":"605599","orgId":"nssc1000538"}]',
            url='https://www.cninfo.com.cn/new/information/topSearch/query?keyWord=605599&maxNum=10',
            seen=_at(24,16,11),detail='report-index:org:605599')
        assert promotion._verified_org_lookup(archive,receipt_id=receipt,
            stock_code='605599',organization_id='nssc1000538')['receipt_id'] == receipt
        with pytest.raises(promotion.PdfPromotionError,match='does not prove'):
            promotion._verified_org_lookup(archive,receipt_id=receipt,
                stock_code='605599',organization_id='nssc1000539')
        with pytest.raises(promotion.PdfPromotionError):
            promotion._verified_org_lookup(archive,receipt_id=receipt,
                stock_code='600519',organization_id='nssc1000538')
        candidate=replace(candidate,instrument_id='605599')
        review=replace(review,query_stock_code='605599',query_organization_id='nssc1000538',
            org_lookup_receipt_id=receipt,query_digest=promotion.cninfo_query_digest(
                stock_code='605599',organization_id='nssc1000538',
                period_start=review.query_period_start,period_end=review.query_period_end))
        # New namespace passes the format gate, but cannot repurpose an old
        # different-stock request/page receipt, even when its digest is typed.
        with pytest.raises(promotion.PdfPromotionError,match='lacks matching request binding'):
            promotion._version_evidence(archive,candidate,review,now=_at(25,17))
    finally:con.close()


def test_real_gfbj_namespace_keeps_exact_security_lookup_and_request(tmp_path,monkeypatch):
    con,archive,_,candidate,_,review=_setup(tmp_path,monkeypatch)
    try:
        receipt=_record(archive,b'[{"code":"600933","orgId":"gfbj0834393"}]',
            url='https://www.cninfo.com.cn/new/information/topSearch/query?keyWord=600933&maxNum=10',
            seen=_at(24,16,11),detail='report-index:org:600933')
        assert promotion._verified_org_lookup(archive,receipt_id=receipt,
            stock_code='600933',organization_id='gfbj0834393')['receipt_id']==receipt
        for code,org in [('600933','gfbj0834394'),('605305','gfbj0834393')]:
            with pytest.raises(promotion.PdfPromotionError):
                promotion._verified_org_lookup(archive,receipt_id=receipt,stock_code=code,organization_id=org)
        candidate=replace(candidate,instrument_id='600933')
        review=replace(review,query_stock_code='600933',query_organization_id='gfbj0834393',
            org_lookup_receipt_id=receipt,query_digest=promotion.cninfo_query_digest(
                stock_code='600933',organization_id='gfbj0834393',
                period_start=review.query_period_start,period_end=review.query_period_end))
        # A real lookup proves the namespace; a different-stock old page still
        # fails its original raw request binding, regardless of typed digest.
        with pytest.raises(promotion.PdfPromotionError,match='lacks matching request binding'):
            promotion._version_evidence(archive,candidate,review,now=_at(25,17))
        for invalid in ('gfbj083439','gfbj08343930','gfbjABCDEFG','gfbj0834393suffix'):
            changed=replace(review,query_organization_id=invalid,
                query_digest=promotion.cninfo_query_digest(stock_code='600933',organization_id=invalid,
                    period_start=review.query_period_start,period_end=review.query_period_end))
            with pytest.raises(promotion.PdfPromotionError,match='digest/pages/scope'):
                promotion._version_evidence(archive,candidate,changed,now=_at(25,17))
    finally:con.close()
