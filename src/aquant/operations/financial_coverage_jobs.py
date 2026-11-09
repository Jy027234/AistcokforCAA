"""Version-bound, read-only financial coverage job helpers.

The request path captures cheap immutable input fingerprints only.  The worker
does the existing full research/proof validation and report computation, then
stores the (potentially large) report as a hash-bound file below the snapshot
data directory; the shared ``job.result_json`` remains a small reference.
"""

from __future__ import annotations

import copy
import hashlib
import json
import mmap
import os
import re
import sqlite3
import threading
import tempfile
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from aquant.artifact_paths import resolve_artifact_path
from aquant.domain.data.db import connect
from aquant.domain.data.snapshot import SnapshotError
from aquant.domain.fundamentals.fact_repository import FinancialFactRepository
from aquant.operations.financial_coverage import build_financial_coverage_report
from aquant.operations.forward_comparison import ComparisonError, ForwardComparisonStore
from aquant.operations.research_candidates import ResearchCandidateError
from aquant.operations.research_jobs import job_payload
from aquant.operations.research_simulation import load_simulation_research
from aquant.operations.s2_readiness import S2ReadinessError, load_s2_target_period_map


JOB_FINANCIAL_CHECK = "FINANCIAL_CHECK"
FINANCIAL_CHECK_VERSION = "financial-check-v19"
RESULT_DIRECTORY = "financial-check-results"
_JOB_ID = re.compile(r"^job_[0-9a-f]{20}$")
_ROOT = Path(__file__).resolve().parents[3]
_PRIORITY_READERS = ThreadPoolExecutor(max_workers=2, thread_name_prefix="financial-priority")


