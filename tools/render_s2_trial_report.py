"""从已有 S2 因子及公告绑定 JSON 生成独立中文 HTML 和摘要；完全离线。"""

from __future__ import annotations

import argparse
import html
import json
import os
from collections import Counter, defaultdict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from uuid import uuid4


FACTOR_KEYS = ("F07", "F08", "F09", "F10")
FOCUS_PAIRS = (
    ("000034", "2025-12-31"), ("000036", "2024-06-30"),
    ("000506", "2024-06-30"), ("000514", "2025-12-31"),
    ("600965", "2024-12-31"),
)
STATUS_LABELS = {
    "RECONSTRUCTED_READY": "重建可用", "NO_PERIODIC_REPORT": "无匹配全文报告",
    "REVISION_REVIEW_REQUIRED": "修订版仍需核验", "INDEX_INCOMPLETE": "索引不完整",
    "MATCH": "五字段匹配", "MISMATCH": "五字段存在差异",
    "PDF_UNVERIFIED": "PDF 五字段未全部通过核验",
    "PDF_NOT_ARCHIVED": "PDF 未归档", "ABSENT": "未提供修订 PDF 核验证据",
    "S2_NON_POSITIVE_DENOMINATOR": "分母非正",
    "S2_TTM_INCOMPLETE": "TTM 所需期间不完整",
    "S2_FINANCIAL_FACT_MISSING": "财务事实缺失",
}


def read_document(path: Path, key: str) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or not isinstance(value.get(key), list):
        raise ValueError(f"{path}: expected an object with a {key} list")
    if any(not isinstance(row, dict) for row in value[key]):
        raise ValueError(f"{path}: {key} must contain objects")
    return value


def signal_score(row: dict) -> Decimal:
    try:
        score = Decimal(str(row["signal"]["signal_rank"]))
    except (KeyError, TypeError, InvalidOperation) as exc:
        raise ValueError("a signal is missing a numeric signal_rank") from exc
    if not score.is_finite():
        raise ValueError("signal_rank must be finite")
    return score


def factor_summary(document: dict, path: Path) -> tuple[dict, list[dict]]:
    rows = document["instruments"]
    codes = [str(row["instrument_id"]) for row in rows]
    if len(codes) != len(set(codes)):
        raise ValueError(f"{path}: repeated instrument_id in factor report")
    signals = sorted((row for row in rows if row.get("signal") is not None),
                     key=lambda row: (-signal_score(row), str(row["instrument_id"])))
    compact = []
    for row in rows:
        factors = row.get("factors") or {}
        signal = row.get("signal") or {}
        compact.append({
            "instrument_id": str(row["instrument_id"]),
            **{key: factors.get(key) for key in FACTOR_KEYS},
            "signal_rank": signal.get("signal_rank"),
            "latest_period_end": factors.get("latest_period_end"),
            "exclusion_code": row.get("exclusion_code"),
            "exclusion_reason": row.get("exclusion_reason"),
        })
    by_code = {row["instrument_id"]: row for row in compact}
    complete = sum(isinstance(row.get("factors"), dict) and all(
        row["factors"].get(key) is not None for key in FACTOR_KEYS) for row in rows)
    excluded = [row for row in rows if row.get("signal") is None]
    summary = {
        "path": str(path.resolve()), "generated_at": document.get("generated_at"),
        "trading_day": document.get("trading_day"),
        "decision_cutoff_at": document.get("decision_cutoff_at"),
        "pit_mode": document.get("pit_mode"), "boundary": document.get("boundary"),
        "facts_db": document.get("facts_db"),
        "market_cap_source": document.get("market_cap_source") or {},
        "pool_size": len(rows), "complete_factor_count": complete,
        "ranked_count": len(signals), "excluded_count": len(excluded),
        "market_cap_matched": document.get("market_cap_matched"),
        "reviewed_complete_factor_count": document.get("reviewed_complete_factor_count"),
        "exclusion_counts": dict(sorted(Counter(
            str(row.get("exclusion_code") or "UNSPECIFIED") for row in excluded).items())),
        "required_periods_for_latest": document.get("required_periods_for_latest") or {},
        "input_declared_counts": {key: document.get(key) for key in (
            "pool_size", "complete_factor_count", "ranked_count")},
        "top20": [by_code[str(row["instrument_id"])] for row in signals[:20]],
    }
    return summary, sorted(compact, key=lambda row: row["instrument_id"])


