"""前向归档：本系统自己产生可证明的时点数据。

为什么这是必需的（T4 结论 + 主文档 §7.2/§21）：
   东方财富等免费源不提供历史时点版本，且前复权序列会随后续分红重算。
   因此**没有任何免费源能提供"当时看到的值"**。
   唯一可行的路径是从接入第一天起自行归档：每次抓取都记录原始响应、
   抓取时刻（first_seen_at）与内容哈希，由此积累 :class:`PitMode.LIVE_OBSERVED`
   记录——这类记录的可用时点是我们自己观察到的，可证明，不依赖供应商。

关键设计：
  * 归档**先于解析**。原始字节落盘后才有解析，解析失败不影响证据。
  * 每次抓取产生一条 `fetch_receipt`：URL、抓取时刻、状态、内容哈希、字节数。
  * 同一内容重复抓取不重复存字节（按哈希去重），但**仍然记录新的抓取时刻**——
    这正是"同一事实在不同时刻都被观察到"的证据。
  * `first_seen_at` 是归档时刻，绝不回填为过去（§7.2）。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ...domain.data.db import write_tx

ARCHIVE_SCHEMA = """
CREATE TABLE IF NOT EXISTS raw_artifact (
    content_hash    TEXT PRIMARY KEY,
    byte_size       INTEGER NOT NULL CHECK (byte_size >= 0),
    media_type      TEXT,
    stored_path     TEXT NOT NULL,
    first_stored_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS fetch_receipt (
    receipt_id      TEXT PRIMARY KEY,
    source_id       TEXT NOT NULL,
    url             TEXT NOT NULL,
    requested_at    TEXT NOT NULL,
    responded_at    TEXT NOT NULL,
    http_status     INTEGER,
    outcome         TEXT NOT NULL CHECK (outcome IN
                      ('OK','HTTP_ERROR','DENIED','TIMEOUT','TRANSPORT_ERROR','TOO_LARGE')),
    content_hash    TEXT REFERENCES raw_artifact(content_hash),
    byte_size       INTEGER,
    detail          TEXT,
    -- §7.2 本系统首次观察到的时刻。这是可证明时点的来源，不得伪造到过去。
    first_seen_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_receipt_source_time
    ON fetch_receipt (source_id, first_seen_at);
CREATE INDEX IF NOT EXISTS idx_receipt_hash ON fetch_receipt (content_hash);
"""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        raise ValueError("archive timestamps must be timezone-aware")
    return dt.astimezone(timezone.utc).isoformat()


@dataclass(frozen=True, slots=True)
class FetchReceipt:
    receipt_id: str
    source_id: str
    url: str
    requested_at: datetime
    responded_at: datetime
    outcome: str
    first_seen_at: datetime
    http_status: int | None = None
    content_hash: str | None = None
    byte_size: int | None = None
    detail: str | None = None


class ForwardArchive:
    """原始响应归档。数据库只存索引，字节落盘。"""

    def __init__(self, con: sqlite3.Connection, root: str | Path) -> None:
        self.con = con
        self.root = Path(root)
        self.blob_dir = self.root / "raw"
        self.blob_dir.mkdir(parents=True, exist_ok=True)
        con.executescript(ARCHIVE_SCHEMA)

    # ------------------------------------------------------------ store
    def store_bytes(self, payload: bytes, *, media_type: str | None = None) -> tuple[str, bool]:
        """按内容哈希去重存字节。返回 (content_hash, is_new)。"""

        digest = "sha256:" + hashlib.sha256(payload).hexdigest()
        existing = self.con.execute(
            "SELECT content_hash FROM raw_artifact WHERE content_hash=?", (digest,)
        ).fetchone()
        if existing:
            return digest, False

        rel = f"{digest.removeprefix('sha256:')[:2]}/{digest.removeprefix('sha256:')}"
        target = self.blob_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        # 先写临时文件再原子改名：避免半截文件被当成完整证据
        tmp = target.with_suffix(".tmp")
        tmp.write_bytes(payload)
        tmp.replace(target)

        with write_tx(self.con):
            self.con.execute(
                "INSERT INTO raw_artifact (content_hash,byte_size,media_type,stored_path,"
                "first_stored_at) VALUES (?,?,?,?,?)",
                (digest, len(payload), media_type, str(target.relative_to(self.root)), _iso(_now())),
            )
        return digest, True

    def record(
        self,
        *,
        source_id: str,
        url: str,
        outcome: str,
        requested_at: datetime,
        responded_at: datetime | None = None,
        http_status: int | None = None,
        content_hash: str | None = None,
        byte_size: int | None = None,
        detail: str | None = None,
    ) -> FetchReceipt:
        """记录一次抓取。

        **失败也要记录**：拒绝、超时、HTTP 错误同样是证据
        （主文档 §6.1：公告"保留索引和失败原因"）。
        """

        responded = responded_at or _now()
        receipt_id = "rcpt_" + hashlib.sha256(
            f"{source_id}|{url}|{_iso(requested_at)}|{outcome}".encode()
        ).hexdigest()[:24]
        first_seen = responded  # 归档时刻即首次观察时刻，绝不回填

        with write_tx(self.con):
            self.con.execute(
                "INSERT OR REPLACE INTO fetch_receipt (receipt_id,source_id,url,requested_at,"
                "responded_at,http_status,outcome,content_hash,byte_size,detail,first_seen_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (receipt_id, source_id, url, _iso(requested_at), _iso(responded),
                 http_status, outcome, content_hash, byte_size, detail, _iso(first_seen)),
            )
        return FetchReceipt(
            receipt_id=receipt_id, source_id=source_id, url=url,
            requested_at=requested_at, responded_at=responded, outcome=outcome,
            first_seen_at=first_seen, http_status=http_status,
            content_hash=content_hash, byte_size=byte_size, detail=detail,
        )

    # ------------------------------------------------------------ read
    def load_bytes(self, content_hash: str) -> bytes:
        row = self.con.execute(
            "SELECT stored_path FROM raw_artifact WHERE content_hash=?", (content_hash,)
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown content hash {content_hash!r}")
        return (self.root / row["stored_path"]).read_bytes()

    def verify(self, content_hash: str) -> bool:
        """重算哈希，确认归档内容未被替换。"""

        try:
            payload = self.load_bytes(content_hash)
        except KeyError:
            return False
        actual = "sha256:" + hashlib.sha256(payload).hexdigest()
        return actual == content_hash

    def receipts_for(self, source_id: str) -> list[dict]:
        rows = self.con.execute(
            "SELECT * FROM fetch_receipt WHERE source_id=? ORDER BY first_seen_at",
            (source_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def observation_history(self, url: str) -> list[dict]:
        """某 URL 的全部观察记录。

        用途：同一内容在多个时刻被观察到 -> 可以证明"这个值至少从
        first_seen_at 起就可见"，这正是 PIT 判定所需要的。
        """

        rows = self.con.execute(
            "SELECT first_seen_at, content_hash, outcome, http_status FROM fetch_receipt "
            "WHERE url=? ORDER BY first_seen_at",
            (url,),
        ).fetchall()
        return [dict(r) for r in rows]

    def distinct_content_count(self, source_id: str) -> int:
        row = self.con.execute(
            "SELECT COUNT(DISTINCT content_hash) AS n FROM fetch_receipt "
            "WHERE source_id=? AND content_hash IS NOT NULL",
            (source_id,),
        ).fetchone()
        return int(row["n"] if row else 0)