class FinancialCheckError(RuntimeError):
    """A safe, user-displayable failure from input binding or execution."""

    def __init__(self, code: str, message: str, repair_action: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.repair_action = repair_action


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def _digest(value: bytes | mmap.mmap) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _read_artifact(path: str | Path) -> tuple[Path, bytes, str]:
    resolved = Path(resolve_artifact_path(path))
    body = resolved.read_bytes()
    return resolved, body, _digest(body)


_ROOT_CLOSE = re.compile(rb'(?m)^\}[ \t]*\r?$')
_ROOT_INDENT = re.compile(rb'\A(?:\xef\xbb\xbf)?\{\r?\n( {1,8})"')


def _decode_json_fragment(fragment: bytes) -> Any:
    text = fragment.decode("utf-8")
    value, end = json.JSONDecoder().raw_decode(text)
    if text[end:].strip():
        raise ValueError("unexpected content after research metadata")
    return value


def _indented_research_metadata(body: bytes | mmap.mmap, indent: bytes) -> dict[str, Any]:
    """Read the producer's root indentation and find metadata in C-level searches.

    The large candidate list is never walked in Python. The standard producer
    writes top-level fields at one or two spaces, with nested values indented
    further, so the next root property or root close bounds provenance.
    """
    prefix = rb'(?m)^' + re.escape(indent)
    top_level = re.compile(prefix + rb'"(?:[^"\\]|\\.)*"[ \t]*:')
    snapshot_field = re.compile(prefix + rb'"snapshotId"[ \t]*:[ \t]*')
    provenance_field = re.compile(prefix + rb'"provenance"[ \t]*:[ \t]*')

    def fields(name: bytes, pattern: re.Pattern[bytes]):
        # Find literal root keys in C before checking their small syntax spans.
        # Scanning every candidate line with a multiline regex is much slower
        # on the real research packet; nested fields have a different indent.
        marker = b"\n" + indent + b'"' + name + b'"'
        position = body.find(marker)
        matches = []
        while position >= 0:
            match = pattern.match(body, position + 1)
            if match is not None:
                matches.append(match)
            position = body.find(marker, position + len(marker))
        return matches

    snapshot_fields = fields(b"snapshotId", snapshot_field)
    if len(snapshot_fields) != 1:
        raise ValueError("research artifact must have one top-level snapshotId")
    snapshot_match = snapshot_fields[0]
    snapshot_end = body.find(b"\n", snapshot_match.end())
    if snapshot_end < 0:
        snapshot_end = len(body)
    snapshot_raw = body[snapshot_match.end():snapshot_end].strip()
    if snapshot_raw.endswith(b","):
        snapshot_raw = snapshot_raw[:-1].rstrip()
    snapshot_id = _decode_json_fragment(snapshot_raw)

    provenance_fields = fields(b"provenance", provenance_field)
    if len(provenance_fields) > 1:
        raise ValueError("research artifact repeats top-level provenance")
    if not provenance_fields:
        provenance: Any = []
    else:
        provenance_match = provenance_fields[0]
        next_field = top_level.search(body, provenance_match.end())
        root_close = _ROOT_CLOSE.search(body, provenance_match.end())
        ends = [match.start() for match in (next_field, root_close) if match is not None]
        if not ends:
            raise ValueError("research artifact has no closing top-level object")
        raw_end = min(ends)
        provenance_raw = body[provenance_match.end():raw_end].strip()
        if provenance_raw.endswith(b","):
            provenance_raw = provenance_raw[:-1].rstrip()
        provenance = _decode_json_fragment(provenance_raw)

    return {"snapshotId": snapshot_id, "provenance": provenance}


def _research_metadata(body: bytes | mmap.mmap) -> tuple[dict, Path | None]:
    """Decode binding fields without materializing a large pretty-printed packet.

    Non-production formatting falls back to the standard C JSON decoder; the
    worker still performs full source, schema, and formula validation.
    """
    indentation = _ROOT_INDENT.match(body[:256])
    metadata = (_indented_research_metadata(body, indentation.group(1))
                if indentation
                else json.loads(bytes(body)))
    if not isinstance(metadata, dict):
        raise ValueError("research artifact must be an object")
    provenance = metadata.get("provenance")
    if provenance is None:
        provenance = []
    elif not isinstance(provenance, list):
        raise ValueError("research provenance must be a list")
    snapshot_id = metadata.get("snapshotId")
    if not isinstance(snapshot_id, str) or not snapshot_id.strip():
        raise ValueError("research artifact has no snapshot binding")
    reconstructed = next((Path(item["path"]) for item in provenance
                          if isinstance(item, dict) and item.get("role") == "facts_db"), None)
    if reconstructed is not None:
        reconstructed = Path(resolve_artifact_path(reconstructed))
    return {"snapshotId": snapshot_id, "provenance": provenance}, reconstructed


def _table_digest(con: sqlite3.Connection, table: str, *, order_by: str,
                  required: bool) -> str:
    """Hash exact stored rows and schema without validating or replaying proof."""

    schema = con.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    if schema is None:
        if required:
            raise ValueError(f"required financial version table {table} is missing")
        return _digest(("missing:" + table).encode("utf-8"))
    digest = hashlib.sha256()
    digest.update(_canonical([table, schema["sql"]]))
    for row in con.execute(
        f'SELECT * FROM "{table}" ORDER BY {order_by}'
    ):
        digest.update(b"\n")
        digest.update(_canonical(list(row)))
    triggers = con.execute(
        "SELECT name,sql FROM sqlite_master WHERE type='trigger' AND tbl_name=? "
        "ORDER BY name", (table,),
    )
    for row in triggers:
        digest.update(b"\ntrigger:")
        digest.update(_canonical([row["name"], row["sql"]]))
    return "sha256:" + digest.hexdigest()


_REPOSITORY_VERSION_CACHE_LIMIT = 4
_REPOSITORY_VERSION_CACHE_LOCK = threading.RLock()


@dataclass(slots=True)
class _RepositoryVersionWatcher:
    path: Path
    connection: sqlite3.Connection
    file_identity: tuple[Any, ...]
    data_version: int
    facts_version: str | None = None
    proof_version: str | None = None
    proof_known: bool = False


_REPOSITORY_VERSION_WATCHERS: OrderedDict[str, _RepositoryVersionWatcher] = OrderedDict()
_FINANCIAL_REPOSITORY_VERSION_PATHS: tuple[Path, Path | None] | None = None


class _RepositoryVersionChanged(RuntimeError):
    """The source changed while its exact version was being checked."""


def _stat_identity(path: Path) -> tuple[int, int, int, int, int] | None:
    try:
        info = path.stat()
    except FileNotFoundError:
        return None
    return (info.st_dev, info.st_ino, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns)


def _repository_file_identity(path: Path) -> tuple[Any, ...]:
    main_identity = _stat_identity(path)
    if main_identity is None:
        raise FileNotFoundError(path)
    return main_identity


def _read_data_version(connection: sqlite3.Connection) -> int:
    row = connection.execute("PRAGMA data_version").fetchone()
    if row is None:
        raise sqlite3.DatabaseError("SQLite returned no data_version")
    return int(row[0])


def _clear_repository_version_digest(watcher: _RepositoryVersionWatcher,
                                      data_version: int) -> None:
    watcher.data_version = data_version
    watcher.facts_version = None
    watcher.proof_version = None
    watcher.proof_known = False


def _close_repository_version_watcher(path_key: str) -> None:
    watcher = _REPOSITORY_VERSION_WATCHERS.pop(path_key, None)
    if watcher is not None:
        watcher.connection.close()


def shutdown_repository_version_cache() -> None:
    """Close all bounded, read-only fingerprint watchers on application shutdown."""

    global _FINANCIAL_REPOSITORY_VERSION_PATHS
    with _REPOSITORY_VERSION_CACHE_LOCK:
        _FINANCIAL_REPOSITORY_VERSION_PATHS = None
        watchers = list(_REPOSITORY_VERSION_WATCHERS.values())
        _REPOSITORY_VERSION_WATCHERS.clear()
        first_error: sqlite3.Error | None = None
        for watcher in watchers:
            try:
                watcher.connection.close()
            except sqlite3.Error as exc:
                if first_error is None:
                    first_error = exc
        if first_error is not None:
            raise RuntimeError(
                "failed to close financial repository version watchers"
            ) from first_error


def _open_repository_version_watcher(
    path: Path, expected_identity: tuple[Any, ...],
) -> _RepositoryVersionWatcher:
    connection = connect(path, read_only=True, allow_thread_sharing=True)
    try:
        connection.execute("PRAGMA query_only=ON")
        opened_identity = _repository_file_identity(path)
        data_version = _read_data_version(connection)
        final_identity = _repository_file_identity(path)
        if opened_identity != expected_identity or final_identity != expected_identity:
            raise _RepositoryVersionChanged(
                "financial facts database identity changed while opening")
        return _RepositoryVersionWatcher(
            path=path, connection=connection, file_identity=final_identity,
            data_version=data_version,
        )
    except BaseException:
        connection.close()
        raise


def _repository_version_watcher(
    path: Path,
) -> tuple[str, _RepositoryVersionWatcher]:
    path_key = str(path)
    file_identity = _repository_file_identity(path)
    watcher = _REPOSITORY_VERSION_WATCHERS.get(path_key)
    if watcher is not None and watcher.file_identity != file_identity:
        _close_repository_version_watcher(path_key)
        watcher = None
    if watcher is None:
        watcher = _open_repository_version_watcher(path, file_identity)
        _REPOSITORY_VERSION_WATCHERS[path_key] = watcher
    _REPOSITORY_VERSION_WATCHERS.move_to_end(path_key)
    while len(_REPOSITORY_VERSION_WATCHERS) > _REPOSITORY_VERSION_CACHE_LIMIT:
        oldest_key = next(iter(_REPOSITORY_VERSION_WATCHERS))
        _close_repository_version_watcher(oldest_key)
    return path_key, watcher


def _compute_repository_versions(
    connection: sqlite3.Connection, *, include_proof: bool,
) -> dict[str, str | None]:
    connection.execute("BEGIN")
    try:
        facts_version = _table_digest(
            connection, "financial_fact", order_by="sequence", required=True)
        proof_version: str | None = None
        if include_proof:
            reviews = _table_digest(
                connection, "financial_fact_review", order_by="bundle_id", required=True)
            members = _table_digest(
                connection, "financial_fact_review_member",
                order_by="bundle_id,version_id", required=True)
            supplements = _table_digest(
                connection, "financial_fact_proof_supplement",
                order_by="supplement_id", required=False)
            proof_version = _digest(_canonical([reviews, members, supplements]))
        connection.execute("COMMIT")
        return {"factsVersion": facts_version, "proofVersion": proof_version}
    except BaseException:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise


def _versions_from_watcher(
    path_key: str, watcher: _RepositoryVersionWatcher, *, include_proof: bool,
) -> dict[str, str | None]:
    identity_before = _repository_file_identity(watcher.path)
    data_version_before = _read_data_version(watcher.connection)
    identity_after_check = _repository_file_identity(watcher.path)
    if (identity_before != watcher.file_identity
            or identity_after_check != watcher.file_identity):
        _close_repository_version_watcher(path_key)
        raise _RepositoryVersionChanged(
            "financial facts database file identity changed before fingerprint reuse")
    if data_version_before != watcher.data_version:
        _clear_repository_version_digest(watcher, data_version_before)

    if (watcher.facts_version is not None
            and (not include_proof or watcher.proof_known)):
        # Close the gap between the initial eligibility check and fast return.
        # A commit after the final data_version read is ordered after this
        # currentness observation and will invalidate the next lookup.
        identity_cache_before = _repository_file_identity(watcher.path)
        data_version_cache = _read_data_version(watcher.connection)
        identity_cache_after = _repository_file_identity(watcher.path)
        if (identity_cache_before != identity_before
                or identity_cache_after != identity_before):
            _close_repository_version_watcher(path_key)
            raise _RepositoryVersionChanged(
                "financial facts database identity changed before cached version reuse")
        if data_version_cache == data_version_before:
            return {
                "factsVersion": watcher.facts_version,
                "proofVersion": watcher.proof_version if include_proof else None,
            }
        _clear_repository_version_digest(watcher, data_version_cache)
        data_version_before = data_version_cache

    versions = _compute_repository_versions(
        watcher.connection, include_proof=include_proof)
    identity_after_hash = _repository_file_identity(watcher.path)
    data_version_after_hash = _read_data_version(watcher.connection)
    if (identity_after_hash != identity_before
            or data_version_after_hash != data_version_before):
        _close_repository_version_watcher(path_key)
        raise _RepositoryVersionChanged(
            "financial facts database changed while fingerprinting")

    watcher.file_identity = identity_after_hash
    watcher.data_version = data_version_after_hash
    watcher.facts_version = versions["factsVersion"]
    if include_proof:
        watcher.proof_version = versions["proofVersion"]
        watcher.proof_known = True
    return versions


def repository_versions(db_path: str | Path, *, include_proof: bool) -> dict[str, str | None]:
    """Return exact immutable row fingerprints using one short read transaction.

    This deliberately hashes stored rows and proof identities only.  It does
    not run the expensive PDF replay; that remains inside the worker's normal
    ``build_financial_coverage_report`` path. A bounded, read-only watcher may
    reuse the exact prior digest only while the same connection's
    ``data_version`` and main database file identity remain unchanged. This is
    WAL-aware and fails closed if the database changes during a fingerprint.
    """

    path: Path | None = None
    try:
        path = Path(resolve_artifact_path(db_path)).resolve()
        if not path.is_file():
            raise FileNotFoundError
        # Keep the repository's established schema/path validation, without
        # calling either load() or load_with_accepted_s2_pdf_reviews().
        FinancialFactRepository.open_existing(path)
        with _REPOSITORY_VERSION_CACHE_LOCK:
            path_key, watcher = _repository_version_watcher(path)
            return _versions_from_watcher(
                path_key, watcher, include_proof=include_proof)
    except _RepositoryVersionChanged as exc:
        if path is not None:
            with _REPOSITORY_VERSION_CACHE_LOCK:
                _close_repository_version_watcher(str(path))
        raise FinancialCheckError(
            "FINANCIAL_CHECK_INPUT_NOT_READY",
            "财务事实数据库在版本指纹读取期间发生变化。",
            "确认财务资料库写入或归档已完成后重新发起核验。",
        ) from exc
    except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
        if path is not None:
            with _REPOSITORY_VERSION_CACHE_LOCK:
                _close_repository_version_watcher(str(path))
        raise FinancialCheckError(
            "FINANCIAL_CHECK_INPUT_NOT_READY",
            "财务事实或原文核验版本暂不可读取。",
            "确认当前财务资料库可读后重新发起核验。",
        ) from exc


def _configured_path(env_name: str, default: Path) -> Path:
    configured = os.environ.get(env_name, "").strip()
    return Path(resolve_artifact_path(configured or default))


def _research_packet_for_binding(research_path: Path) -> tuple[dict, str, Path | None]:
    """Read bytes/metadata only; full source and formula validation is worker work."""

    try:
        resolved = Path(resolve_artifact_path(research_path))
        with resolved.open("rb") as handle:
            before = os.fstat(handle.fileno())
            if before.st_size == 0:
                raise ValueError("research artifact is empty")
            with mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ) as body:
                content_hash = _digest(body)
                packet, reconstructed = _research_metadata(body)
            after = os.fstat(handle.fileno())
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise ValueError("research artifact changed while binding inputs")
        return packet, content_hash, reconstructed
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise FinancialCheckError(
            "FINANCIAL_CHECK_INPUT_NOT_READY",
            "研究材料版本暂不可读取。",
            "确认当前研究资料可读后重新发起核验。",
        ) from exc


