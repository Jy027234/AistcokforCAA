"""运行 CNINFO 报告期公告受限探针。

示例（只输出标题、时间戳、ID/URL 和去敏审计元数据）：

    $env:AQUANT_TRUSTED_PROXY_NETWORKS='198.18.0.0/15,fdfe:dcba:9876::/48'
    python tools/probe_cninfo_reports.py --code 600519 --period 2024-12-31

``--output`` 若指定，只写同一份去原文 JSON；本工具不会下载公告 PDF，
也不会保存 CNINFO 原始响应或财务正文。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aquant.adapters.providers.cninfo_probe import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
