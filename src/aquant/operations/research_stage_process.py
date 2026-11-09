"""Resource-guarded one-shot verifier for staged research scans."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
import threading
import time
from typing import Sequence
from uuid import uuid4


_GIB = 1024 ** 3
_START_HEADROOM = 5 * _GIB // 2
_MIN_RUNNING_HEADROOM = _GIB
_POLL_SECONDS = 0.25
_TIME_LIMIT_SECONDS = 60.0
_DB_BUSY_TIMEOUT_MS = 500
_MAX_RECEIPT_BYTES = 4096
_MAX_PATH_CHARS = 4096
_MAX_VERSION_CHARS = 256
_HASH_RE = re.compile(r"\Asha256:[0-9a-f]{64}\Z")
_RECEIPT_KEYS = frozenset({"nonce", "path", "artifactHash", "sourcesHash", "factsHash", "version"})


def provenance_hash(sources: Sequence[Sequence[str]]) -> str:
    """Hash sorted provenance triples using the lab's compact JSON encoding."""
    canonical = sorted(tuple(source) for source in sources)
    encoded = json.dumps(canonical, separators=(",", ":")).encode("ascii")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _financial_check_active(metadata_db: Path) -> bool:
    path = Path(metadata_db).resolve(strict=True)
    connection = sqlite3.connect(
        f"{path.as_uri()}?mode=ro",
        uri=True,
        timeout=_DB_BUSY_TIMEOUT_MS / 1000,
        isolation_level=None,
    )
    try:
        connection.execute(f"PRAGMA busy_timeout = {_DB_BUSY_TIMEOUT_MS}")
        connection.execute("PRAGMA query_only = ON")
        row = connection.execute(
            "SELECT EXISTS(SELECT 1 FROM job "
            "WHERE job_type = ? AND status IN (?, ?))",
            ("FINANCIAL_CHECK", "PENDING", "RUNNING"),
        ).fetchone()
        if row is None or row[0] not in (0, 1):
            raise sqlite3.DatabaseError("active-job query returned no boolean")
        return bool(row[0])
    finally:
        connection.close()


def _read_headroom_bytes() -> int | None:
    """Return VM headroom tightened by a finite cgroup memory limit."""
    try:
        meminfo = Path("/proc/meminfo").read_text(encoding="ascii")
        vm_available = None
        for line in meminfo.splitlines():
            fields = line.split()
            if len(fields) == 3 and fields[0] == "MemAvailable:" and fields[2] == "kB":
                vm_available = int(fields[1]) * 1024
                break
        if vm_available is None or vm_available < 0:
            return None

        cgroup_limit = Path("/sys/fs/cgroup/memory.max").read_text(encoding="ascii").strip()
        if cgroup_limit == "max":
            return vm_available
        limit_bytes = int(cgroup_limit)
        current_bytes = int(Path("/sys/fs/cgroup/memory.current").read_text(encoding="ascii").strip())
        if limit_bytes < 0 or current_bytes < 0:
            return None
        return min(vm_available, max(0, limit_bytes - current_bytes))
    except (OSError, UnicodeError, ValueError):
        return None


