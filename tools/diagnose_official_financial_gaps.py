"""Offline per-report candidate and independent-path diagnostics; no fact writes."""
from __future__ import annotations
import argparse
from collections import defaultdict
from datetime import date, datetime, timezone
import hashlib, json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT))
from aquant.adapters.providers.cninfo_statement_sections import extract_section_facts
from aquant.adapters.providers.s2_machine_verification import verify_machine_evidence
from tools.promote_official_financials_offline import company_from_cover


def run(demand_path, packet_path, output_path, checkpoint_path, stocks=()):
    demand = json.loads(demand_path.read_text(encoding='utf-8'))
    packet = json.loads(packet_path.read_text(encoding='utf-8'))
    packet_hash = 'sha256:' + hashlib.sha256(packet_path.read_bytes()).hexdigest()
    if demand['pdfPacketHash'] != packet_hash:
        raise ValueError('diagnostic PDF source differs from frozen missing demand')
    pairs = defaultdict(list)
    for row in demand['demands']:
        if not stocks or row['code'] in stocks:
            pairs[(row['code'], row['periodEnd'])].append(row['metric'])
    binding = {'demandHash': 'sha256:' + hashlib.sha256(demand_path.read_bytes()).hexdigest(),
        'pdfPacketHash': packet_hash, 'stocks': list(stocks), 'parserHashes': {
            name: 'sha256:' + hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
            for name in ('src/aquant/adapters/providers/cninfo_financial_candidate.py',
                         'src/aquant/adapters/providers/cninfo_statement_sections.py',
                         'src/aquant/adapters/providers/s2_machine_verification.py')}}
    results = []
    if checkpoint_path.exists():
        existing = [json.loads(line) for line in checkpoint_path.read_text(encoding='utf-8').splitlines()]
        if existing[0].get('binding') != binding: raise ValueError('diagnostic checkpoint version differs')
        results = existing[1:]
    else:
        with checkpoint_path.open('x', encoding='utf-8') as stream:
            stream.write(json.dumps({'binding': binding}, ensure_ascii=False) + '\n')
    completed = {(row['code'], row['periodEnd']) for row in results}
    for (code, period), fields in sorted(pairs.items()):
        if (code, period) in completed: continue
        pdf = next(row for row in packet['pdfRecords'] if row['instrumentId'].split('.')[-1] == code and row['periodEnd'] == period)
        payload = Path(pdf['rawArtifactPath']).read_bytes()
        if 'sha256:' + hashlib.sha256(payload).hexdigest() != pdf['contentHash']:
            raise ValueError('raw original PDF changed during diagnostic')
        result = {'code': code, 'periodEnd': period, 'fields': sorted(fields),
                  'pdfContentHash': pdf['contentHash'], 'pdfPath': pdf['rawArtifactPath']}
        try:
            identity = company_from_cover(pdf['rawArtifactPath'], expected_code=code)
            result['companyIdentityEvidence'] = identity
            candidate = extract_section_facts(payload, instrument_id=code, company=identity['company'],
                period_end=date.fromisoformat(period), announcement_id=pdf['announcementId'],
                document_url=pdf['documentUrl'], required_fields=tuple(sorted(fields)))
            result['candidateValuesYuan'] = {key: str(row.value_yuan) for key, row in candidate.by_field.items()}
            verification = verify_machine_evidence(payload, candidate=candidate, expected_fields=tuple(sorted(fields)))
            result.update({'status': 'ALL_REQUIRED_FIELDS_VERIFIED' if verification.verified else 'INDEPENDENT_PATH_MISSING',
                           'verification': verification.as_evidence()})
        except Exception as exc:
            result.update({'status': 'EXTRACTION_FAILED', 'reason': f'{type(exc).__name__}: {exc}'})
        result['diagnosedAt'] = datetime.now(timezone.utc).isoformat()
        with checkpoint_path.open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(result, ensure_ascii=False) + '\n')
        results.append(result)
        print(json.dumps({key: result.get(key) for key in ('code', 'periodEnd', 'status', 'reason')}, ensure_ascii=False), flush=True)
    report = {'schema': 'aquant.financial-offline-diagnostics.v1', 'binding': binding,
              'generatedAt': datetime.now(timezone.utc).isoformat(), 'factWrites': 0,
              'providerRequestsStarted': 0, 'targetReportCount': len(pairs), 'results': results}
    with output_path.open('x', encoding='utf-8') as stream: json.dump(report, stream, ensure_ascii=False, indent=2)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--demand', type=Path, required=True)
    parser.add_argument('--pdf-packet', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--checkpoint', type=Path, required=True)
    parser.add_argument('--stock', action='append', default=[])
    args = parser.parse_args(argv)
    run(args.demand, args.pdf_packet, args.output, args.checkpoint, tuple(args.stock))
    return 0


if __name__ == '__main__': raise SystemExit(main())
