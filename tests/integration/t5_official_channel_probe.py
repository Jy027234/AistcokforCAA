"""官方公告渠道探测（上交所 / 深交所 / 巨潮）。

目的（T4 结论）：免费行情源不提供历史时点，但交易所与巨潮的公告页面
**自带发布时间**，是本项目唯一能拿到 PARTIAL 级时点证据的来源。

因此这里要回答三个问题：
  1. 端点是否可达、是否要求特殊请求头；
  2. 返回体是否包含**发布时间**字段（这是时点证据的关键）；
  3. 是否需要签名/加密参数（巨潮新版接口有动态参数）。

只做少量请求，礼貌间隔。
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120 Safari/537.36")
OUT = Path(__file__).resolve().parent.parent.parent / "deploy" / "agentctl-q0" / "official-channel-probe.json"
PAUSE = 2.0


def get(url: str, *, referer: str = "", timeout: int = 20, attempts: int = 2):
    last = None
    for i in range(attempts):
        try:
            headers = {"User-Agent": UA, "Accept": "*/*"}
            if referer:
                headers["Referer"] = referer
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read(400_000)
                return resp.status, body, None
        except urllib.error.HTTPError as exc:
            return exc.code, b"", f"HTTP {exc.code}"
        except Exception as exc:  # noqa: BLE001
            last = f"{type(exc).__name__}: {exc}"
            time.sleep(1.5 * (i + 1))
    return None, b"", last


def probe_sse() -> dict:
    """上交所信息披露。页面为 HTML，需确认是否含发布时间。"""
    url = ("http://query.sse.com.cn/security/stock/queryCompanyBulletinNew.do?"
           "jsonCallBack=jsonpCallback&isPagination=true&productId=&securityType=0101%2C120100"
           "%2C020100%2C020200%2C120200&reportType2=DQGG&reportType=ALL&beginDate=2026-09-01"
           "&endDate=2026-09-12&pageHelp.pageSize=5&pageHelp.pageNo=1&pageHelp.beginPage=1"
           "&pageHelp.cacheSize=1&pageHelp.endPage=1")
    status, body, err = get(url, referer="http://www.sse.com.cn/")
    text = body.decode("utf-8", errors="replace")
    out = {"endpoint": "sse-query", "status": status, "error": err, "bytes": len(body)}
    out["has_publish_field"] = any(k in text for k in
                                   ("SSEDATE", "publishDate", "PUBLISHDATE", "bulletinDate"))
    out["sample"] = text[:400]
    return out


def probe_szse() -> dict:
    """深交所公告查询。"""
    url = ("http://www.szse.cn/api/disc/announcement/annList?"
           "random=0.123&secCode=&channelCode=listedNotice_disc&pageSize=5&pageNum=1")
    status, body, err = get(url, referer="http://www.szse.cn/disclosure/listed/notice/index.html")
    text = body.decode("utf-8", errors="replace")
    out = {"endpoint": "szse-annList", "status": status, "error": err, "bytes": len(body)}
    try:
        doc = json.loads(text)
        data = doc.get("data") or []
        out["count"] = len(data)
        if data:
            out["keys"] = sorted(data[0].keys())
            # 发布时间字段是时点证据的关键
            out["time_fields"] = {k: data[0].get(k) for k in data[0]
                                  if "time" in k.lower() or "date" in k.lower()}
    except json.JSONDecodeError:
        out["sample"] = text[:300]
    return out


def probe_cninfo() -> dict:
    """巨潮资讯网公告查询。新版接口可能需要动态参数。"""
    url = ("http://www.cninfo.com.cn/new/hisAnnouncement/query")
    data = ("pageNum=1&pageSize=5&column=szse&tabName=fulltext&plate=&stock=&searchkey="
            "&secid=&category=&trade=&seDate=2026-09-01~2026-09-12&sortName=&sortType="
            "&isHLtitle=true").encode()
    try:
        req = urllib.request.Request(url, data=data, headers={
            "User-Agent": UA,
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Referer": "http://www.cninfo.com.cn/new/commonUrl?url=disclosure/list/notice",
            "X-Requested-With": "XMLHttpRequest",
        })
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = resp.read(400_000)
            status = resp.status
        text = body.decode("utf-8", errors="replace")
        out = {"endpoint": "cninfo-hisAnnouncement", "status": status, "bytes": len(body)}
        try:
            doc = json.loads(text)
            anns = doc.get("announcements") or []
            out["count"] = len(anns)
            if anns:
                out["keys"] = sorted(anns[0].keys())
                out["time_fields"] = {k: anns[0].get(k) for k in anns[0]
                                      if "time" in k.lower() or "date" in k.lower()}
        except json.JSONDecodeError:
            out["sample"] = text[:300]
        return out
    except Exception as exc:  # noqa: BLE001
        return {"endpoint": "cninfo-hisAnnouncement", "error": f"{type(exc).__name__}: {exc}"}


def main() -> int:
    result = {"probed_at": datetime.now(timezone.utc).isoformat()}
    for name, fn in (("sse", probe_sse), ("szse", probe_szse), ("cninfo", probe_cninfo)):
        print(f"== {name} ==")
        try:
            r = fn()
        except Exception as exc:  # noqa: BLE001
            r = {"error": f"{type(exc).__name__}: {exc}"}
        result[name] = r
        print(json.dumps(r, ensure_ascii=False)[:600])
        print()
        time.sleep(PAUSE)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
