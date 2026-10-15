"""Read-only research candidates compiled from an auditable frozen factor trial."""
from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
from functools import lru_cache
from pathlib import Path

from aquant.artifact_paths import resolve_artifact_path

SCHEMA = "aquant.research-candidates.v1"
FACTORS = ("F07", "F08", "F09", "F10")
LABELS = {"F07": "归母盈利/平均权益", "F08": "经营现金流/合并利润",
          "F09": "营业收入 TTM 同比", "F10": "归母盈利/决策日市值"}


class ResearchCandidateError(ValueError):
    pass


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with resolve_artifact_path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _rank(value) -> float:
    number = float(value)
    if not math.isfinite(number) or not 0 <= number <= 1:
        raise ResearchCandidateError("rank must be finite and between zero and one")
    return number


def build_research_candidates(report: dict, bound: dict, *, facts: dict,
                              names: dict, industries: dict, provenance: list[dict]) -> dict:
    """Use the frozen formula's existing order; no new selection weights or orders."""
    cap = report.get("market_cap_source") or {}
    if (report.get("pit_mode") != "HISTORICAL_RECONSTRUCTED"
            or cap.get("archive_verified") is not True or cap.get("placeholder_used") is not False
            or cap.get("market_cap_as_of") != report.get("trading_day")):
        raise ResearchCandidateError("a verified real-cap historical research trial is required")
    rows = report["instruments"]
    if len({row["instrument_id"] for row in rows}) != len(rows):
        raise ResearchCandidateError("factor report has duplicate codes")
    bindings = {(row["instrument_id"], row["period_end"]): row for row in bound["records"]}
    eligible = sorted((row for row in rows if row.get("signal") is not None),
        key=lambda row: (-_rank(row["signal"]["signal_rank"]), row["instrument_id"]))
    if (len(eligible) != report.get("ranked_count") or len(eligible) != report.get("complete_factor_count")
            or len(rows) != report.get("pool_size")):
        raise ResearchCandidateError("declared factor counts differ from the actual rows")
    cutoff = datetime.fromisoformat(report["decision_cutoff_at"])
    candidates = []
    for position, row in enumerate(eligible, 1):
        code = row["instrument_id"]
        factor = row.get("factors") or {}
        values = {key: Decimal(str(factor[key])) for key in FACTORS}
        if not all(value.is_finite() for value in values.values()):
            raise ResearchCandidateError(f"non-finite factors for {code}")
        ranks = {key: _rank(row["signal"][key + "_rank"]) for key in FACTORS}
        used_periods = set()
        references = factor.get("fact_version_ids") or []
        if not references:
            raise ResearchCandidateError(f"{code}: missing source fact references")
        for version in references:
            fact = facts.get(version)
            if fact is None or fact.instrument_id != code or fact.available_at > cutoff:
                raise ResearchCandidateError(f"{code}: invalid or future fact reference {version}")
            period = fact.period_end.isoformat()
            binding = bindings.get((code, period)) or {}
            announcement = binding.get("announcement") or {}
            proof = binding.get("revision_value_verification") or {}
            if (binding.get("status") != "RECONSTRUCTED_READY"
                    or (proof and proof.get("status") != "MATCH")
                    or str(announcement.get("announcement_id")) != fact.source_document_id
                    or Decimal(str((binding.get("values") or {}).get(fact.metric))) != fact.value
                    or (binding.get("report_content_hash") or announcement.get("index_page_hash")) != fact.content_hash):
                raise ResearchCandidateError(f"{code}: fact does not match the admitted announcement bundle")
            used_periods.add(period)
        evidence = []
        for period in sorted(used_periods):
            binding = bindings[(code, period)]
            ann = binding["announcement"]
            evidence.append({"periodEnd": period, "announcementId": ann["announcement_id"],
                "title": ann["title"], "publishedDate": ann["published_date"], "url": ann["url"],
                "valueVerification": "MATCH" if binding.get("report_content_hash") else "STRUCTURED_BOUND",
                "receiptId": (binding.get("revision_value_verification") or {}).get("receipt_id") or ann["index_receipt_id"],
                "contentHash": binding.get("report_content_hash") or ann["index_page_hash"]})
        strongest = sorted(FACTORS, key=lambda key: (-ranks[key], key))[:2]
        basis = [f"{LABELS[key]} {values[key]:.6f}，本池排名百分位 {ranks[key] * 100:.2f}%"
                 for key in strongest]
        risks = []
        if values["F09"] < 0:
            risks.append(f"营业收入 TTM 同比为 {values['F09'] * 100:.2f}%，收入增长为负。")
        if values["F08"] < 1:
            risks.append(f"经营现金流/合并利润为 {values['F08']:.6f}，低于 1。")
        industry = industries.get(code, "")
        if industry.startswith("J"):
            risks.append("归档行业为金融业；现金流与权益口径需按金融行业解读，不能直接套用工商企业解释。")
        if any(item["valueVerification"] == "STRUCTURED_BOUND" for item in evidence):
            risks.append("部分期间为结构化财报与官方公告绑定，尚未逐份完成 PDF 数值核验。")
        risks.append("本次排序没有验证今日价格、停牌或风险警示状态，不具备本次模拟执行条件。")
        prefix = "SH" if code.startswith(("60", "68")) else "SZ" if code.startswith(("00", "30")) else "BJ"
        candidates.append({"instrumentId": f"{prefix}.{code}", "code": code,
            "displayName": names.get(code) or code, "position": position,
            "signalRank": _rank(row["signal"]["signal_rank"]),
            "factors": {key: str(values[key]) for key in FACTORS}, "factorRanks": ranks,
            "basis": basis, "risks": risks, "evidence": evidence})
    return {"schema": SCHEMA, "generatedAt": datetime.now(timezone.utc).isoformat(),
        "dataAsOf": report["trading_day"], "strategy": "S2_RESEARCH",
        "pitMode": report["pit_mode"], "researchOnly": True, "simulationAvailable": False,
        "poolSize": len(rows), "rankedCount": len(eligible), "excludedCount": len(rows) - len(eligible),
        "returnedCount": len(candidates), "candidates": candidates,
        "exclusions": [{"code": code, "count": count} for code, count in sorted(Counter(
            row.get("exclusion_code") or "UNSPECIFIED" for row in rows if row.get("signal") is None).items())],
        "source": {"factorReport": report.get("source_path"), "marketCapSource": cap["source_id"],
            "marketCapAsOf": cap["market_cap_as_of"], "archiveVerified": True, "placeholderUsed": False},
        "limitations": ["财报为历史重建；数据日期为 " + report["trading_day"] + "，不是今日收盘名单。",
            "入选依据来自冻结四因子与池内排序；没有收益预测、买入价格或仓位建议。",
            "研究候选使用独立数据集，未启用正式 S2 策略或模拟写链路。"],
        "provenance": provenance}