def _stop_and_reap(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        try:
            process.terminate()
        except OSError:
            pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        if process.poll() is None:
            try:
                process.kill()
            except OSError:
                pass
        process.wait()


def _read_bounded_stdout(stream, result: dict[str, object]) -> None:
    captured = bytearray()
    overflow = False
    try:
        while True:
            chunk = stream.read(8192)
            if not chunk:
                break
            remaining = _MAX_RECEIPT_BYTES - len(captured)
            if remaining > 0:
                captured.extend(chunk[:remaining])
            if len(chunk) > remaining:
                overflow = True
    finally:
        result["stdout"] = bytes(captured)
        result["overflow"] = overflow
        stream.close()


def _run_guarded_verifier(command: list[str], metadata_db: Path) -> tuple[bytes | None, str]:
    try:
        if _financial_check_active(Path(metadata_db)):
            return None, "financial_busy"
    except (OSError, sqlite3.Error, ValueError, TypeError):
        return None, "resource_unavailable"

    headroom = _read_headroom_bytes()
    if headroom is None:
        return None, "resource_unavailable"
    if headroom < _START_HEADROOM:
        return None, "insufficient_memory"
    try:
        # The parent owns this directory and removes it even after terminating
        # the child during its verified facts database copy.
        with TemporaryDirectory(prefix="aquant-stage-child-") as scratch:
            return _run_child(command, metadata_db, scratch)
    except OSError:
        return None, "resource_unavailable"


def _run_child(command, metadata_db, scratch):
    try:
        child_env = os.environ.copy()
        child_env.update({"TMPDIR": scratch, "TMP": scratch, "TEMP": scratch})
        process = subprocess.Popen(
            command,
            env=child_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=False,
            close_fds=True,
        )
    except (OSError, ValueError, TypeError):
        return None, "verifier_failed"

    capture: dict[str, object] = {}
    reader = threading.Thread(
        target=_read_bounded_stdout,
        args=(process.stdout, capture),
        name="research-stage-stdout",
        daemon=True,
    )
    reason = "verifier_failed"
    keep_output = False
    deadline = time.monotonic() + _TIME_LIMIT_SECONDS
    try:
        reader.start()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                reason = "timeout"
                break
            try:
                process.wait(timeout=min(_POLL_SECONDS, remaining))
            except subprocess.TimeoutExpired:
                if time.monotonic() >= deadline:
                    reason = "timeout"
                    break
                try:
                    if _financial_check_active(Path(metadata_db)):
                        reason = "financial_busy"
                        break
                except (OSError, sqlite3.Error, ValueError, TypeError):
                    reason = "resource_unavailable"
                    break

                headroom = _read_headroom_bytes()
                if headroom is None:
                    reason = "resource_unavailable"
                    break
                if headroom < _MIN_RUNNING_HEADROOM:
                    reason = "insufficient_memory"
                    break
                continue

            if process.returncode == 0:
                reason = "verified"
                keep_output = True
            else:
                reason = "verifier_failed"
            break
    except Exception:
        reason = "resource_unavailable"
    finally:
        if process.poll() is None:
            _stop_and_reap(process)
        if reader.ident is not None:
            reader.join(timeout=5)
        if reader.ident is None or reader.is_alive():
            try:
                process.stdout.close()
            except OSError:
                pass
            if reader.ident is not None:
                reader.join(timeout=1)

    if not keep_output or reader.is_alive() or capture.get("overflow"):
        return None, "verifier_failed" if keep_output else reason
    stdout = capture.get("stdout")
    if not isinstance(stdout, bytes):
        return None, "verifier_failed"
    return stdout, "verified"


def _strict_json_object(payload: bytes) -> dict[str, object]:
    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON receipt field")
            result[key] = value
        return result

    value = json.loads(payload.decode("utf-8"), object_pairs_hook=unique_pairs)
    if not isinstance(value, dict) or set(value) != _RECEIPT_KEYS:
        raise ValueError("child receipt has an unexpected shape")
    return value


def verify_staged_scan(
    path: Path,
    expected_hash: str,
    metadata_db: Path,
    version: str,
) -> tuple[dict[str, str] | None, str]:
    """Verify a staged scan in a guarded child, returning no receipt on failure."""
    try:
        if not isinstance(expected_hash, str) or not _HASH_RE.fullmatch(expected_hash):
            return None, "verifier_failed"
        if not isinstance(version, str) or not version or len(version) > _MAX_VERSION_CHARS or "\x00" in version:
            return None, "verifier_failed"
        staged = Path(path).resolve(strict=True)
        if not staged.is_file() or len(str(staged)) > _MAX_PATH_CHARS:
            return None, "verifier_failed"
        nonce = uuid4().hex
        command = [
            sys.executable,
            "-B",
            "-m",
            "aquant.operations.research_stage_process",
            str(staged),
            expected_hash,
            nonce,
            version,
        ]
        output, outcome = _run_guarded_verifier(command, Path(metadata_db))
        if output is None:
            return None, outcome
        if len(output) > _MAX_RECEIPT_BYTES:
            return None, "verifier_failed"

        receipt = _strict_json_object(output)
        if any(not isinstance(receipt[key], str) for key in _RECEIPT_KEYS):
            return None, "verifier_failed"
        if (receipt["nonce"] != nonce or receipt["path"] != str(staged)
                or receipt["artifactHash"] != expected_hash or receipt["version"] != version
                or not _HASH_RE.fullmatch(receipt["sourcesHash"])
                or not _HASH_RE.fullmatch(receipt["factsHash"])):
            return None, "verifier_failed"
        return receipt, "verified"
    except Exception:
        return None, "resource_unavailable"


def _apply_low_priority() -> None:
    """Best-effort CPU and I/O priority reduction before importing scan code."""
    if not sys.platform.startswith("linux"):
        return
    try:
        os.nice(10)
    except (OSError, AttributeError):
        pass
    try:
        changed = subprocess.run(
            ["ionice", "-c", "3", "-p", str(os.getpid())],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
        if changed.returncode != 0:
            return
    except (OSError, subprocess.SubprocessError):
        pass


def _verify_in_child(staged_arg: str, expected_hash: str, nonce: str, version: str) -> dict[str, str]:
    if len(staged_arg) > _MAX_PATH_CHARS:
        raise ValueError("staged path exceeds the supported length")
    staged_arg_path = Path(staged_arg)
    if not staged_arg_path.is_absolute():
        raise ValueError("staged path must be absolute")
    staged = staged_arg_path.resolve(strict=True)
    if not staged.is_file() or len(str(staged)) > _MAX_PATH_CHARS:
        raise ValueError("staged path is not a supported file")
    if not _HASH_RE.fullmatch(expected_hash) or not nonce or len(nonce) > 64 or "\x00" in nonce:
        raise ValueError("child verification arguments are invalid")
    if not version or len(version) > _MAX_VERSION_CHARS or "\x00" in version:
        raise ValueError("verifier version is invalid")

    from aquant.operations import research_scan

    if version != research_scan._RESEARCH_SCAN_CACHE_VERSION:
        raise ValueError("requested verifier version is stale")
    packet, artifact_hash, _body = research_scan._verified_research_scan_packet(staged)
    if artifact_hash != expected_hash:
        raise ValueError("verified artifact digest differs from the requested digest")

    sources = tuple(sorted(
        (item["role"], item["path"], item["contentHash"])
        for item in packet["provenance"]
    ))
    if not sources or any(
        not isinstance(role, str) or not role
        or not isinstance(source_path, str) or not source_path
        or not isinstance(digest, str) or not _HASH_RE.fullmatch(digest)
        for role, source_path, digest in sources
    ):
        raise ValueError("verified artifact provenance is malformed")
    facts_sources = [source for source in sources if source[0] == "facts_db"]
    if len(facts_sources) != 1:
        raise ValueError("verified artifact does not select exactly one facts database")
    facts_hash = facts_sources[0][2]

    facts_cache = research_scan._SCAN_FACTS_CACHE
    if (not isinstance(facts_cache, tuple) or len(facts_cache) != 2
            or facts_cache[0] != facts_hash or facts_cache[1] is None):
        raise ValueError("verified proof-free facts cache is absent or does not match facts_db")

    return {
        "nonce": nonce,
        "path": str(staged),
        "artifactHash": artifact_hash,
        "sourcesHash": provenance_hash(sources),
        "factsHash": facts_hash,
        "version": research_scan._RESEARCH_SCAN_CACHE_VERSION,
    }


def _child_main(argv: list[str]) -> int:
    if len(argv) != 5:
        return 2
    staged_arg, expected_hash, nonce, version = argv[1:]
    _apply_low_priority()
    try:
        # Keep stdout reserved for the bounded, single-object receipt.
        import contextlib
        with open(os.devnull, "w", encoding="utf-8") as discarded, contextlib.redirect_stdout(discarded):
            receipt = _verify_in_child(staged_arg, expected_hash, nonce, version)
        encoded = json.dumps(receipt, separators=(",", ":"), ensure_ascii=True).encode("ascii") + b"\n"
        if len(encoded) > _MAX_RECEIPT_BYTES:
            return 3
    except Exception:
        return 1
    sys.stdout.buffer.write(encoded)
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(_child_main(sys.argv))