def bound_summary(document: dict, path: Path) -> dict:
    rows = document["records"]
    by_period: dict[str, Counter] = defaultdict(Counter)
    proofs: list[dict] = []
    for row in rows:
        proof = row.get("revision_value_verification") or {}
        by_period[str(row.get("period_end", "UNKNOWN"))][str(proof.get("status", "ABSENT"))] += 1
        if proof.get("receipt_id") and proof.get("content_hash"):
            verification = proof.get("verification") or {}
            fields = [{key: field.get(key) for key in (
                "field", "verified", "value_yuan", "satisfied_paths")}
                for field in verification.get("fields", []) if isinstance(field, dict)]
            proofs.append({
                "instrument_id": str(row["instrument_id"]), "period_end": row.get("period_end"),
                "binding_status": row.get("status"), "status": proof.get("status"),
                "announcement_id": proof.get("announcement_id"),
                "receipt_id": proof.get("receipt_id"), "content_hash": proof.get("content_hash"),
                "reason": proof.get("reason"), "verified_fields": proof.get("verified_fields"),
                "fields": fields, "differences": proof.get("differences") or {},
            })
    priority = {pair: index for index, pair in enumerate(FOCUS_PAIRS)}
    proofs.sort(key=lambda row: (
        priority.get((row["instrument_id"], row["period_end"]), len(priority)),
        row["instrument_id"], str(row["period_end"])))
    proof_counts = Counter()
    for counts in by_period.values():
        proof_counts.update(counts)
    return {
        "path": str(path.resolve()), "generated_at": document.get("generated_at"),
        "observed_at": document.get("observed_at"), "boundary": document.get("boundary"),
        "record_count": len(rows),
        "security_code_count": len({str(row["instrument_id"]) for row in rows}),
        "periods": sorted(by_period),
        "status_counts": dict(sorted(Counter(str(row.get("status", "UNKNOWN"))
                                              for row in rows).items())),
        "revision_proof_counts": dict(sorted(proof_counts.items())),
        "revision_proofs_by_period": {period: dict(sorted(counts.items()))
                                      for period, counts in sorted(by_period.items())},
        "archived_pdf_proof_records": len(proofs), "pdf_examples": proofs[:5],
        "input_declared_counts": document.get("counts"),
        "alias_evidence_record_count": sum(bool(row.get("security_alias_evidence")) for row in rows),
    }


def esc(value: object) -> str:
    # All source strings, including serialized JSON, enter HTML only as escaped text.
    if value is None:
        return "—"
    if isinstance(value, (dict, list, tuple)):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True)
    return html.escape(str(value), quote=True)


def number(value: object) -> str:
    if value is None:
        return "—"
    try:
        parsed = Decimal(str(value))
        if parsed.is_finite():
            return format(parsed, ".6f")
    except InvalidOperation:
        pass
    return esc(value)


def table(headers: list[str], rows: list[list[object]]) -> str:
    return '<div class="table-wrap"><table><thead><tr>' + "".join(
        f"<th>{esc(cell)}</th>" for cell in headers) + "</tr></thead><tbody>" + "".join(
        "<tr>" + "".join(f"<td>{esc(cell)}</td>" for cell in row) + "</tr>"
        for row in rows) + "</tbody></table></div>"


def count_table(current: dict, baseline: dict | None, key: str) -> str:
    current_counts = current[key]
    base_counts = baseline[key] if baseline else {}
    keys = sorted(set(current_counts) | set(base_counts))
    headers = ["状态 / 原始代码", "当前记录数"]
    if baseline:
        headers += ["基线记录数", "变化"]
    rows = []
    for status in keys:
        count = current_counts.get(status, 0)
        row = [f"{STATUS_LABELS.get(status, status)} · {status}", count]
        if baseline:
            row += [base_counts.get(status, 0), count - base_counts.get(status, 0)]
        rows.append(row)
    return table(headers, rows)


