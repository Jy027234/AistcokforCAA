"""快照发布与读取。

主文档 §8.3、§15.4、§20 ADR-009。快照是研究的唯一输入基准。

本模块强制四件事，任何一件不成立就拒绝：
  1. **不可变**：PUBLISHED 之后不可修改；修正必须新 ID + supersedes。
  2. **哈希可验证**：读取时重算数据集哈希，不匹配即拒绝。
  3. **时点上界**：数据集 as_of_upper_bound 不得晚于 input_cutoff_at。
  4. **模式不混用**：SYNTHETIC 不得与 PRODUCTION 混成同一结论。

这四条都有对应的拒绝测试，而不是只写在文档里。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

from .db import write_tx


class DataMode(str, Enum):
    PRODUCTION = "PRODUCTION"
    SYNTHETIC = "SYNTHETIC"


class SnapshotStatus(str, Enum):
    DRAFT = "DRAFT"
    VALIDATING = "VALIDATING"
    PUBLISHED = "PUBLISHED"
    SUPERSEDED = "SUPERSEDED"
    REJECTED = "REJECTED"


class SnapshotError(Exception):
    def __init__(self, code: str, message: str, object_id: str, repair_action: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.object_id = object_id
        self.repair_action = repair_action

    def as_error(self) -> dict:
        return {
            "code": self.code,
            "message": self.message,
            "object_id": self.object_id,
            "retryable": False,
            "repair_action": self.repair_action,
        }


@dataclass(frozen=True, slots=True)
class DatasetRef:
    name: str
    path: str
    sha256: str
    record_count: int
    as_of_upper_bound: datetime
    coverage_ratio: float | None = None

    def recompute_sha256(self, root: Path) -> str:
        target = root / self.path
        h = hashlib.sha256()
        with target.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        return "sha256:" + h.hexdigest()


@dataclass(slots=True)
class SnapshotDraft:
    snapshot_id: str
    kind: str
    data_mode: DataMode
    input_cutoff_at: datetime
    created_at: datetime
    code_version: str
    data_version: str
    as_of_time: datetime | None = None
    published_at: datetime | None = None
    parent_snapshot_id: str | None = None
    supersedes: str | None = None
    watermark: str | None = None
    strategy_version: str | None = None
    feature_version: str | None = None
    rule_version: str | None = None
    fee_version: str | None = None
    pool_hash: str | None = None
    datasets: list[DatasetRef] = field(default_factory=list)
    blocking_issues: list[dict] = field(default_factory=list)


def _require_aware(dt: datetime, field_name: str) -> None:
    """§7.1 存储统一 UTC 且必须带时区；朴素时间一律拒绝。"""

    if not isinstance(dt, datetime):
        raise ValueError(f"{field_name} must be a datetime with a timezone")
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError(
            f"{field_name} must be timezone-aware; store UTC and convert at the edges (§7.1)"
        )


def _iso(dt: datetime) -> str:
    _require_aware(dt, "timestamp")
    return dt.astimezone(timezone.utc).isoformat()


def _parse(text: str) -> datetime:
    return datetime.fromisoformat(text)


class SnapshotStore:
    def __init__(self, con: sqlite3.Connection, root: str | Path) -> None:
        self.con = con
        self.root = Path(root)

    # ------------------------------------------------------------ publish
    def publish(self, draft: SnapshotDraft) -> str:
        """校验并发布。任一条不成立即拒绝，且不写入。"""

        self._validate_draft(draft)

        if draft.data_mode is DataMode.SYNTHETIC and not (draft.watermark or "").strip():
            raise SnapshotError(
                "DATA_NOT_READY",
                "SYNTHETIC snapshot requires a watermark (§15.4)",
                draft.snapshot_id,
                "set watermark so consumers can never mistake synthetic for real data",
            )

        if draft.supersedes:
            prior = self._find(draft.supersedes)
            if prior is None:
                raise SnapshotError(
                    "DATA_NOT_READY",
                    f"cannot supersede unknown snapshot {draft.supersedes!r}",
                    draft.snapshot_id,
                    "supersede an existing PUBLISHED snapshot, or drop the supersedes field",
                )
            if prior["status"] not in (SnapshotStatus.PUBLISHED.value,):
                raise SnapshotError(
                    "STALE_SNAPSHOT",
                    f"cannot supersede {draft.supersedes!r}: it is not PUBLISHED",
                    draft.snapshot_id,
                    "supersede only a published snapshot",
                )

        quality_status = "BLOCKING" if draft.blocking_issues else "OK"
        published_at = draft.published_at or datetime.now(timezone.utc)

        with write_tx(self.con):
            self.con.execute(
                "INSERT INTO snapshot (snapshot_id,kind,data_mode,status,input_cutoff_at,"
                "as_of_time,published_at,created_at,parent_snapshot_id,supersedes,watermark,"
                "code_version,data_version,strategy_version,feature_version,rule_version,"
                "fee_version,quality_status,pool_hash) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    draft.snapshot_id, draft.kind, draft.data_mode.value,
                    SnapshotStatus.PUBLISHED.value, _iso(draft.input_cutoff_at),
                    _iso(draft.as_of_time) if draft.as_of_time else None,
                    _iso(published_at), _iso(draft.created_at),
                    draft.parent_snapshot_id, draft.supersedes, draft.watermark,
                    draft.code_version, draft.data_version, draft.strategy_version,
                    draft.feature_version, draft.rule_version, draft.fee_version,
                    quality_status, draft.pool_hash,
                ),
            )
            for d in draft.datasets:
                self.con.execute(
                    "INSERT INTO snapshot_dataset (snapshot_id,name,path,sha256,record_count,"
                    "as_of_upper_bound,coverage_ratio) VALUES (?,?,?,?,?,?,?)",
                    (draft.snapshot_id, d.name, d.path, d.sha256, d.record_count,
                     _iso(d.as_of_upper_bound), d.coverage_ratio),
                )
            for i, issue in enumerate(draft.blocking_issues):
                self.con.execute(
                    "INSERT INTO snapshot_blocking_issue (snapshot_id,issue_seq,error_code,"
                    "message,object_id,retryable,repair_action) VALUES (?,?,?,?,?,?,?)",
                    (draft.snapshot_id, i, issue.get("code", "DATA_NOT_READY"),
                     issue.get("message", ""), issue.get("object_id"),
                     1 if issue.get("retryable") else 0,
                     issue.get("repair_action", "")),
                )
            if draft.supersedes:
                self.con.execute(
                    "UPDATE snapshot SET status=? WHERE snapshot_id=?",
                    (SnapshotStatus.SUPERSEDED.value, draft.supersedes),
                )
        return draft.snapshot_id

    def _validate_draft(self, draft: SnapshotDraft) -> None:
        if not draft.datasets:
            raise SnapshotError(
                "DATA_NOT_READY", "snapshot has no datasets", draft.snapshot_id,
                "publish requires at least one dataset with a verifiable hash",
            )
        # §7.1 禁止朴素时间：先在所有入口做一次检查，避免在比较处才炸出
        # 难以理解的 TypeError（也不允许"看起来能用"的隐式本地时区假设）。
        for field_name in ("input_cutoff_at", "created_at", "as_of_time"):
            value = getattr(draft, field_name, None)
            if value is not None:
                _require_aware(value, field_name)
        for d in draft.datasets:
            _require_aware(d.as_of_upper_bound, f"dataset {d.name}.as_of_upper_bound")

        existing = self.con.execute(
            "SELECT snapshot_id FROM snapshot WHERE snapshot_id=?", (draft.snapshot_id,)
        ).fetchone()
        if existing:
            raise SnapshotError(
                "DATA_NOT_READY",
                f"snapshot_id {draft.snapshot_id!r} already exists; ids are immutable",
                draft.snapshot_id,
                "publish under a new snapshot_id and set supersedes if this is a correction",
            )
        cutoff = draft.input_cutoff_at
        for d in draft.datasets:
            if d.as_of_upper_bound > cutoff:
                raise SnapshotError(
                    "STALE_SNAPSHOT",
                    f"dataset {d.name!r} as_of_upper_bound {_iso(d.as_of_upper_bound)} "
                    f"is later than input_cutoff_at {_iso(cutoff)}",
                    draft.snapshot_id,
                    "exclude late-arriving records or raise the cutoff and re-derive the snapshot",
                )
            actual = d.recompute_sha256(self.root)
            if actual != d.sha256:
                raise SnapshotError(
                    "DATA_NOT_READY",
                    f"dataset {d.name!r} hash mismatch: declared {d.sha256}, actual {actual}",
                    draft.snapshot_id,
                    "recompute the dataset hash; do not publish unverified content",
                )

    # ------------------------------------------------------------ read
    def _find(self, snapshot_id: str) -> dict | None:
        row = self.con.execute(
            "SELECT * FROM snapshot WHERE snapshot_id=?", (snapshot_id,)
        ).fetchone()
        return dict(row) if row is not None else None

    def get(self, snapshot_id: str) -> dict:
        found = self._find(snapshot_id)
        if found is None:
            raise SnapshotError(
                "DATA_NOT_READY", f"unknown snapshot {snapshot_id!r}", snapshot_id,
                "list published snapshots and use an existing id",
            )
        return found

    def require_published(self, snapshot_id: str) -> dict:
        """§15.4 消费者只读 PUBLISHED。"""

        snap = self.get(snapshot_id)
        if snap["status"] != SnapshotStatus.PUBLISHED.value:
            raise SnapshotError(
                "STALE_SNAPSHOT",
                f"snapshot {snapshot_id!r} is {snap['status']}, not PUBLISHED",
                snapshot_id,
                "resolve a published snapshot; draft or rejected snapshots are not research inputs",
            )
        return snap

    def datasets(self, snapshot_id: str) -> list[dict]:
        self.require_published(snapshot_id)
        rows = self.con.execute(
            "SELECT * FROM snapshot_dataset WHERE snapshot_id=? ORDER BY name", (snapshot_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    def verify_dataset(self, snapshot_id: str, name: str) -> str:
        """读取时重算哈希，防止已发布内容被替换。"""

        for d in self.datasets(snapshot_id):
            if d["name"] == name:
                ref = DatasetRef(
                    name=d["name"], path=d["path"], sha256=d["sha256"],
                    record_count=d["record_count"],
                    as_of_upper_bound=_parse(d["as_of_upper_bound"]),
                )
                actual = ref.recompute_sha256(self.root)
                if actual != ref.sha256:
                    raise SnapshotError(
                        "DATA_NOT_READY",
                        f"dataset {name!r} of {snapshot_id} failed hash verification",
                        snapshot_id,
                        "the published content changed after publication; restore it or republish",
                    )
                return actual
        raise SnapshotError(
            "DATA_NOT_READY", f"snapshot {snapshot_id!r} has no dataset {name!r}",
            snapshot_id, "check the snapshot manifest for available dataset names",
        )

    def blocking_issues(self, snapshot_id: str) -> list[dict]:
        rows = self.con.execute(
            "SELECT * FROM snapshot_blocking_issue WHERE snapshot_id=? ORDER BY issue_seq",
            (snapshot_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------ guards
    def assert_same_mode(self, snapshot_ids: list[str]) -> DataMode:
        """§15.4 禁止把合成数据与真实行情混成单一收益曲线。"""

        modes = {self.get(s)["data_mode"] for s in snapshot_ids}
        if len(modes) > 1:
            raise SnapshotError(
                "DATA_NOT_READY",
                f"snapshots mix data modes {sorted(modes)}; synthetic and production series "
                "must never be combined into one return curve",
                ",".join(snapshot_ids),
                "split the comparison, or restrict the input to one data mode",
            )
        return DataMode(next(iter(modes)))


def write_dataset(root: str | Path, relpath: str, payload: bytes) -> DatasetRef:  # type: ignore[name-defined]
    """写一个数据集文件并返回带真实哈希的引用。仅用于测试与合成数据生成。"""

    target = Path(root) / relpath
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)
    ref = DatasetRef(
        name=Path(relpath).stem, path=relpath,
        sha256="sha256:" + hashlib.sha256(payload).hexdigest(),
        record_count=payload.count(b"\n") + (0 if payload.endswith(b"\n") else 1),
        as_of_upper_bound=datetime.now(timezone.utc),
    )
    return ref


def json_bytes(obj: object) -> bytes:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")