@lru_cache(maxsize=4)
def _frozen_description(sources: tuple[tuple[str, str, str], ...]) -> dict:
    """Rebuild user-visible facts from hash-checked immutable source files."""
    from aquant.domain.fundamentals.fact_repository import FinancialFactRepository
    import yaml
    paths = {role: resolve_artifact_path(path) for role, path, _ in sources}
    required = {"factor_report", "bound_records", "facts_db", "company_names", "industry_snapshot", "market_caps"}
    if not required <= paths.keys():
        raise ResearchCandidateError("missing frozen research inputs")
    report = json.loads(paths["factor_report"].read_text(encoding="utf-8"))
    report["source_path"] = next(path for role, path, _ in sources if role == "factor_report")
    bound = json.loads(paths["bound_records"].read_text(encoding="utf-8"))
    store, _ = FinancialFactRepository.open_existing(paths["facts_db"]).load_with_accepted_s2_pdf_reviews()
    names = {row["code"]: row.get("zwjc", row["code"]) for row in
             json.loads(paths["company_names"].read_bytes())["stockList"]}
    industry = yaml.safe_load(paths["industry_snapshot"].read_text(encoding="utf-8"))
    industries = {str(row["code"])[-6:]: str(row.get("industry") or "") for row in industry["instruments"]}
    return build_research_candidates(report, bound, facts={f.version_id: f for f in store.facts},
        names=names, industries=industries, provenance=[])