def factor_rows(rows: list[dict], *, filterable: bool = False) -> str:
    body = []
    for index, row in enumerate(rows, 1):
        attr = f' data-code="{esc(row["instrument_id"])}"' if filterable else ""
        cells = [str(index), esc(row["instrument_id"])] + [number(row[key]) for key in FACTOR_KEYS]
        cells += [number(row["signal_rank"]), esc(row.get("latest_period_end")),
                  esc(row.get("exclusion_code")), esc(row.get("exclusion_reason"))]
        body.append(f"<tr{attr}>" + "".join(f"<td>{cell}</td>" for cell in cells) + "</tr>")
    return '<div class="table-wrap"><table' + (' id="pool-table"' if filterable else "") + (
        '><thead><tr>' + "".join(f"<th>{esc(h)}</th>" for h in (
            "序号", "证券代码", *FACTOR_KEYS, "综合分 signal_rank", "最新报告期", "排除代码", "排除原因"))
        + "</tr></thead><tbody>" + "".join(body) + "</tbody></table></div>")


def render(summary: dict, pool: list[dict]) -> str:
    factor, bound = summary["factor"], summary["bound"]
    baseline_factor, baseline_bound = summary["baseline_factor"], summary["baseline_bound"]
    source = factor["market_cap_source"]
    source_rows = [[key, source.get(key)] for key in (
        "source_id", "market_cap_as_of", "archive_verified", "placeholder_used",
        "observed_at", "receipt_id", "content_hash", "path", "archive_root")]
    factor_count_rows = []
    for key, label in (("pool_size", "代码池规模"), ("complete_factor_count", "实际完整四因子"),
                       ("ranked_count", "实际信号排名"), ("excluded_count", "实际排除"),
                       ("market_cap_matched", "市值匹配（源报告）"),
                       ("reviewed_complete_factor_count", "PDF 已复核完整因子（源报告）")):
        row = [label, factor.get(key)]
        if baseline_factor:
            row.append(baseline_factor.get(key))
        factor_count_rows.append(row)
    proof_statuses = sorted(bound["revision_proof_counts"])
    proof_period_rows = [[period] + [counts.get(status, 0) for status in proof_statuses]
                         for period, counts in bound["revision_proofs_by_period"].items()]
    examples = []
    for proof in bound["pdf_examples"]:
        fields = [[field.get("field"), field.get("value_yuan"), field.get("verified"),
                   field.get("satisfied_paths")] for field in proof["fields"]]
        examples.append(
            f'<details open><summary>{esc(proof["instrument_id"])} / {esc(proof["period_end"])}'
            f' · {esc(proof["status"])}</summary>'
            + table(["属性", "输入证据"], [[key, proof.get(key)] for key in (
                "binding_status", "announcement_id", "receipt_id", "content_hash", "reason",
                "verified_fields", "differences")])
            + (table(["字段", "PDF 值（元）", "核验通过", "满足路径"], fields)
               if fields else "<p>输入未提供逐字段核验明细。</p>") + "</details>")
    provenance = [["当前因子", factor["path"]], ["当前绑定", bound["path"]],
                  ["事实库", factor.get("facts_db")]]
    if baseline_factor:
        provenance.append(["基线因子", baseline_factor["path"]])
    if baseline_bound:
        provenance.append(["基线绑定", baseline_bound["path"]])
    boundary = (
        "本页展示历史重建研究试运行。财务事实的历史可用时点来自重建证据，不证明系统在决策时点已观察到这些事实。"
        if factor.get("pit_mode") == "HISTORICAL_RECONSTRUCTED" else
        "请按下列输入 PIT 模式和原报告边界理解结果；未提供 PIT 模式时不推断其性质。")
    if source.get("placeholder_used"):
        boundary += " 本次使用显式模拟市值分母，仅为演示和引擎检查，不能视为真实市值排名。"
    else:
        boundary += " 市值来源、归档校验状态均按输入报告显示；本工具不重新读取供应商或验证归档。"
    return f'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'">