def clear_financial_repository_version_paths() -> None:
    """Forget the last resolved targets before a fresh application startup."""

    global _FINANCIAL_REPOSITORY_VERSION_PATHS
    with _REPOSITORY_VERSION_CACHE_LOCK:
        _FINANCIAL_REPOSITORY_VERSION_PATHS = None


def current_financial_repository_version_paths(
) -> tuple[Path, Path | None] | None:
    """Return the startup warm's resolved targets, if available."""

    with _REPOSITORY_VERSION_CACHE_LOCK:
        return _FINANCIAL_REPOSITORY_VERSION_PATHS


def resolve_financial_repository_version_paths() -> tuple[Path, Path | None]:
    """Resolve exact configured DB targets from current packet metadata.

    The potentially large research packet is read only when startup or a
    publication change calls this resolver. Stable health polling uses the
    returned paths directly and relies on each SQLite watcher's data_version.
    """

    research_path = _configured_path(
        "AQUANT_RESEARCH_CANDIDATES_FILE",
        _ROOT / "data" / "s2-structured" / "research-candidates.json",
    )
    live_facts_path = _configured_path(
        "AQUANT_LIVE_FINANCIAL_FACTS_DB",
        _ROOT / "data" / "s2-pdf-review" / "s2-machine-verified-facts.sqlite",
    )
    _, _, reconstructed_path = _research_packet_for_binding(research_path)
    return live_facts_path, reconstructed_path


