"""告警：让流水线的失败能被看见（§14.2）。

要解决的问题
------------
每日流水线会写运行留痕并返回退出码，但**没人主动去看留痕**。
定时任务最常见的失败模式不是崩溃，而是**静默地什么也没做**——
市场休市、数据源没更新、磁盘满了。等发现时已经过去几天。

三件必须成立的事
----------------
1. **先落盘，再外发**。外发（webhook / 邮件 / IM）会失败——网络、
   凭据、对方挂了。如果先外发再记录，外发一失败告警本身就没了。
   因此顺序是固定的：写本地 JSONL -> 尝试外发 -> 外发结果也记下来。
2. **外发失败不等于流程失败**。告警通道坏了不该让流水线跟着红；
   但"外发失败"这件事本身要留下记录，否则会以为告警已经发出去了。
3. **不引入消息中间件**（§14.2）。只有一个可选的 webhook URL，
   没有 URL 就只落盘——这是刻意的：告警通道的选择是运维侧的事，
   本项目不替使用者决定用哪家 IM。
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

#: 告警级别。刻意只有两档：需要人看的（ERROR）与需要人知道的（WARNING）。
#: 更多档位会变成"这个到底要不要管"的讨论，而告警的价值在于明确。
LEVEL_ERROR = "ERROR"
LEVEL_WARNING = "WARNING"
_LEVELS = (LEVEL_ERROR, LEVEL_WARNING)


@dataclass(slots=True)
class Alert:
    level: str
    source: str
    message: str
    detail: dict = field(default_factory=dict)
    raised_at: str = ""

    def __post_init__(self) -> None:
        if self.level not in _LEVELS:
            raise ValueError(f"unknown alert level {self.level!r}; use {_LEVELS}")
        if not self.raised_at:
            self.raised_at = datetime.now(timezone.utc).isoformat()

    def as_dict(self) -> dict:
        return asdict(self)


class AlertLog:
    """告警落盘。**先于任何外发动作**执行。"""

    def __init__(self, path: Path) -> None:
        self.path = path

    def write(self, alert: Alert) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(alert.as_dict(), ensure_ascii=False) + "\n")

    def recent(self, limit: int = 50) -> list[dict]:
        if not self.path.exists():
            return []
        lines = self.path.read_text(encoding="utf-8").splitlines()
        return [json.loads(x) for x in lines[-limit:] if x.strip()]

    def errors_since(self, since_day: str) -> list[dict]:
        """某天以来的 ERROR。用来回答"最近有没有需要人管的事"。"""

        return [a for a in self.recent(limit=1000)
                if a.get("level") == LEVEL_ERROR
                and (a.get("raised_at") or "")[:10] >= since_day]


def raise_alert(log: AlertLog, *, level: str, source: str, message: str,
                detail: dict | None = None, webhook: str | None = None,
                timeout: float = 10.0) -> dict:
    """记一条告警。返回 {alerted, delivered, deliveryError}。

    顺序不可调换：**落盘 -> 外发 -> 记录外发结果**。
    先外发再落盘的话，外发一失败告警就丢了——而那正是最需要它的时候。
    """

    alert = Alert(level=level, source=source, message=message,
                  detail=detail or {})
    log.write(alert)

    url = webhook if webhook is not None else os.environ.get("AQUANT_ALERT_WEBHOOK", "")
    if not url.strip():
        # 没有配置通道：只落盘。这是**正常状态**，不是错误——
        # 但返回体里 delivered=False，调用方不该以为告警已经发出去了。
        return {"alerted": True, "delivered": False,
                "deliveryError": "未配置 AQUANT_ALERT_WEBHOOK，仅落盘"}

    payload = json.dumps({"text": f"[{level}] {source}: {message}\n"
                                  + json.dumps(detail or {}, ensure_ascii=False)},
                         ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url, data=payload, method="POST",
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            ok = 200 <= resp.status < 300
        error = None if ok else f"HTTP {resp.status}"
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        ok, error = False, f"{type(exc).__name__}: {exc}"

    if error:
        # 外发失败**也记一条**，且级别降为 WARNING：
        # 流水线本身没坏，坏的是通知渠道。不记的话，
        # 下一次会被误以为"告警已经发出去了"。
        log.write(Alert(level=LEVEL_WARNING, source="alerting",
                        message="告警外发失败", detail={"error": error,
                                                       "original": alert.message}))
    return {"alerted": True, "delivered": ok, "deliveryError": error}
