"""把已绑定公告时点的结构化值写成**正式历史重建事实**。

这是 C 包（历史重建）的最后一段：``collect_structured_financials.py`` 取值、
``bind_structured_to_announcements.py`` 绑官方公告时点，本工具把结果落成
``FinancialFact``，其 ``pit_mode=HISTORICAL_RECONSTRUCTED``、
``availability_basis=RECONSTRUCTED``、``available_at`` 由公告日按已证实交易日历
推导。消费端 ``admissible_facts(allow_reconstructed=True)`` 随之放行。

## 写入前的硬门槛（任一不满足就**不写**）

1. 该条绑定状态必须是 ``RECONSTRUCTED_READY``；更正/修订期间只有所选最新
   PDF 的五字段机器核验并与结构化值一致才可放行，未证实仍待复核；
2. 五个必需字段**必须齐全**——缺任何一个就不写这条，**绝不填零或拿本期值顶替**；
3. 公告日期与 ``available_at`` 必须存在（时点是这条事实可用的前提）。

默认 **dry-run**；只有显式 ``--commit`` 才写库。

用法::

    python tools/ingest_reconstructed_facts.py --bound tmp/bind-probe.json
    python tools/ingest_reconstructed_facts.py --bound data/.../bound-facts.json --commit
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
from contextlib import closing
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aquant.domain.data.pit import AvailabilityBasis, PitMode  # noqa: E402
from aquant.domain.fundamentals.fact_repository import (  # noqa: E402
    FinancialFactRepository,
)
from aquant.domain.fundamentals.versioned import (  # noqa: E402
    FinancialFact, ProfitScope, StatementScope,
)

BOUND = ROOT / "data" / "s2-structured" / "bound-facts.json"
REPOSITORY = ROOT / "data" / "s2-pdf-review" / "s2-machine-verified-facts.sqlite"
SOURCE_ID = "eastmoney-structured-f10"

#: S2 的五个必需字段。缺任何一个都足以让该期间不可用（TTM 要成套）。
REQUIRED = (
    "revenue", "net_profit_attributable", "net_profit_consolidated",
    "parent_equity", "operating_cashflow",
)

#: 逐字段的 ``profit_scope``。**不能一律写 None**：两个利润指标必须区分归属口径，
#: 否则同一 (证券, 期间) 的"归母"与"合并"会失去区分度，而且下游按 profit_scope
#: 排序时会拿 ``None`` 与枚举比较直接抛
#: ``'<' not supported between instances of 'NoneType' and 'ProfitScope'``。
#: 这里的取值与官方 PDF 路径**完全一致**（实测 PDF 事实：
#: ``net_profit_attributable→ATTRIBUTABLE``、``net_profit_consolidated→CONSOLIDATED``，
#: 其余三个为 ``None``）。
PROFIT_SCOPES: dict[str, ProfitScope | None] = {
    "revenue": None,
    "net_profit_attributable": ProfitScope.ATTRIBUTABLE,
    "net_profit_consolidated": ProfitScope.CONSOLIDATED,
    "parent_equity": None,
    "operating_cashflow": None,
}


def _fact(record: dict, metric: str, value: Decimal) -> FinancialFact:
    announcement = record["announcement"]
    period_end = date.fromisoformat(str(record["period_end"]))
    published = date.fromisoformat(str(announcement["published_date"])[:10])
    moment = datetime.fromisoformat(str(record["available_at"]))
    observed = datetime.fromisoformat(str(record["observed_at"]))
    # 同期五字段共享公告版本链接。修订值核验通过时使用所选 PDF 的实际哈希；
    # 普通结构化重建使用公告索引页哈希，这只证明公告链接，不等于 PDF 数值核验。
    # 每字段原始响应哈希/收据仍在绑定产物 evidence 中供复核。
    content_hash = str(record.get("report_content_hash")
                       or announcement.get("index_page_hash") or "")
    # 同一发行人的新旧证券代码可能链接同一公告；仓储 version_id 是全库唯一键。
    version_id = (f"em-{record['instrument_id']}-{announcement['announcement_id']}-"
                  f"{metric}-{period_end.isoformat()}")
    return FinancialFact(
        instrument_id=str(record["instrument_id"]), metric=metric,
        period_end=period_end, statement_scope=StatementScope.CONSOLIDATED,
        profit_scope=PROFIT_SCOPES[metric], value=value, currency="CNY",
        raw_unit="yuan",
        source_id=SOURCE_ID,
        source_document_id=str(announcement["announcement_id"]),
        source_published_date=published, source_published_at=None,
        timestamp_precision="DATE", first_seen_at=observed,
        ingested_at=datetime.fromisoformat(record["ingested_at"]),
        available_at=moment, availability_basis=AvailabilityBasis.RECONSTRUCTED,
        pit_mode=PitMode.HISTORICAL_RECONSTRUCTED, content_hash=content_hash,
        version_id=version_id,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bound", type=Path, default=BOUND)
    parser.add_argument("--repository", type=Path, default=REPOSITORY)
    parser.add_argument("--commit", action="store_true",
                        help="真正写库；默认只做校验与预演")
    args = parser.parse_args(argv)

    payload = json.loads(args.bound.resolve().read_text(encoding="utf-8"))
    records = payload.get("records") or []
    ingested_at = datetime.now(timezone.utc).isoformat()
    counts: Counter[str] = Counter()
    prepared: list[FinancialFact] = []
    for record in records:
        status = record.get("status")
        if status != "RECONSTRUCTED_READY":
            counts[f"skipped:{status}"] += 1
            continue
        values = record.get("values") or {}
        missing = [name for name in REQUIRED if values.get(name) in (None, "")]
        if missing:
            # 绝不用 0 或本期值顶替。
            counts["refused:incomplete_fields"] += 1
            continue
        if not record.get("available_at") or not record.get("observed_at"):
            counts["refused:no_availability"] += 1
            continue
        # 没有公告修订版身份就不写：``content_hash`` 为空会让同一期间的多张表
        # 在 ``_same_bundle`` 里凑不出唯一版本，等于把问题推到下游。
        announcement = record.get("announcement") or {}
        if not str(announcement.get("index_page_hash") or "").strip():
            counts["refused:no_announcement_identity"] += 1
            continue
        proof = record.get("revision_value_verification")
        if proof and proof.get("status") != "MATCH":
            counts["refused:unverified_revision_values"] += 1
            continue
        entry = dict(record, ingested_at=ingested_at)
        try:
            bundle = [_fact(entry, name, Decimal(str(values[name]))) for name in REQUIRED]
        except (InvalidOperation, ValueError, KeyError) as exc:
            counts["refused:unbuildable"] += 1
            print(f"  skip {record.get('instrument_id')} "
                  f"{record.get('period_end')}: {type(exc).__name__}: {exc}",
                  file=sys.stderr)
            continue
        prepared.extend(bundle)
        counts["prepared"] += 1

    # 仓储会拒绝"同一逻辑事实出现两个来源根"（``financial fact has two roots
    # for one logical fact``）。这是**正当的完整性守卫**，不该绕过：同一
    # (证券, 指标, 期间, 口径) 若已有 PDF 核验事实，那条就是它的根。
    #
    # 对这类键本工具**跳过**而不是改写既有事实：那些值已经过官方 PDF 机器核验，
    # 且我们已逐值对拍确认两边一致（23/23），因此跳过不损失任何信息。
    #
    # 逻辑键用**域自己的** ``FinancialFact.logical_key``，不自己拼：
    # 早期版本去查一个并不存在的 ``logical_key`` 列，还被 ``except`` 静默吞掉，
    # 结果守卫看起来生效、实际从未生效（真实写库时才炸出"two roots"）。
    existing_keys: set[str] = set()
    if args.repository.is_file():
        existing, _ = FinancialFactRepository.open_existing(
            args.repository).load_with_accepted_s2_pdf_reviews()
        existing_keys = {fact.logical_key for fact in existing.facts}
    kept: list[FinancialFact] = []
    for fact in prepared:
        if fact.logical_key in existing_keys:
            counts["skipped:already_has_a_root"] += 1
            continue
        kept.append(fact)
    prepared = kept

    print(f"{args.bound}: {len(records)} 条绑定记录 → {len(prepared)} 条事实")
    for key in sorted(counts):
        print(f"   {key}: {counts[key]}")
    if not prepared:
        print("nothing to write")
        return 0
    if not args.commit:
        print("dry-run（未写库）；加 --commit 才写入")
        return 0

    # 仓储用 ``_create_schema=True`` 的构造即"打开并确保表存在"；
    # ``open_existing`` 是只读视图，写不进去。库不存在时直接构造会建表，
    # 因此这里统一走构造函数而不是先判断文件存在。
    repository = FinancialFactRepository(args.repository)
    inserted = repository.append_many(prepared)
    print(f"写入完成：新增 {inserted} / 提交 {len(prepared)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