def warm_financial_repository_version_paths(
    live_facts_path: str | Path,
    reconstructed_path: str | Path | None,
) -> None:
    """Refresh exact fingerprints for already resolved repository targets."""

    repository_versions(live_facts_path, include_proof=True)
    if reconstructed_path is not None:
        repository_versions(reconstructed_path, include_proof=False)


def warm_financial_repository_versions() -> None:
    """Warm exact source fingerprints for the current configured research packet.

    This only reads the configured research metadata and financial repositories;
    it does not create a job or run the full coverage report. Failures propagate
    so the application can log a safe feature-scoped warning and stay available.
    """

    global _FINANCIAL_REPOSITORY_VERSION_PATHS
    paths = resolve_financial_repository_version_paths()
    with _REPOSITORY_VERSION_CACHE_LOCK:
        # Keep resolved targets available for background retry even if the
        # repository scan below fails. The packet was resolved once at startup.
        _FINANCIAL_REPOSITORY_VERSION_PATHS = paths
    warm_financial_repository_version_paths(*paths)


def _priority_ids(data_dir: Path, subject: str) -> list[str]:
    try:
        series = ForwardComparisonStore(data_dir / "forward-comparison", create=False).list_series(subject=subject)
        return sorted({str(target["instrument_id"])
                       for item in series
                       for target in item.get("modelTargets", [])
                       if isinstance(target, dict) and target.get("instrument_id")})
    except (OSError, ValueError, ComparisonError, KeyError, TypeError) as exc:
        raise FinancialCheckError(
            "FINANCIAL_CHECK_INPUT_NOT_READY",
            "研究优先目标暂不可读取。",
            "确认研究对照资料可读后重新发起核验。",
        ) from exc


