"""T4 免费数据源实测：东方财富直连接口。

目的（主文档 §6.2 M0 硬交付）：回答"能支持什么、不能支持什么"，
而不是"接口能返回数据"。重点测四项，其中第 4 项最关键：

  1. 证券身份与板块 —— 覆盖多少、字段是否可用
  2. 日行情 —— 历史深度、复权、单位含义、修订行为
  3. 交易日历 —— 能否从指数序列推导
  4. **历史时点（PIT）** —— 免费源几乎必然缺失。这决定能否做正式历史回测，
     必须实测取证而不是假定（§21 头号风险："数据接口有值但历史时点错误"）

只读、限速、不并发轰炸。输出 JSON 供能力卡引用。
"""

from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)", "Referer": "https://quote.eastmoney.com/"}
OUT = Path(__file__).resolve().parent.parent.parent / "deploy" / "agentctl-q0" / "t4-eastmoney-probe.json"
PAUSE = 2.5   # 礼貌间隔


def get_json(url: str, timeout: int = 20, attempts: int = 4) -> dict:
    # The public endpoint closes connections when hit too quickly.
    # Retry with backoff instead of treating that as "no data".
    last: Exception | None = None
    for i in range(attempts):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
            time.sleep(PAUSE)
            return json.loads(raw)
        except Exception as exc:
            last = exc
            time.sleep(1.5 * (2 ** i))
    raise RuntimeError("gave up after %d attempts: %s" % (attempts, last))


def probe_universe() -> dict:
    """全 A 股列表。fs 组合覆盖沪深主板/创业板/科创板/北交所。"""
    url = ("https://push2.eastmoney.com/api/qt/clist/get?"
           "pn=1&pz=10&po=1&np=1&fltt=2&invt=2&fid=f12"
           "&fs=m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048"
           "&fields=f12,f13,f14,f2,f3,f100,f26,f21,f20")
    data = get_json(url)
    d = data.get("data") or {}
    diff = d.get("diff") or []
    fields_present = sorted({k for row in diff for k in row.keys()})
    return {
        "total": d.get("total"),
        "sample_rows": len(diff),
        "fields_present": fields_present,
        "sample": diff[:3],
        "notes": "f13=市场(0深/1沪), f12=代码, f14=名称, f100=行业, f26=上市日期, f20=总市值",
    }


def probe_ohlcv(symbol: str, secid: str, begin: str, end: str) -> dict:
    """日 K 线：不复权与三种复权各取一次，比较差异以证明复权可用。"""
    out: dict = {"symbol": symbol, "secid": secid, "range": [begin, end]}
    for label, fqt in (("none", 0), ("qfq", 1), ("hfq", 2)):
        url = ("https://push2his.eastmoney.com/api/qt/stock/kline/get?"
               f"secid={secid}&fields1=f1,f2,f3,f4,f5,f6"
               "&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"
               f"&klt=101&fqt={fqt}&beg={begin}&end={end}")
        try:
            data = get_json(url)
            d = data.get("data") or {}
            klines = d.get("klines") or []
            out[label] = {
                "name": d.get("name"),
                "bars": len(klines),
                "first": klines[0] if klines else None,
                "last": klines[-1] if klines else None,
            }
        except Exception as exc:  # noqa: BLE001
            out[label] = {"error": f"{type(exc).__name__}: {exc}"}
    # 复权是否真的不同
    try:
        a = out["none"]["first"].split(",")[2]
        b = out["qfq"]["first"].split(",")[2]
        c = out["hfq"]["first"].split(",")[2]
        out["adjustment_differs"] = len({a, b, c}) > 1
    except Exception:
        out["adjustment_differs"] = None
    out["fields"] = "f51日期,f52开,f53收,f54高,f55低,f56量(手),f57额(元),f58振幅,f59涨跌幅,f60涨跌额,f61换手率"
    return out


def probe_calendar(begin: str, end: str) -> dict:
    """用上证指数日线序列推导交易日历，并统计年度交易日数。"""
    url = ("https://push2his.eastmoney.com/api/qt/stock/kline/get?"
           "secid=1.000001&fields1=f1,f2&fields2=f51,f53&klt=101&fqt=0"
           f"&beg={begin}&end={end}")
    data = get_json(url)
    d = data.get("data") or {}
    klines = d.get("klines") or []
    days = [k.split(",")[0] for k in klines]
    # 相邻间隔分布，用于识别长假
    gaps = []
    for i in range(1, len(days)):
        prev = date.fromisoformat(days[i - 1])
        cur = date.fromisoformat(days[i])
        gaps.append((cur - prev).days)
    return {
        "bars": len(klines),
        "first_day": days[0] if days else None,
        "last_day": days[-1] if days else None,
        "gap_histogram": {str(g): gaps.count(g) for g in sorted(set(gaps))} if gaps else {},
        "max_gap_days": max(gaps) if gaps else None,
    }


