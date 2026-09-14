"""把真实公告正文抽成离线 fixture，供解析测试使用。

为什么要有这个脚本
------------------
解析测试必须离线：依赖网络的测试会在网络抖动时给出与代码无关的结论，
而这类测试的结论恰恰是"这笔分红能不能记账"。因此正文固化在仓库里，
需要更新时显式重跑本脚本。

用法：
    $env:AQUANT_TRUSTED_PROXY_NETWORKS='198.18.0.0/15,fdfe:dcba:9876::/48'
    python tools/refresh_cninfo_fixtures.py

它只做只读抓取：查公告列表 -> 下载 PDF -> 抽正文 -> 写入
tests/fixtures/cninfo/<日期>-<公告ID>.txt（首行是标题）。
"""

from __future__ import annotations

import io
import json
import re
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "tests" / "fixtures" / "cninfo"

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120 Safari/537.36"

#: 固定取样：一份"表格形态 + 非整数分金额"，一份"每 10 股"写法。
#: 取样写死而不是按日期滚动，是为了让 fixture 与测试断言一一对应。
TARGETS = [
    ("sse", "600519,gssh0600519", "2026-06-22/1225379934.PDF"),
    ("sse", "000001,gssz0000001", "2026-06-05/1225352449.PDF"),
]


def list_announcements(column: str, stock: str, se_date: str) -> list[dict]:
    body = urllib.parse.urlencode({
        "pageNum": 1, "pageSize": 40, "column": column, "tabName": "fulltext",
        "stock": stock, "seDate": se_date, "isHLtitle": "true"}).encode()
    req = urllib.request.Request(
        "http://www.cninfo.com.cn/new/hisAnnouncement/query", data=body,
        headers={"User-Agent": UA,
                 "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                 "Referer": "http://www.cninfo.com.cn/new/commonUrl?url=disclosure/list/notice",
                 "X-Requested-With": "XMLHttpRequest"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8")).get("announcements") or []


def main() -> int:
    try:
        from pypdf import PdfReader
    except ImportError:
        print("需要 pypdf：pip install pypdf")
        return 2

    OUT.mkdir(parents=True, exist_ok=True)
    written = 0
    for column, stock, suffix in TARGETS:
        found = [a for a in list_announcements(column, stock, "2025-01-01~2026-09-14")
                 if (a.get("adjunctUrl") or "").endswith(suffix)]
        if not found:
            print("未找到公告 " + suffix)
            return 1
        ann = found[0]
        url = "http://static.cninfo.com.cn/" + ann["adjunctUrl"]
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=60) as resp:
            pdf = resp.read()
        reader = PdfReader(io.BytesIO(pdf))
        text = "\n".join(page.extract_text() or "" for page in reader.pages)

        announcement_id = suffix.rsplit("/", 1)[-1].removesuffix(".PDF")
        announced = suffix.split("/")[0]
        path = OUT / (announced + "-" + announcement_id + ".txt")
        path.write_bytes(("# title: " + ann["announcementTitle"] + "\n" + text)
                         .encode("utf-8"))
        print("写入 " + path.name + "（" + str(len(text)) + " 字，"
              + str(len(pdf)) + " bytes PDF）")
        written += 1

    print("完成：" + str(written) + " 份 fixture")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