def capture_financial_check_inputs(*, data_dir: str | Path, snapshot_id: str,
                                  trading_day: str, snapshot_cutoff_at: str,
                                  subject: str) -> dict[str, Any]:
    """Capture a cheap server-side binding; never run deep report validation."""

    data_root = Path(data_dir).resolve()
    research_path = _configured_path(
        "AQUANT_RESEARCH_CANDIDATES_FILE",
        _ROOT / "data" / "s2-structured" / "research-candidates.json",
    )
    live_facts_path = _configured_path(
        "AQUANT_LIVE_FINANCIAL_FACTS_DB",
        _ROOT / "data" / "s2-pdf-review" / "s2-machine-verified-facts.sqlite",
    )
    target_map_value = os.environ.get("AQUANT_S2_TARGET_PERIOD_MAP", "").strip()
    target_map_path = (Path(resolve_artifact_path(target_map_value))
                       if target_map_value else None)
    archive_root = _configured_path(
        "AQUANT_FORWARD_ARCHIVE_ROOT", _ROOT / "deploy" / "agentctl-q0" / "forward-archive")

    priority_read = _PRIORITY_READERS.submit(_priority_ids, data_root, subject)
    try:
        packet, research_hash, reconstructed_path = _research_packet_for_binding(research_path)
        live_versions = repository_versions(live_facts_path, include_proof=True)
        reconstructed_versions = (repository_versions(reconstructed_path, include_proof=False)
                                  if reconstructed_path is not None else
                                  {"factsVersion": None, "proofVersion": None})
        if target_map_path is None:
            target_map_hash = _digest(b"no-target-period-map")
        else:
            try:
                _, _, target_map_hash = _read_artifact(target_map_path)
            except OSError as exc:
                raise FinancialCheckError(
                    "FINANCIAL_CHECK_INPUT_NOT_READY",
                    "目标报告期映射暂不可读取。",
                    "确认目标报告期资料可读后重新发起核验。",
                ) from exc
        # Keep the original error precedence even when the priority read finishes first.
        priorities = priority_read.result()
    finally:
        priority_read.cancel()

    binding: dict[str, Any] = {
        "snapshotId": snapshot_id,
        "snapshotCutoffAt": snapshot_cutoff_at,
        "tradingDay": trading_day,
        "researchSnapshotId": str(packet["snapshotId"]),
        "researchContentHash": research_hash,
        "liveFactsVersion": live_versions["factsVersion"],
        "proofVersion": live_versions["proofVersion"],
        "reconstructedFactsVersion": reconstructed_versions["factsVersion"],
        "targetMapHash": target_map_hash,
        "priorityInstrumentIdsHash": _digest(_canonical(priorities)),
        "codeVersion": FINANCIAL_CHECK_VERSION,
    }
    binding["inputHash"] = _digest(_canonical(binding))
    input_spec = {
        "dataDir": str(data_root),
        "researchCandidatesFile": str(research_path),
        "liveFactsDb": str(live_facts_path),
        "reconstructedFactsDb": str(reconstructed_path) if reconstructed_path else None,
        "targetPeriodMapFile": str(target_map_path) if target_map_path else None,
        "marketCapsArchiveRoot": str(archive_root),
        "priorityInstrumentIds": priorities,
    }
    return {
        "inputBinding": binding,
        "inputSpec": input_spec,
        "idempotencyNamespace": f"fincheck:{hashlib.sha256(subject.encode('utf-8')).hexdigest()[:16]}:{binding['inputHash']}",
    }


