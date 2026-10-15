"""Provider cannot turn an unbound or partial impact claim into a financial fact."""
from copy import deepcopy
from datetime import date,datetime,timezone
from decimal import Decimal
from types import SimpleNamespace
import pytest
from aquant.adapters.providers import s2_revision_impact as impact
from aquant.adapters.providers.pdf_promotion import _validated_revision_impact,PdfPromotionError

NOW=datetime(2026,10,13,13,tzinfo=timezone.utc)
DONE=datetime(2026,10,13,12,58,tzinfo=timezone.utc)


def _inputs(tmp_path):
    candidate=SimpleNamespace(instrument_id='000902',period_end=date(2025,6,30),
                              candidates=(SimpleNamespace(field='revenue',value_yuan=Decimal('10')),),
                              pdf_sha256='sha256:'+'a'*64,document_url='https://static.cninfo.com.cn/finalpage/2025-08-01/123.PDF')
    company={'code':'000902','instrumentId':'SZ.000902','orgLookupReceiptId':'org'}
    descriptor={'root':str(tmp_path),'database_path':str(tmp_path/'meta.sqlite'),'company':company,
                'documents':[{'announcementId':'123'},{'announcementId':'alternate'}]}
    archive=SimpleNamespace(root=tmp_path,con=SimpleNamespace(execute=lambda _: [(0,'main',str(tmp_path/'meta.sqlite'))]))
    cross=SimpleNamespace(org_lookup_receipt_id='org',publication_window=(date(2024,6,30),date(2026,10,13)),
                          pages=(SimpleNamespace(receipt_id='page',request_body_hash='body'),))
    review=SimpleNamespace(org_lookup_receipt_id='org',reviewed_at=NOW,announcement_id='123')
    proof={'issuerName':'新洋丰农业科技股份有限公司','instrumentId':'SZ.000902','targetPeriodEnd':'2025-06-30',
           'completedAt':DONE.isoformat(),'relationshipScope':{'completeForArchivedIndexResponse':True,
            'publicationWindow':['2024-06-30','2026-10-13'],'requestBodyHashes':['body'],
            'financialCandidateIds':['123','alternate']},'indexReceipts':[{'receipt_id':'org'},{'receipt_id':'page'}],
           'outcomes':[{'metric':'revenue','periodEnd':'2025-06-30','status':'NO_EFFECT',
                        'currentSource':{'announcementId':'123','factPeriodEnd':'2025-06-30',
                         'sourceReportPeriodEnd':'2025-06-30','columnRole':'CURRENT','statementScope':'CONSOLIDATED','amountYuan':'10'}}],
           'sources':[{'announcementId':'123','originalHash':candidate.pdf_sha256,
                       'receipt':{'receipt_id':'pdf'},'index':{'documentUrl':candidate.document_url}}]}
    return dict(proof=proof,descriptor=descriptor,archive=archive,candidate=candidate,
                pdf_receipt_id='pdf',required_fields=('revenue',),cross=cross,review=review,
                reviewed_at=NOW,company_name=proof['issuerName'])


def test_provider_replays_complete_override_instead_of_using_true_flag(tmp_path,monkeypatch):
    inputs=_inputs(tmp_path);calls=[]
    monkeypatch.setattr(impact,'replay_revision_impact',lambda saved,**kwargs: calls.append((saved,kwargs)))
    assert _validated_revision_impact(**inputs)==inputs['proof']
    assert calls[0][1]['documents']==inputs['descriptor']['documents']
    assert calls[0][1]['target_metrics']==('revenue',)
    assert calls[0][1]['issuer_name']==inputs['company_name']


@pytest.mark.parametrize('mutation',[
    lambda p:p['outcomes'][0].update(status='AFFECTS'),
    lambda p:p['outcomes'][0].update(status='UNRESOLVED'),
    lambda p:p['outcomes'][0]['currentSource'].update(amountYuan='11'),
    lambda p:p['outcomes'][0]['currentSource'].update(columnRole='PRIOR'),
    lambda p:p['outcomes'][0]['currentSource'].update(announcementId='alternate'),
    lambda p:p['relationshipScope'].update(completeForArchivedIndexResponse=False),
    lambda p:p['relationshipScope'].update(requestBodyHashes=['other']),
    lambda p:p['sources'][0]['receipt'].update(receipt_id='other'),
    lambda p:p.update(completedAt=datetime(2026,10,13,13,1,tzinfo=timezone.utc).isoformat()),
])
def test_provider_exact_scope_value_time_and_source_gates(tmp_path,monkeypatch,mutation):
    inputs=_inputs(tmp_path);monkeypatch.setattr(impact,'replay_revision_impact',lambda *a,**k: None)
    mutation(inputs['proof'])
    with pytest.raises(PdfPromotionError):_validated_revision_impact(**inputs)


def test_saved_proof_failure_cannot_be_relabelled_no_effect(tmp_path,monkeypatch):
    inputs=_inputs(tmp_path)
    def reject(*args,**kwargs):raise impact.RevisionImpactError('alternate full report missing')
    monkeypatch.setattr(impact,'replay_revision_impact',reject)
    with pytest.raises(PdfPromotionError,match='alternate full report missing'):_validated_revision_impact(**inputs)


def test_missing_archive_or_foreign_archive_is_rejected(tmp_path,monkeypatch):
    inputs=_inputs(tmp_path);inputs['descriptor']=None
    with pytest.raises(PdfPromotionError):_validated_revision_impact(**inputs)
    inputs=_inputs(tmp_path);inputs['descriptor']['root']=str(tmp_path/'foreign')
    monkeypatch.setattr(impact,'replay_revision_impact',lambda *a,**k: None)
    with pytest.raises(PdfPromotionError):_validated_revision_impact(**inputs)
