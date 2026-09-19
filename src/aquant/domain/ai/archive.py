"""Immutable model-output archives and deterministic replay support.

Q5 A12 has two separate facts to preserve:

* the exact model response that was used by a research path; and
* the fact that a later replay/new model attempt is a new run, even when it
  consumes the same archived response.

The archive is deliberately kept in the product metadata store.  It is
content-addressed by the fixed model input, output bytes, provider/model and
prompt version, while ``model_run`` records each use of that archive.  Reading
an archive always re-computes its SHA-256 before returning the text.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from aquant.domain.data.db import write_tx
from aquant.domain.ai.model import ModelRequest, ModelResponse, ModelUnavailable


def _sha256(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def output_hash(text: str) -> str:
    """Hash the exact UTF-8 bytes of a model response."""

    if not isinstance(text, str):
        raise TypeError("model output must be text")
    return _sha256(text.encode("utf-8"))


def input_hash(request: ModelRequest) -> str:
    """Hash all fixed model-input fields that affect a response.

    Provider/model identity is intentionally excluded: replaying the same
    input with a different model is a separate run, while the input remains
    comparable.  The output token limit and temperature are included because
    they can change the response bytes.
    """

    payload = {
        "instructions": request.instructions,
        "context": request.context,
        "prompt_version": request.prompt_version,
        "max_output_tokens": request.max_output_tokens,
        "temperature": request.temperature,
    }
    body = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return _sha256(body)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ModelArchiveError(RuntimeError):
    """Stored archive is missing, malformed, or has changed bytes."""


@dataclass(frozen=True, slots=True)
class ModelOutputArchive:
    archive_id: str
    subject_id: str
    input_hash: str
    output_hash: str
    artifact_hash: str | None
    output_text: str
    provider: str
    model: str
    prompt_version: str
    source_model_call_id: str | None
    archived_at: str


def _archive_id(*, subject_id: str, input_digest: str, output_digest: str,
                provider: str, model: str, prompt_version: str) -> str:
    body = "|".join(
        (subject_id, input_digest, output_digest, provider, model, prompt_version)
    ).encode("utf-8")
    return "mo-" + hashlib.sha256(body).hexdigest()[:32]


def _normalise_subject(subject_id: str | None) -> str:
    """Return the authenticated owner scope used by archive records.

    The current product API exposes a verified subject header rather than a
    separate tenant claim.  Keeping the exact subject as the scope avoids
    silently widening access; deployments with tenant mapping can pass their
    mapped subject value here.  Direct domain callers retain the explicit
    ``system`` scope for backwards-compatible local jobs.
    """

    value = str(subject_id or "").strip()
    return value or "system"


def structured_artifact_hash(text: str) -> str | None:
    """Hash a canonical JSON research artifact when the model returned one.

    ``output_hash`` covers the exact response bytes.  This second digest is
    useful for a quantitative result whose JSON key order/whitespace may vary:
    canonical JSON gives the product an explicit structured-artifact identity
    while retaining the original bytes for audit and replay.
    """

    if not isinstance(text, str):
        raise TypeError("model output must be text")
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return None
    if not isinstance(value, (dict, list)):
        return None
    body = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return _sha256(body)


def _archive_output_in_tx(
    con: sqlite3.Connection,
    *,
    input_digest: str,
    output_text: str,
    provider: str,
    model: str,
    prompt_version: str,
    source_model_call_id: str | None,
    subject_id: str | None = None,
) -> ModelOutputArchive:
    if not isinstance(output_text, str):
        raise TypeError("model output must be text")
    if not provider or not model or not prompt_version:
        raise ValueError("provider, model and prompt_version are required")
    owner = _normalise_subject(subject_id)
    digest = output_hash(output_text)
    artifact_digest = structured_artifact_hash(output_text)
    archive_id = _archive_id(
        subject_id=owner, input_digest=input_digest,
        output_digest=digest,
        provider=provider,
        model=model,
        prompt_version=prompt_version,
    )
    archived_at = _now()
    con.execute(
        "INSERT OR IGNORE INTO model_output_archive "
        "(archive_id,subject_id,input_hash,output_hash,artifact_hash,"
        "output_text,provider,model,prompt_version,source_model_call_id,"
        "archived_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (archive_id, owner, input_digest, digest, artifact_digest, output_text,
         provider, model, prompt_version, source_model_call_id, archived_at),
    )
    row = con.execute(
        "SELECT archive_id,subject_id,input_hash,output_hash,artifact_hash,"
        "output_text,provider,model,prompt_version,source_model_call_id,"
        "archived_at "
        "FROM model_output_archive WHERE archive_id=?",
        (archive_id,),
    ).fetchone()
    if row is None:
        raise ModelArchiveError("model output archive was not persisted")
    return ModelOutputArchive(
        archive_id=str(row["archive_id"]),
        subject_id=str(row["subject_id"]),
        input_hash=str(row["input_hash"]),
        output_hash=str(row["output_hash"]),
        artifact_hash=(
            str(row["artifact_hash"]) if row["artifact_hash"] is not None else None
        ),
        output_text=str(row["output_text"]),
        provider=str(row["provider"]),
        model=str(row["model"]),
        prompt_version=str(row["prompt_version"]),
        source_model_call_id=(
            str(row["source_model_call_id"])
            if row["source_model_call_id"] is not None else None
        ),
        archived_at=str(row["archived_at"]),
    )


def archive_output(
    con: sqlite3.Connection,
    *,
    request: ModelRequest,
    output_text: str,
    provider: str,
    model: str,
    subject_id: str | None = None,
    source_model_call_id: str | None = None,
) -> ModelOutputArchive:
    """Persist one exact response and return its content-addressed record."""

    with write_tx(con):
        return _archive_output_in_tx(
            con,
            input_digest=input_hash(request),
            output_text=output_text,
            provider=provider,
            model=model,
            prompt_version=request.prompt_version,
            subject_id=subject_id,
            source_model_call_id=source_model_call_id,
        )


def _decode_archive(row: sqlite3.Row) -> ModelOutputArchive:
    archive = ModelOutputArchive(
        archive_id=str(row["archive_id"]),
        subject_id=str(row["subject_id"]),
        input_hash=str(row["input_hash"]),
        output_hash=str(row["output_hash"]),
        artifact_hash=(
            str(row["artifact_hash"]) if row["artifact_hash"] is not None else None
        ),
        output_text=str(row["output_text"]),
        provider=str(row["provider"]),
        model=str(row["model"]),
        prompt_version=str(row["prompt_version"]),
        source_model_call_id=(
            str(row["source_model_call_id"])
            if row["source_model_call_id"] is not None else None
        ),
        archived_at=str(row["archived_at"]),
    )
    actual = output_hash(archive.output_text)
    if actual != archive.output_hash:
        raise ModelArchiveError(
            f"archive {archive.archive_id} output hash mismatch: "
            f"recorded {archive.output_hash}, computed {actual}"
        )
    actual_artifact = structured_artifact_hash(archive.output_text)
    if actual_artifact != archive.artifact_hash:
        raise ModelArchiveError(
            f"archive {archive.archive_id} structured artifact hash mismatch: "
            f"recorded {archive.artifact_hash}, computed {actual_artifact}"
        )
    return archive


def load_archive(
    con: sqlite3.Connection,
    archive_id: str,
    *,
    subject_id: str | None = None,
) -> ModelOutputArchive:
    """Load and verify an archive by ID."""

    owner = _normalise_subject(subject_id) if subject_id is not None else None
    where = "archive_id=?"
    params: tuple[str, ...] = (archive_id,)
    if owner is not None:
        where += " AND subject_id=?"
        params += (owner,)
    row = con.execute(
        "SELECT archive_id,subject_id,input_hash,output_hash,artifact_hash,"
        "output_text,provider,model,prompt_version,source_model_call_id,"
        "archived_at FROM model_output_archive WHERE " + where,
        params,
    ).fetchone()
    if row is None:
        raise ModelArchiveError(f"unknown model output archive {archive_id!r}")
    return _decode_archive(row)


def list_model_runs(
    con: sqlite3.Connection,
    *,
    limit: int = 100,
    subject_id: str | None = None,
) -> list[dict[str, Any]]:
    """Return independent original/replay registrations for audit views."""

    params: list[Any] = []
    where = ""
    if subject_id is not None:
        where = "WHERE subject_id=?"
        params.append(_normalise_subject(subject_id))
    params.append(max(1, min(int(limit), 500)))
    rows = con.execute(
        "SELECT model_run_id,model_call_id,subject_id,research_run_id,archive_id,"
        "input_hash,output_hash,artifact_hash,provider,model,prompt_version,"
        "run_kind,registered_at FROM model_run " + where
        + " ORDER BY registered_at DESC LIMIT ?",
        params,
    ).fetchall()
    return [dict(row) for row in rows]


class ArchivedModelProvider:
    """Text-model provider that replays one verified archived response.

    It follows the normal ``TextModelProvider`` contract, so callers use the
    same ``ask_assistant`` path.  A changed fixed input is an explicit failure;
    the provider never silently returns an old answer for a new context.
    """

    provider_name = "archive-replay"

    def __init__(
        self,
        con: sqlite3.Connection,
        archive_id: str,
        *,
        subject_id: str | None = None,
    ) -> None:
        self._con = con
        self.archive_id = archive_id
        self.subject_id = _normalise_subject(subject_id)
        self._archive = load_archive(con, archive_id, subject_id=self.subject_id)
        self.model_name = self._archive.model

    @property
    def archive(self) -> ModelOutputArchive:
        return self._archive

    def complete(self, request: ModelRequest) -> ModelResponse:
        expected = input_hash(request)
        if expected != self._archive.input_hash:
            raise ModelUnavailable(
                "fixed model input does not match the archived input",
                code="REPLAY_INPUT_MISMATCH",
                repair_action="reuse the frozen snapshot, material and prompt version",
            )
        # Verify immediately before use as well.  This catches a file/database
        # mutation between provider construction and completion.
        try:
            archive = load_archive(
                self._con, self.archive_id, subject_id=self.subject_id
            )
        except ModelArchiveError as exc:
            raise ModelUnavailable(
                str(exc), code="ARCHIVE_CORRUPT",
                repair_action="restore the immutable model output archive",
            ) from exc
        return ModelResponse(
            text=archive.output_text,
            provider=self.provider_name,
            model=archive.model,
            content_hash=archive.output_hash,
            raw_meta={
                "archive_id": archive.archive_id,
                "source_provider": archive.provider,
                "source_model": archive.model,
                "artifact_hash": archive.artifact_hash,
            },
        )