def public_binding(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    keys = (
        "inputHash", "snapshotId", "snapshotCutoffAt", "tradingDay",
        "researchSnapshotId", "researchContentHash", "liveFactsVersion",
        "proofVersion", "reconstructedFactsVersion", "targetMapHash",
        "priorityInstrumentIdsHash", "codeVersion",
    )
    return {key: value.get(key) for key in keys}


def binding_from_job(job) -> dict[str, Any]:
    payload = job_payload(job.payload_json)
    binding = payload.get("inputBinding")
    return public_binding(binding)


def idempotency_namespace_for_retry(namespace: str, parent_job_id: str) -> str:
    return f"{namespace}:retry:{parent_job_id}"


def _validate_binding_shape(binding: Any) -> dict[str, Any]:
    if not isinstance(binding, dict) or binding.get("codeVersion") != FINANCIAL_CHECK_VERSION:
        raise FinancialCheckError(
            "FINANCIAL_CHECK_INPUT_INVALID",
            "财务核验任务的版本绑定无效。",
            "重新发起当前财务核验。",
        )
    expected = dict(binding)
    input_hash = expected.pop("inputHash", None)
    if not isinstance(input_hash, str) or input_hash != _digest(_canonical(expected)):
        raise FinancialCheckError(
            "FINANCIAL_CHECK_INPUT_INVALID",
            "财务核验任务的版本绑定校验失败。",
            "重新发起当前财务核验。",
        )
    return binding


def _check_research_binding(path: Path, binding: dict[str, Any]) -> tuple[dict, dict, Path | None]:
    try:
        packet, research_binding = load_simulation_research(path)
    except (OSError, ValueError, ResearchCandidateError) as exc:
        raise FinancialCheckError(
            "FINANCIAL_CHECK_INPUT_NOT_READY",
            "研究材料完整性核验失败。",
            "确认当前研究资料版本有效后重新发起核验。",
        ) from exc
    if (research_binding.get("researchContentHash") != binding.get("researchContentHash")
            or research_binding.get("researchSnapshotId") != binding.get("researchSnapshotId")):
        raise FinancialCheckError(
            "FINANCIAL_CHECK_INPUT_CHANGED",
            "研究材料在排队期间发生变化，本次结果未生成。",
            "重新发起当前财务核验以绑定最新研究材料。",
        )
    provenance = packet.get("provenance") or []
    reconstructed = next((Path(item["path"]) for item in provenance
                          if isinstance(item, dict) and item.get("role") == "facts_db"), None)
    if reconstructed is not None:
        reconstructed = Path(resolve_artifact_path(reconstructed))
    return packet, research_binding, reconstructed


def _check_source_versions(input_spec: dict[str, Any], binding: dict[str, Any]) -> tuple[dict, dict | None]:
    live = repository_versions(input_spec["liveFactsDb"], include_proof=True)
    if (live["factsVersion"] != binding.get("liveFactsVersion")
            or live["proofVersion"] != binding.get("proofVersion")):
        raise FinancialCheckError(
            "FINANCIAL_CHECK_INPUT_CHANGED",
            "正式事实或原文核验版本在排队期间发生变化，本次结果未生成。",
            "重新发起当前财务核验以绑定最新事实版本。",
        )
    reconstructed_path = input_spec.get("reconstructedFactsDb")
    reconstructed = (repository_versions(reconstructed_path, include_proof=False)
                     if reconstructed_path else None)
    expected_reconstructed = (reconstructed["factsVersion"] if reconstructed else None)
    if expected_reconstructed != binding.get("reconstructedFactsVersion"):
        raise FinancialCheckError(
            "FINANCIAL_CHECK_INPUT_CHANGED",
            "历史重建事实版本在排队期间发生变化，本次结果未生成。",
            "重新发起当前财务核验以绑定最新研究资料。",
        )
    return live, reconstructed


def _artifact_path(data_dir: str | Path, job_id: str, *, create: bool) -> Path:
    if not _JOB_ID.fullmatch(str(job_id)):
        raise FinancialCheckError(
            "FINANCIAL_CHECK_RESULT_UNAVAILABLE",
            "财务核验结果引用无效。",
            "重新读取任务状态；如问题仍在，请联系维护人员。",
        )
    root = Path(data_dir).resolve()
    directory = root / RESULT_DIRECTORY
    if create:
        directory.mkdir(parents=True, exist_ok=True)
    try:
        resolved_directory = directory.resolve(strict=True)
        resolved_directory.relative_to(root)
    except (OSError, ValueError) as exc:
        raise FinancialCheckError(
            "FINANCIAL_CHECK_RESULT_UNAVAILABLE",
            "财务核验结果存储不可用。",
            "确认本机数据目录可读写后重新发起核验。",
        ) from exc
    return resolved_directory / f"{job_id}.json"


def _safe_report(report: dict) -> dict:
    public = copy.deepcopy(report)
    for section in ("formalS2", "historicalReconstructed"):
        value = public.get(section)
        if isinstance(value, dict):
            value.pop("factsDb", None)
    return public


def _write_report(data_dir: str | Path, job_id: str, report: dict) -> tuple[str, int]:
    target = _artifact_path(data_dir, job_id, create=True)
    body = _canonical(_safe_report(report))
    temp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=str(target.parent), prefix=f".{job_id}.",
            suffix=".tmp", delete=False,
        ) as handle:
            temp_name = handle.name
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, target)
    except OSError as exc:
        if temp_name:
            try:
                Path(temp_name).unlink()
            except OSError:
                pass
        raise FinancialCheckError(
            "FINANCIAL_CHECK_RESULT_UNAVAILABLE",
            "财务核验完成，但结果文件无法保存。",
            "确认本机数据目录可读写后重新发起核验。",
        ) from exc
    return _digest(body), len(body)


