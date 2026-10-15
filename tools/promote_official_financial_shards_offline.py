"""Consume explicit immutable Luna shards sequentially, without provider requests."""
import argparse,json,hashlib
from pathlib import Path
from promote_official_financials_offline import run


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan',type=Path,required=True)
    args=parser.parse_args()
    plan=json.loads(args.plan.read_text(encoding='utf-8'))
    for shard in plan['shards']:
        discovery=Path(shard['discoveryPath']); packet=Path(shard['pdfPacketPath'])
        for path,key in ((discovery,'discoveryHash'),(packet,'pdfPacketHash')):
            if 'sha256:'+hashlib.sha256(path.read_bytes()).hexdigest()!=shard[key]:
                raise ValueError(f'immutable shard bytes differ: {path}')
        if Path(shard['outputPath']).exists():
            report=json.loads(Path(shard['outputPath']).read_text(encoding='utf-8'))
            if report['binding']['discoveryHash']!=shard['discoveryHash'] or report['binding']['pdfPacketHash']!=shard['pdfPacketHash'] or report['binding']['stocks']!=shard['stocks']:
                raise ValueError('completed shard belongs to different frozen inputs')
            print(json.dumps({'shard':shard['name'],'status':'COMPLETED_UNCHANGED'},ensure_ascii=False),flush=True)
            continue
        print(json.dumps({'shard':shard['name'],'status':'STARTED','processingSecurityCount':len(shard['stocks'])},ensure_ascii=False),flush=True)
        result=run(discovery_path=discovery,pdf_packet_path=packet,
            fact_db=Path(plan['factDb']),calendar_path=Path(plan['calendarPath']),
            output_path=Path(shard['outputPath']),checkpoint_path=Path(shard['checkpointPath']),
            stocks=tuple(shard['stocks']))
        print(json.dumps({'shard':shard['name'],'status':'COMPLETED','counts':result['counts']},ensure_ascii=False),flush=True)
if __name__=='__main__':main()