def load_research_candidates(path: Path, *, limit: int = 20) -> dict:
    if not 1 <= limit <= 100:
        raise ResearchCandidateError("limit must be between 1 and 100")
    packet = json.loads(resolve_artifact_path(path).read_text(encoding="utf-8"))
    if (packet.get("schema") != SCHEMA or packet.get("researchOnly") is not True
            or packet.get("simulationAvailable") is not False
            or packet.get("pitMode") != "HISTORICAL_RECONSTRUCTED"):
        raise ResearchCandidateError("artifact is not a read-only historical research candidate list")
    provenance = packet.get("provenance") or []
    if not provenance:
        raise ResearchCandidateError("missing frozen input provenance")
    for source in provenance:
        if file_hash(Path(source["path"])) != source["contentHash"]:
            raise ResearchCandidateError("frozen research input has changed: " + source["role"])
    factor_source = next((item for item in provenance if item["role"] == "factor_report"), None)
    if factor_source is None:
        raise ResearchCandidateError("missing frozen factor report")
    report = json.loads(resolve_artifact_path(factor_source["path"]).read_text(encoding="utf-8"))
    source_rows = sorted((row for row in report["instruments"] if row.get("signal") is not None),
        key=lambda row: (-_rank(row["signal"]["signal_rank"]), row["instrument_id"]))
    cap = report["market_cap_source"]
    if (packet.get("dataAsOf") != report["trading_day"]
            or packet.get("strategy") != "S2_RESEARCH"
            or packet.get("poolSize") != report["pool_size"]
            or packet.get("rankedCount") != len(source_rows)
            or cap.get("archive_verified") is not True or cap.get("placeholder_used") is not False
            or packet.get("source", {}).get("marketCapAsOf") != cap["market_cap_as_of"]):
        raise ResearchCandidateError("candidate dates or scope differ from the frozen trial")
    rows = packet.get("candidates") or []
    if (len(rows) != packet.get("rankedCount") or len({row["code"] for row in rows}) != len(rows)
            or packet.get("excludedCount") + len(rows) != packet.get("poolSize")):
        raise ResearchCandidateError("candidate coverage counts are inconsistent")
    for index, row in enumerate(rows, 1):
        if row["position"] != index:
            raise ResearchCandidateError("candidate positions are inconsistent")
        _rank(row["signalRank"])
        source_row = source_rows[index - 1]
        if (row["code"] != source_row["instrument_id"]
                or row["signalRank"] != source_row["signal"]["signal_rank"]
                or any(Decimal(row["factors"][key]) != Decimal(source_row["factors"][key]) for key in FACTORS)
                or any(row["factorRanks"][key] != source_row["signal"][key + "_rank"] for key in FACTORS)):
            raise ResearchCandidateError("candidate values or order differ from the frozen trial")
    # Identity, explanations, warnings and evidence are product facts too; the
    # mutable display packet cannot promote their meaning independently.
    expected = _frozen_description(tuple(sorted((item["role"], item["path"], item["contentHash"])
                                                for item in provenance)))
    if (rows != expected["candidates"] or packet["source"] != expected["source"]
            or packet["limitations"] != expected["limitations"]
            or packet["exclusions"] != expected["exclusions"]):
        raise ResearchCandidateError("candidate identity, explanations or evidence differ from frozen inputs")
    return dict(packet, candidates=rows[:limit], returnedCount=min(len(rows), limit))