def run_financial_check_job(job, *, write_guard=None) -> dict[str, Any]:
    """Run the unchanged financial report for the exact captured inputs."""

    try:
        payload = job_payload(job.payload_json)
    except (TypeError, ValueError) as exc:
        raise FinancialCheckError(
            "FINANCIAL_CHECK_INPUT_INVALID",
            "财务核验任务的输入记录无效。",
            "重新发起当前财务核验。",
        ) from exc
    binding = _validate_binding_shape(payload.get("inputBinding"))
    input_spec = payload.get("inputSpec")
    if not isinstance(input_spec, dict):
        raise FinancialCheckError(
            "FINANCIAL_CHECK_INPUT_INVALID",
            "财务核验任务缺少输入资料绑定。",
            "重新发起当前财务核验。",
        )
    data_dir = Path(input_spec["dataDir"])
    research_path = Path(input_spec["researchCandidatesFile"])
    _, _, reconstructed_path = _check_research_binding(research_path, binding)
    expected_reconstructed_path = input_spec.get("reconstructedFactsDb")
    actual_reconstructed_path = str(reconstructed_path) if reconstructed_path else None
    if actual_reconstructed_path != expected_reconstructed_path:
        raise FinancialCheckError(
            "FINANCIAL_CHECK_INPUT_CHANGED",
            "研究材料引用的历史事实来源发生变化，本次结果未生成。",
            "重新发起当前财务核验以绑定最新研究材料。",
        )

    target_map_path = input_spec.get("targetPeriodMapFile")
    if target_map_path:
        try:
            target_map = load_s2_target_period_map(target_map_path)
        except (OSError, sqlite3.Error, ValueError, S2ReadinessError) as exc:
            raise FinancialCheckError(
                "FINANCIAL_CHECK_INPUT_NOT_READY",
                "目标报告期映射完整性核验失败。",
                "确认当前目标报告期资料有效后重新发起核验。",
            ) from exc
        if target_map.get("content_hash") != binding.get("targetMapHash"):
            raise FinancialCheckError(
                "FINANCIAL_CHECK_INPUT_CHANGED",
                "目标报告期映射在排队期间发生变化，本次结果未生成。",
                "重新发起当前财务核验以绑定最新目标映射。",
            )
    else:
        target_map = {}
    _check_source_versions(input_spec, binding)

    if write_guard is not None:
        write_guard()
    try:
        report = build_financial_coverage_report(
            snapshot_dir=data_dir,
            snapshot_id=binding["snapshotId"],
            live_facts_db=input_spec["liveFactsDb"],
            reconstructed_facts_db=expected_reconstructed_path,
            market_caps_archive_root=Path(input_spec["marketCapsArchiveRoot"]),
            priority_instrument_ids=input_spec.get("priorityInstrumentIds", []),
            latest_period_ends=target_map.get("latest_period_ends"),
            target_period_records=target_map.get("records", []),
            **({"target_period_basis": target_map["target_period_basis"]}
               if target_map else {}),
        )
    except (OSError, sqlite3.Error, ValueError, SnapshotError) as exc:
        raise FinancialCheckError(
            "FINANCIAL_CHECK_NOT_READY",
            "财务覆盖核验未能完成。",
            "确认快照、财务事实和原文资料可读后重新发起核验。",
        ) from exc

    if (report.get("snapshotId") != binding["snapshotId"]
            or report.get("tradingDay") != binding["tradingDay"]
            or report.get("snapshotCutoffAt") != binding["snapshotCutoffAt"]):
        raise FinancialCheckError(
            "FINANCIAL_CHECK_INPUT_CHANGED",
            "报告输出与固定快照绑定不一致，本次结果未发布。",
            "重新发起当前财务核验。",
        )
    try:
        _, _, post_research_hash = _read_artifact(research_path)
    except OSError as exc:
        raise FinancialCheckError(
            "FINANCIAL_CHECK_INPUT_CHANGED",
            "研究材料在核验期间不可读取，本次结果未生成。",
            "重新发起当前财务核验以绑定最新研究材料。",
        ) from exc
    if post_research_hash != binding.get("researchContentHash"):
        raise FinancialCheckError(
            "FINANCIAL_CHECK_INPUT_CHANGED",
            "研究材料在核验期间发生变化，本次结果未生成。",
            "重新发起当前财务核验以绑定最新研究材料。",
        )
    target_map_path = input_spec.get("targetPeriodMapFile")
    if target_map_path:
        try:
            _, _, current_target_hash = _read_artifact(target_map_path)
        except OSError as exc:
            raise FinancialCheckError(
                "FINANCIAL_CHECK_INPUT_CHANGED",
                "目标报告期映射在核验期间不可读取，本次结果未生成。",
                "重新发起当前财务核验以绑定最新目标映射。",
            ) from exc
        if current_target_hash != binding.get("targetMapHash"):
            raise FinancialCheckError(
                "FINANCIAL_CHECK_INPUT_CHANGED",
                "目标报告期映射在核验期间发生变化，本次结果未生成。",
                "重新发起当前财务核验以绑定最新目标映射。",
            )
    _check_source_versions(input_spec, binding)
    if write_guard is not None:
        write_guard()

    result_hash, result_size = _write_report(data_dir, job.job_id, report)
    verified_at = datetime.now(timezone.utc).isoformat()
    return {
        "artifactId": job.job_id,
        "resultHash": result_hash,
        "resultSizeBytes": result_size,
        "verifiedAt": verified_at,
        "snapshotId": binding["snapshotId"],
        "inputBinding": public_binding(binding),
    }