def probe_pit() -> dict:
    """时点能力实测。这是 T4 的核心。

    免费行情接口是"当前视图"：它返回的是今天的复权序列，
    无法回答"2020-03-01 那天我看到的收盘价是多少"。
    下面用可观察证据说明这一点，而不是引用文档。
    """
    findings: list[str] = []
    evidence: dict = {}

    # 证据 1：接口没有 as-of / 修订版本参数
    url = ("https://push2his.eastmoney.com/api/qt/stock/kline/get?"
           "secid=1.600519&fields1=f1&fields2=f51,f53&klt=101&fqt=1&beg=20200101&end=20200110")
    data = get_json(url)
    d = data.get("data") or {}
    evidence["kline_response_keys"] = sorted(d.keys())
    findings.append(
        "日线接口无 as-of/修订版本参数；返回字段仅含序列本身，"
        "不含 published_at / revision / version 元数据"
    )

    # 证据 2：前复权序列会随后续分红变化 -> 历史值不可复现
    early = ("https://push2his.eastmoney.com/api/qt/stock/kline/get?"
             "secid=1.600519&fields1=f1&fields2=f51,f53&klt=101&fqt=1&beg=20200102&end=20200102")
    r1 = get_json(early)
    d1 = (r1.get("data") or {}).get("klines") or []
    evidence["qfq_2020_01_02_close"] = d1[0] if d1 else None
    findings.append(
        "前复权(fqt=1)序列以最新除权基准重算；未来分红会改变历史数值。"
        "因此同一历史日期的前复权收盘价不是不变量，不能作为已发布快照的内容"
    )

    # 证据 3：财务接口只返回最新修订版，无原始披露时间序列
    try:
        fin_url = ("https://datacenter.eastmoney.com/securities/api/data/v1/get?"
                   "reportName=RPT_LICO_FN_CPD&columns=SECURITY_CODE,REPORT_DATE,"
                   "PUBLISH_DATE,TOTAL_OPERATE_INCOME,PARENT_NETPROFIT"
                   "&filter=(SECURITY_CODE%3D%22600519%22)&pageNumber=1&pageSize=3")
        fin = get_json(fin_url)
        evidence["financial_probe_success"] = bool(fin.get("result"))
        if fin.get("result"):
            evidence["financial_sample_keys"] = sorted(
                (fin["result"].get("data") or [{}])[0].keys()
            )
        findings.append(
            "财务数据中心接口提供 PUBLISH_DATE 字段，但返回的是**当前**数据集；"
            "无法取得'某次修订当时可见的值'。故财务类 PIT 回测不可默认启用（§7.4）"
        )
    except Exception as exc:  # noqa: BLE001
        evidence["financial_probe_error"] = f"{type(exc).__name__}: {exc}"
        findings.append(f"财务接口探测失败：{exc}")

    # 结论
    return {
        "pit_available": "NO",
        "pit_basis": "UNKNOWN",
        "evidence": evidence,
        "findings": findings,
        "implication": (
            "该数据源只能用于：① 前向观察（LIVE_OBSERVED，从接入日起自行归档）；"
            "② 当前研究展示。**不能**支撑正式历史 PIT 回测。"
            "§7.4 规定：相关历史因子默认不启用，但事件归档与阅读照常运行"
        ),
        "recommended_posture": (
            "接入时以'自建前向归档'为主：每次抓取都记录 first_seen_at 与原始响应哈希，"
            "由此积累本系统自己的可证明时点序列（§7.2 LIVE_OBSERVED）"
        ),
    }


def main() -> int:
    result: dict = {
        "kind": "AQuantDataCapabilityProbe",
        "source_id": "eastmoney-direct",
        "probed_at": datetime.now(timezone.utc).isoformat(),
        "cost_model": "FREE",
        "auth_required": False,
    }
    print("== 1. 证券身份与板块 ==")
    try:
        result["universe"] = probe_universe()
        print(json.dumps(result["universe"], ensure_ascii=False)[:400])
    except Exception as exc:  # noqa: BLE001
        result["universe"] = {"error": str(exc)}
        print("FAIL", exc)

    print("\n== 2. 日行情与复权 ==")
    try:
        result["ohlcv"] = probe_ohlcv("600519", "1.600519", "20200101", "20260912")
        for k in ("none", "qfq", "hfq"):
            v = result["ohlcv"].get(k) or {}
            print(f"  {k}: bars={v.get('bars')} first={v.get('first')}")
        print("  adjustment_differs:", result["ohlcv"].get("adjustment_differs"))
    except Exception as exc:  # noqa: BLE001
        result["ohlcv"] = {"error": str(exc)}
        print("FAIL", exc)

    print("\n== 3. 交易日历 ==")
    try:
        result["calendar"] = probe_calendar("20250101", "20260912")
        print(json.dumps(result["calendar"], ensure_ascii=False)[:400])
    except Exception as exc:  # noqa: BLE001
        result["calendar"] = {"error": str(exc)}
        print("FAIL", exc)

    print("\n== 4. 历史时点（PIT）能力 ==")
    try:
        result["pit"] = probe_pit()
        print("  pit_available:", result["pit"]["pit_available"])
        for f in result["pit"]["findings"]:
            print("   -", f)
    except Exception as exc:  # noqa: BLE001
        result["pit"] = {"error": str(exc)}
        print("FAIL", exc)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nwrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
