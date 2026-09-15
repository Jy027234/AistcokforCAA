"""把数据源产物写入 M1 底座：证券、日历、行情、公司行为、来源登记。

职责边界（主文档 §14.3）：
    本模块只做"抓取与版本"，不做因子计算、不做组合、不碰账本。

三条硬约束，违反即拒绝：

1. **单位归一**（§6.3）：成交量为股、金额为元（存储用分）、价格为分。
   供应商的"手""千元"等原始单位必须显式转换并保留原始值，不得靠列名猜测。
2. **合成数据带水印**（§15.4）：SYNTHETIC 数据集的每条记录都必须能追溯到水印，
   且禁止与 PRODUCTION 混入同一快照。
3. **来源登记先于入库**（§17.2）：未登记权利的来源，其数据不得入库。

入库是幂等的：同一 data_version 重复导入不会产生重复行。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from aquant.domain.simulation.board_rules import BOARD_RULES
from aquant.domain.simulation.simulator import DailySimulator

from .db import write_tx

#: 供应商原始单位 -> 归一化。§6.3 要求显式转换并同时保留原始单位。
VOLUME_UNIT_TO_SHARES = {
    "SHARE": 1,
    "LOT": 100,        # A股 1 手 = 100 股（主板）
    "HUNDRED_SHARE": 100,
}
AMOUNT_UNIT_TO_CENTS = {
    "CNY": 100,        # 元 -> 分
    "CNY_10000": 1_000_000,   # 万元 -> 分
    "CNY_100000000": 10_000_000_000,  # 亿元 -> 分
}


class IngestError(Exception):
    def __init__(self, code: str, message: str, object_id: str, repair_action: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.object_id = object_id
        self.repair_action = repair_action

    def as_error(self) -> dict:
        return {"code": self.code, "message": self.message, "object_id": self.object_id,
                "retryable": False, "repair_action": self.repair_action}


@dataclass(frozen=True, slots=True)
class IngestReport:
    data_version: str
    instruments: int
    trading_days: int
    quotes: int
    corporate_actions: int
    events: int
    skipped: tuple[str, ...] = ()


def mark_board_limit_up(doc: dict, *, rules=None) -> int:
    """给清单里的行情行标注"当日**开盘**是否即涨停"，返回标注为真的行数。

    为什么必须在**写出快照时**算，而不是读取或模拟时算
    --------------------------------------------------
    `board_limit_up` 是一个派生事实：由 前收 + 交易所+板块+生效日的涨跌幅
    唯一决定。它必须与行情一起被冻结进快照——否则同一条行情在不同的
    规则版本下会得出不同结论，快照就不再是"可重放"的了。

    事故背景：这个字段原先只存在于示例 YAML 的手写行里，生产链路从不计算它，
    读取器只能取默认值 False。于是模拟器里"开盘涨停不得假定买入成交"的守卫
    （§12.3 / S02）在真实数据上从未生效——合成夹具反而测得出该行为，
    测试全绿而真实路径失效。

    判定口径：**开盘价达到涨停上限**才算。涨停收盘但开盘更低的情况，
    开盘是可以成交的，不能标记为涨停。四舍五入到分，与
    `DailySimulator.price_limits` 用同一套算法，避免两处各自取整。
    """

    rules = BOARD_RULES if rules is None else rules
    boards = {
        str(i.get("instrument_id")): (i.get("exchange", "OTHER"), i.get("board", "OTHER"))
        for i in (doc.get("instruments") or [])
    }
    marked = 0
    for q in doc.get("daily_quotes") or []:
        exchange, board = boards.get(str(q.get("instrument_id")), ("OTHER", "OTHER"))
        prev_close = q.get("prev_close_cents")
        day = q.get("trading_day")
        # 当日之前没有收盘价（窗口首行）就没有"涨停"可言，如实为 False
        if not prev_close or not day:
            q["board_limit_up"] = False
            continue
        try:
            trading_day = date.fromisoformat(str(day))
        except ValueError:
            q["board_limit_up"] = False
            continue
        matches = [r for r in rules
                   if r.exchange == exchange and r.board == board and r.covers(trading_day)]
        if len(matches) != 1:
            # 唯一匹配是硬要求：0 条是规则缺失，>1 条是规则冲突。
            # 两者都不能靠"猜一个"糊过去——但行情必须原样写出，
            # 所以这里只是不标记，由 §7.1 的规则校验另行报告。
            q["board_limit_up"] = False
            continue
        _, high_limit = DailySimulator.price_limits(prev_close, matches[0].price_limit_pct)
        q["board_limit_up"] = bool(q.get("open_cents")) and q["open_cents"] >= high_limit
        marked += 1 if q["board_limit_up"] else 0
    return marked


def _iso(dt: datetime | date) -> str:
    if isinstance(dt, datetime):
        if dt.tzinfo is None:
            raise IngestError("DATA_NOT_READY", "naive datetime rejected (§7.1)",
                              "<timestamp>", "store timezone-aware UTC")
        return dt.astimezone(timezone.utc).isoformat()
    return dt.isoformat()


def convert_volume(value: int, unit: str, *, object_id: str) -> int:
    """成交量归一到股。"""

    factor = VOLUME_UNIT_TO_SHARES.get(unit.upper())
    if factor is None:
        raise IngestError(
            "DATA_NOT_READY", f"unknown volume unit {unit!r}", object_id,
            f"declare one of {sorted(VOLUME_UNIT_TO_SHARES)}; never guess from the column name",
        )
    return int(value) * factor


def convert_amount(value: int | float, unit: str, *, object_id: str) -> int:
    """金额归一到分。"""

    factor = AMOUNT_UNIT_TO_CENTS.get(unit.upper())
    if factor is None:
        raise IngestError(
            "DATA_NOT_READY", f"unknown amount unit {unit!r}", object_id,
            f"declare one of {sorted(AMOUNT_UNIT_TO_CENTS)}; never guess from the column name",
        )
    return int(round(float(value) * factor))


class SnapshotBuilder:
    """从源清单构建一个快照的落盘产物。

    产出物 = 数据集文件（JSON）+ 数据集引用（含真实哈希），交给 SnapshotStore 发布。
    本类不直接写 snapshot 表——发布语义由 SnapshotStore 独占。
    """

    def __init__(self, con: sqlite3.Connection, dataset_dir: str | Path) -> None:
        self.con = con
        self.dataset_dir = Path(dataset_dir)

    # ------------------------------------------------------------ load
    def load_manifest(self, path: str | Path) -> dict:
        doc = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        if not isinstance(doc, dict):
            raise IngestError("DATA_NOT_READY", "manifest is not a mapping",
                              str(path), "provide a YAML mapping")
        mode = doc.get("data_mode")
        if mode == "SYNTHETIC" and not str(doc.get("watermark") or "").strip():
            raise IngestError("DATA_NOT_READY", "SYNTHETIC manifest lacks a watermark",
                              str(path), "set watermark (§15.4)")
        return doc

    # ------------------------------------------------------------ register
    def ensure_source(self, source_id: str, *, display_name: str,
                      domains: list[str], cost_model: str = "FREE",
                      integration_state: str = "TEST_PASSED",
                      pit_available: str = "NO", pit_basis: str = "UNKNOWN",
                      rights: dict[str, str] | None = None) -> None:
        now = _iso(datetime.now(timezone.utc))
        rights = rights or {}
        with write_tx(self.con):
            self.con.execute(
                "INSERT OR IGNORE INTO source_registry (source_id,display_name,cost_model,"
                "integration_state,created_at,updated_at) VALUES (?,?,?,?,?,?)",
                (source_id, display_name, cost_model, integration_state, now, now),
            )
            # 权利逐项登记；未提供即保持 UNKNOWN（§17.2 未知默认不开放）
            for field in ("research_use", "local_storage", "model_processing",
                          "excerpt_display", "third_party_redistribution", "commercial_use"):
                value = str(rights.get(field, "UNKNOWN")).upper()
                self.con.execute(
                    f"UPDATE source_registry SET {field}=?, updated_at=? WHERE source_id=?",
                    (value, now, source_id),
                )
            for domain in domains:
                self.con.execute(
                    "INSERT OR REPLACE INTO data_capability_card "
                    "(card_id,source_id,domain,pit_available,pit_basis,measured_at) "
                    "VALUES (?,?,?,?,?,?)",
                    (f"card-{source_id}-{domain}", source_id, domain,
                     pit_available, pit_basis, now),
                )

    def register_data_version(self, data_version: str, *, source_id: str, domains: list[str],
                              record_count: int, as_of_upper_bound: datetime,
                              content_hash: str | None = None) -> None:
        with write_tx(self.con):
            for domain in domains:
                self.con.execute(
                    "INSERT OR REPLACE INTO data_version (data_version,source_id,domain,"
                    "ingested_at,as_of_upper_bound,record_count,content_hash) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (f"{data_version}:{domain}", source_id, domain,
                     _iso(datetime.now(timezone.utc)), _iso(as_of_upper_bound),
                     record_count, content_hash),
                )

    # ------------------------------------------------------------ persist
    def ingest(self, doc: dict, *, source_id: str, data_version: str) -> IngestReport:
        """把清单写入交易性表（证券、日历、公司行为、事件）。"""

        now = _iso(datetime.now(timezone.utc))
        skipped: list[str] = []
        n_inst = n_days = n_quotes = n_ca = n_evt = 0

        with write_tx(self.con):
            # --- 证券 ---
            for inst in doc.get("instruments") or []:
                iid = str(inst.get("instrument_id") or "").strip()
                if not iid:
                    raise IngestError("DATA_NOT_READY", "instrument without instrument_id",
                                      "<unknown>", "assign a stable internal id (§15.2)")
                self.con.execute(
                    "INSERT OR IGNORE INTO instrument (instrument_id,exchange,board,"
                    "security_class,short_name,listed_on,delisted_on,created_at,updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (iid, inst.get("exchange", "OTHER"), inst.get("board", "OTHER"),
                     inst.get("security_class", "EQUITY"), inst.get("short_name"),
                     inst.get("listed_on"), inst.get("delisted_on"), now, now),
                )
                for ver in inst.get("status_history") or []:
                    self.con.execute(
                        "INSERT OR REPLACE INTO instrument_status_version "
                        "(instrument_id,valid_from,valid_to,name,status,industry_code,"
                        "industry_name,classification_version,source_id) "
                        "VALUES (?,?,?,?,?,?,?,?,?)",
                        (iid, ver.get("valid_from"), ver.get("valid_to"), ver.get("name"),
                         ver.get("status", "UNKNOWN"), ver.get("industry_code"),
                         ver.get("industry_name"), ver.get("classification_version"),
                         source_id),
                    )
                n_inst += 1

            # --- 交易日历 ---
            days = list(doc.get("trading_days") or [])
            for idx, day in enumerate(days):
                self.con.execute(
                    "INSERT OR REPLACE INTO trading_calendar (exchange,calendar_date,"
                    "is_trading_day,prev_trading_day,next_trading_day,source_id) "
                    "VALUES (?,?,1,?,?,?)",
                    ("SSE", day, days[idx - 1] if idx > 0 else None,
                     days[idx + 1] if idx + 1 < len(days) else None, source_id),
                )
                n_days += 1

            # --- 公司行为 ---
            for ca in doc.get("corporate_actions") or []:
                self.con.execute(
                    "INSERT OR REPLACE INTO corporate_action (action_id,instrument_id,"
                    "action_type,announced_on,record_date,ex_date,pay_date,"
                    "cash_per_share_micros,cash_per_share_cents,evidence_json,"
                    "source_url,source_title,bonus_ratio,"
                    "rights_price_cents,supported,source_id,notes) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (ca.get("action_id"), ca.get("instrument_id"), ca.get("action_type"),
                     ca.get("announced_on"), ca.get("record_date"), ca.get("ex_date"),
                     ca.get("pay_date"),
                     # 微元是权威单位；清单只给"分"时按 1 分 = 10000 微元换算。
                     # 两个都不给就是数据缺失，写 NULL 而不是 0——
                     # 0 会被读成"每股分红 0 元"，那是一个具体且错误的结论。
                     ca.get("cash_per_share_micros",
                            (ca["cash_per_share_cents"] * 10_000)
                            if ca.get("cash_per_share_cents") is not None else None),
                     ca.get("cash_per_share_cents"),
                     # 公告原文摘录：证据随快照一起冻结，事后可引用
                     json.dumps(ca["evidence"], ensure_ascii=False)
                     if isinstance(ca.get("evidence"), (dict, list))
                     else ca.get("evidence"),
                     ca.get("source_url"), ca.get("source_title"),
                     ca.get("bonus_ratio"), ca.get("rights_price_cents"),
                     1 if ca.get("supported") else 0, source_id,
                     # 清单里的 notes 可能是字符串（手写 YAML），
                     # 也可能是列表（公告解析器给出的说明）。统一成 JSON 文本：
                     # 直接绑 list 会抛 ProgrammingError，而那是数据形状问题，
                     # 不该在写库时才暴露。
                     json.dumps(ca["notes"], ensure_ascii=False)
                     if isinstance(ca.get("notes"), (list, dict))
                     else ca.get("notes")),
                )
                n_ca += 1

            # --- 事件与证据 ---
            for ev in doc.get("events") or []:
                eid = ev.get("event_id")
                self.con.execute(
                    "INSERT OR REPLACE INTO event (event_id,event_category,fact_summary,"
                    "event_time,event_time_precision,source_published_at,source_published_date,"
                    "first_seen_at,ingested_at,available_at,available_basis,pit_mode,"
                    "verification_status,market_direction,research_window_start,"
                    "research_window_end,created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (eid, ev.get("event_category", "OTHER"), ev.get("fact_summary", ""),
                     (ev.get("event_time") or {}).get("value") if isinstance(ev.get("event_time"), dict) else ev.get("event_time"),
                     ev.get("timestamp_precision", "UNKNOWN"), ev.get("source_published_at"),
                     ev.get("source_published_date"), ev.get("first_seen_at"), now,
                     ev.get("available_at"), ev.get("available_basis", "UNKNOWN"),
                     ev.get("pit_mode", "LIVE_OBSERVED"),
                     ev.get("verification_status", "UNVERIFIED"),
                     ev.get("market_direction", "UNKNOWN"),
                     (ev.get("research_window") or {}).get("start_date"),
                     (ev.get("research_window") or {}).get("end_date"), now),
                )
                for docu in ev.get("documents") or []:
                    self.con.execute(
                        "INSERT OR REPLACE INTO document (document_id,origin,url,is_original,"
                        "repost_of,fetched_at,source_published_date,timestamp_precision,"
                        "license_status,source_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (docu.get("document_id"), docu.get("origin", ""), docu.get("url"),
                         1 if docu.get("is_original") else 0, docu.get("repost_of"),
                         ev.get("first_seen_at") or now, ev.get("source_published_date"),
                         ev.get("timestamp_precision", "UNKNOWN"),
                         docu.get("license_status", "UNKNOWN"), source_id),
                    )
                    self.con.execute(
                        "INSERT OR REPLACE INTO event_document (event_id,document_id,relation) "
                        "VALUES (?,?,?)", (eid, docu.get("document_id"), "SUPPORTS"),
                    )
                for sub in ev.get("subjects") or []:
                    self.con.execute(
                        "INSERT OR REPLACE INTO event_subject (event_id,subject_type,"
                        "subject_id,role) VALUES (?,?,?,?)",
                        (eid, sub.get("subject_type", "OTHER"), sub.get("subject_id"),
                         sub.get("role", "PRIMARY")),
                    )
                for cit in ev.get("citations") or []:
                    loc = cit.get("locator") or {}
                    self.con.execute(
                        "INSERT OR REPLACE INTO citation (citation_id,event_id,document_id,"
                        "quote,locator_kind,locator_start,locator_end,locator_page,"
                        "locator_line,locator_section,located) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (f"cit-{eid}-{cit.get('document_id')}", eid, cit.get("document_id"),
                         cit.get("quote", ""), loc.get("kind", "EXACT_MATCH"),
                         loc.get("start"), loc.get("end"), loc.get("page"),
                         loc.get("line"), loc.get("section"), 1),
                    )
                n_evt += 1

        return IngestReport(
            data_version=data_version, instruments=n_inst, trading_days=n_days,
            quotes=n_quotes, corporate_actions=n_ca, events=n_evt,
            skipped=tuple(skipped),
        )

    # ------------------------------------------------------------ datasets
    def write_datasets(self, doc: dict, *, snapshot_id: str) -> list[dict]:
        """写出快照数据集文件，返回带真实哈希的引用描述。"""

        import hashlib

        out_dir = self.dataset_dir / snapshot_id
        out_dir.mkdir(parents=True, exist_ok=True)
        refs: list[dict] = []

        def emit(name: str, payload: Any, upper: str) -> None:
            # path 必须相对于 SnapshotStore 的 root，否则发布时的哈希校验找不到文件
            rel = f"datasets/{snapshot_id}/{name}.json"
            target = self.dataset_dir / snapshot_id / f"{name}.json"
            body = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                              indent=2).encode("utf-8")
            target.write_bytes(body)
            refs.append({
                "name": name,
                "path": rel,
                "sha256": "sha256:" + hashlib.sha256(body).hexdigest(),
                "record_count": len(payload) if isinstance(payload, list) else 1,
                "as_of_upper_bound": upper,
            })

        cutoff = doc.get("input_cutoff_at") or doc.get("as_of_time")
        if not cutoff:
            raise IngestError("DATA_NOT_READY", "manifest lacks input_cutoff_at",
                              snapshot_id, "set input_cutoff_at so datasets cannot exceed it")

        quotes = list(doc.get("daily_quotes") or [])
        # 派生事实随行情一起冻结进快照（见 mark_board_limit_up 的说明）
        mark_board_limit_up(doc)
        emit("daily_quotes", quotes, cutoff)
        emit("instruments", list(doc.get("instruments") or []), cutoff)
        emit("trading_calendar", list(doc.get("trading_days") or []), cutoff)
        emit("corporate_actions", list(doc.get("corporate_actions") or []), cutoff)
        emit("events", list(doc.get("events") or []), cutoff)
        return refs