def read_financial_check_result(data_dir: str | Path, job) -> dict[str, Any]:
    """Read a stored report by job id and verify bytes; never rerun the check."""

    try:
        metadata = json.loads(job.result_json or "{}")
    except (TypeError, json.JSONDecodeError) as exc:
        raise FinancialCheckError(
            "FINANCIAL_CHECK_RESULT_UNAVAILABLE",
            "财务核验结果索引不可读取。",
            "重新读取任务状态；如问题仍在，请联系维护人员。",
        ) from exc
    if metadata.get("artifactId") != job.job_id:
        raise FinancialCheckError(
            "FINANCIAL_CHECK_RESULT_UNAVAILABLE",
            "财务核验结果索引与任务不匹配。",
            "重新读取任务状态；如问题仍在，请联系维护人员。",
        )
    expected_binding = binding_from_job(job)
    if (metadata.get("snapshotId") != expected_binding.get("snapshotId")
            or public_binding(metadata.get("inputBinding")) != expected_binding):
        raise FinancialCheckError(
            "FINANCIAL_CHECK_RESULT_UNAVAILABLE",
            "财务核验结果版本与任务绑定不匹配。",
            "重新发起核验；如问题仍在，请联系维护人员。",
        )
    path = _artifact_path(data_dir, job.job_id, create=False)
    try:
        body = path.read_bytes()
        if _digest(body) != metadata.get("resultHash"):
            raise ValueError("stored result hash mismatch")
        report = json.loads(body)
        if not isinstance(report, dict):
            raise ValueError("stored report is not an object")
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise FinancialCheckError(
            "FINANCIAL_CHECK_RESULT_UNAVAILABLE",
            "财务核验结果文件缺失或校验失败。",
            "重新发起核验；如问题仍在，请联系维护人员。",
        ) from exc
    return {
        "report": report,
        "verifiedAt": metadata.get("verifiedAt"),
        "binding": public_binding(metadata.get("inputBinding")),
        "resultHash": metadata.get("resultHash"),
    }


def error_response(code: str | None) -> dict[str, Any]:
    """Expose stable user-facing errors without exception paths or secrets."""

    messages = {
        "FINANCIAL_CHECK_INPUT_CHANGED": (
            "核验输入版本已变化，本次结果未作为当前版本返回。",
            "重新发起财务核验以绑定最新输入。",
        ),
        "FINANCIAL_CHECK_INPUT_NOT_READY": (
            "财务核验所需资料暂不可用。",
            "确认当前快照、研究资料和财务资料可读后重新发起核验。",
        ),
        "FINANCIAL_CHECK_RESULT_UNAVAILABLE": (
            "财务核验结果暂不可读取。",
            "重新读取任务状态；如问题仍在，请联系维护人员。",
        ),
    }
    message, repair = messages.get(
        str(code),
        ("财务核验失败。", "检查当前资料状态后重新发起核验。"),
    )
    return {"code": str(code or "DATA_NOT_READY"), "message": message,
            "repairAction": repair}


__all__ = [
    "FINANCIAL_CHECK_VERSION", "JOB_FINANCIAL_CHECK", "FinancialCheckError",
    "binding_from_job", "capture_financial_check_inputs", "error_response",
    "clear_financial_repository_version_paths",
    "current_financial_repository_version_paths",
    "idempotency_namespace_for_retry", "public_binding",
    "read_financial_check_result", "repository_versions",
    "resolve_financial_repository_version_paths", "run_financial_check_job",
    "shutdown_repository_version_cache", "warm_financial_repository_versions",
    "warm_financial_repository_version_paths",
]