<title>S2 实际试运行 · {esc(factor.get("trading_day"))}</title>
<style>
body{{margin:0;background:#f4f6f9;color:#17243a;font:15px/1.65 system-ui,"Microsoft YaHei",sans-serif}}
main{{max-width:1320px;margin:28px auto;padding:0 24px 40px}}header,section{{background:white;border:1px solid #dce3ec;border-radius:12px;padding:22px;margin:18px 0}}
h1{{font-size:29px;margin:0 0 8px}}h2{{font-size:21px;margin:0 0 14px}}h3{{font-size:17px}}p{{margin:8px 0}}.muted{{color:#56647a}}.boundary{{background:#edf3fc;border-left:4px solid #3b69aa;padding:14px}}
.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:14px;margin:18px 0}}.card{{background:#edf3fc;border-radius:9px;padding:15px}}.card strong{{display:block;font-size:30px}}
.table-wrap{{overflow:auto}}table{{border-collapse:collapse;width:100%;font-size:13px}}th,td{{text-align:left;border-bottom:1px solid #e1e6ee;padding:9px 11px;vertical-align:top}}th{{background:#eef2f7;white-space:nowrap}}td{{overflow-wrap:anywhere}}tr:nth-child(even){{background:#fafbfd}}
input{{font:inherit;padding:9px 12px;border:1px solid #9ba9be;border-radius:7px;min-width:230px}}summary{{font-weight:600;cursor:pointer;padding:12px 0}}details{{border-top:1px solid #dce3ec;margin-top:14px}}.source{{white-space:pre-wrap;overflow-wrap:anywhere}}
@media print{{body{{background:white}}main{{max-width:none;margin:0;padding:0}}section,header{{break-inside:avoid;border-radius:0}}#pool-section{{break-inside:auto}}input{{display:none}}}}
</style></head><body><main>
<header><p class="muted">离线研究 · 输入事实展示</p><h1>S2 实际试运行报告</h1>
<p>决策日：{esc(factor.get("trading_day"))}　决策截止：{esc(factor.get("decision_cutoff_at"))}</p>
<p class="muted">页面生成：{esc(summary["generated_at"])}<br>因子生成：{esc(factor.get("generated_at"))}　绑定生成：{esc(bound.get("generated_at"))}</p>
<div class="cards"><div class="card">代码池<strong>{factor["pool_size"]}</strong></div><div class="card">完整四因子<strong>{factor["complete_factor_count"]}</strong></div><div class="card">实际排名<strong>{factor["ranked_count"]}</strong></div><div class="card">明确排除<strong>{factor["excluded_count"]}</strong></div></div>
<p class="boundary">{esc(boundary)} 本页不做收益回测、交易建议或部署。</p></header>
<section><h2>数据来源与时点边界</h2><p>PIT 模式：{esc(factor.get("pit_mode"))}</p>
{table(["市值字段", "输入值"], source_rows)}
<details><summary>原报告边界和文件来源</summary><p class="source">{esc(factor.get("boundary"))}</p><p class="source">{esc(bound.get("boundary"))}</p>{table(["文件", "路径"], provenance)}</details></section>
<section><h2>公告绑定：基线与当前</h2><p>当前 {bound["record_count"]} 条代码×报告期记录，{bound["security_code_count"]} 个不同证券代码。
证券代码数不等于唯一上市公司数：官方旧、新代码可能共享 orgId，本页不计算唯一发行人数。</p>
<p>当前有 {bound["alias_evidence_record_count"]} 条记录附带证券别名证据。{esc("未提供基线绑定文件。" if not baseline_bound else "各状态按实际 records 重算。")}</p>
{count_table(bound, baseline_bound, "status_counts")}</section>
<section><h2>实际因子与排除分布</h2><p>完整因子和排名按实际 instruments 重算；排名数与 PDF 复核数分列。</p>
{table(["指标", "当前"] + (["基线"] if baseline_factor else []), factor_count_rows)}
{count_table(factor, baseline_factor, "exclusion_counts")}</section>
<section><h2>报告期间与修订 PDF 五字段证明</h2><p>输入包含报告期：{esc(bound["periods"])}。四个因子 F07–F10 使用归母净利润、合并净利润、经营现金流、归母权益、营业收入等五个财务字段。</p>
{table(["财务字段", "因子所需期间"], [[key, value] for key, value in factor["required_periods_for_latest"].items()])}
<h3>修订 PDF 核验状态（按报告期）</h3><p>ABSENT 表示输入没有 revision_value_verification，不能解读为已核验；有归档引用也不等于 MATCH。</p>
{count_table(bound, baseline_bound, "revision_proof_counts")}
{table(["报告期"] + proof_statuses, proof_period_rows)}
<h3>已归档 PDF 重点样例</h3><p>当前 {bound["archived_pdf_proof_records"]} 条核验证据含收据和哈希；最多展示 5 条，逐项沿用输入结果。</p>
{"".join(examples) if examples else "<p>输入没有带收据和哈希的 PDF 核验样例。</p>"}</section>
<section><h2>实际综合分前 20</h2><p>按输入 signal_rank 降序排列。序号是此表位置；综合分为真实输入值。四因子为原始比率，显示六位小数，完整精度见输入 JSON；没有来源的股票名称不补写。</p>
{factor_rows(factor["top20"])}</section>
<section id="pool-section"><h2>完整 {factor["pool_size"]} 行代码池</h2><p>按证券代码排列。输入代码或其中一部分即可筛选；所有行已在本地页面，无需网络。</p>
<label>证券代码筛选 <input id="code-filter" type="search" placeholder="例如 601628" autocomplete="off"></label>
<p id="filter-count" class="muted">显示 {factor["pool_size"]} / {factor["pool_size"]} 行</p>
{factor_rows(pool, filterable=True)}</section></main>
<script>
const filter = document.getElementById('code-filter');
const poolRows = Array.from(document.querySelectorAll('#pool-table tbody tr'));
filter.addEventListener('input', () => {{
  const query = filter.value.trim(); let visible = 0;
  poolRows.forEach(row => {{ const show = row.dataset.code.includes(query); row.hidden = !show; if (show) visible++; }});
  document.getElementById('filter-count').textContent = '显示 ' + visible + ' / ' + poolRows.length + ' 行';
}});
</script></body></html>
'''


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-factor", type=Path)
    parser.add_argument("--factor", type=Path, required=True)
    parser.add_argument("--baseline-bound", type=Path)
    parser.add_argument("--bound", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--summary-output", type=Path, required=True)
    args = parser.parse_args(argv)
    inputs = {path.resolve() for path in (
        args.baseline_factor, args.factor, args.baseline_bound, args.bound) if path}
    outputs = {args.output.resolve(), args.summary_output.resolve()}
    if len(outputs) != 2 or inputs & outputs:
        parser.error("outputs must be distinct and cannot overwrite input documents")
    factor, pool = factor_summary(read_document(args.factor, "instruments"), args.factor)
    bound = bound_summary(read_document(args.bound, "records"), args.bound)
    summary = {
        "schema": "aquant.s2-trial-report-summary.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "factor": factor, "bound": bound,
        "baseline_factor": (factor_summary(read_document(args.baseline_factor, "instruments"),
                                           args.baseline_factor)[0] if args.baseline_factor else None),
        "baseline_bound": (bound_summary(read_document(args.baseline_bound, "records"),
                                        args.baseline_bound) if args.baseline_bound else None),
        "boundary": "Offline display of supplied research artifacts; no collection, backtest, advice or deployment.",
    }
    page = render(summary, pool)
    encoded_summary = json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    atomic_write(args.output.resolve(), page)
    atomic_write(args.summary_output.resolve(), encoded_summary)
    print(f"{args.output.resolve()}: pool={factor['pool_size']} ranked={factor['ranked_count']} "
          f"excluded={factor['excluded_count']} bound_records={bound['record_count']}")
    print(args.summary_output.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
