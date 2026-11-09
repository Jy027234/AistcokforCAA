"""Q5 A12: replay the product's persisted model output on fixed inputs.

The HTTP endpoint is the ordinary product assistant path.  The only local
provider is a deterministic fixture for the first run; replay goes through the
same endpoint with :class:`ArchivedModelProvider`, so these assertions do not
exercise a test-only handler or a second implementation of assistant writes.
"""

from __future__ import annotations

import sqlite3
import sys
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "apps" / "api"))
sys.path.insert(0, str(ROOT / "src"))

from aquant.application.assistant import model_calls  # noqa: E402
from aquant.domain.ai.archive import (  # noqa: E402
    list_model_runs,
    load_archive,
    output_hash,
    structured_artifact_hash,
)
from aquant.domain.data.db import apply_migrations  # noqa: E402
from aquant.domain.ai.model import ModelResponse  # noqa: E402
from aquant.domain.evidence.store import record_evidence  # noqa: E402
from main import build_state, create_app  # noqa: E402


class FixedResearchProvider:
    """Deterministic first-run provider; the product still owns the call path."""

    provider_name = "q5-a12-fixture"
    model_name = "research-model-v1"
    test_only = True

    def complete(self, request) -> ModelResponse:
        # This is intentionally JSON-shaped so the archived bytes are also a
        # plausible structured research result.  The declared digest is wrong
        # on purpose: the product must hash the response bytes it received.
        citation_ids = re.findall(r"\[citation_id: ([^\]\n]+)\]", request.context)
        return ModelResponse(
            text=json.dumps({"answer": "固定摘录记录 F10=42。",
                             "citationIds": citation_ids[:1]}, ensure_ascii=False),
            provider=self.provider_name,
            model=self.model_name,
            content_hash="sha256:" + "0" * 64,
            input_tokens=17,
            output_tokens=9,
        )


def test_fixed_input_and_archived_output_replay_keep_artifact_hash_and_register_new_run(
    tmp_path,
):
    state = build_state(tmp_path)
    state.model_provider = FixedResearchProvider()
    app = create_app(state=state)
    source_text = "固定输入材料：F10=42"
    record_evidence(
        state.con, instrument_id="SYN.A.600519", source_id="synthetic-fixture",
        source_url="https://example.invalid/fixed-input.pdf", source_title="A12 合成证据",
        source_text=source_text,
        available_at=datetime(2026, 9, 7, 15, 0, tzinfo=timezone.utc),
        fact_summary="固定输入 F10=42", verification_status="VERIFIED",
        extra={"announced_on": "2026-09-07", "citations": [source_text]},
    )

    with TestClient(app) as client:
        card_response = client.get(
            "/api/v1/instruments/SYN.A.600519/research",
            params={"trading_day": "2026-09-08"},
        )
        assert card_response.status_code == 200, card_response.text
        card = card_response.json()
        body = {
            "card_id": card["cardId"],
            "question_id": "EVIDENCE_SUMMARY",
            "citation_ids": [next(item["citationId"] for item in card["evidence"]
                                  if item["quote"] == source_text)],
            "confirm_external_send": True,
        }
        first_response = client.post(
            "/api/v1/assistant/messages",
            json=body,
            headers={"X-Aquant-Subject": "user:a12"},
        )
        assert first_response.status_code == 200, first_response.text
        first = first_response.json()

        calls = model_calls(state.con)
        original = next(item for item in calls if item["model_call_id"] == first["modelCallId"])
        assert original["run_kind"] == "ORIGINAL", original
        archive_id = original["archive_id"]
        assert archive_id, original
        archive = load_archive(state.con, archive_id)
        assert archive.input_hash == original["input_hash"]
        assert output_hash(archive.output_text) == archive.output_hash
        assert archive.artifact_hash == structured_artifact_hash(archive.output_text)
        assert original["content_hash"] == archive.output_hash
        assert original["artifact_hash"] == archive.artifact_hash

        # The second request uses the unchanged fixed body and the persisted
        # response through the product replay endpoint.  It crosses the same
        # assistant implementation and creates a new model call/run.
        replay_response = client.post(
            "/api/v1/assistant/replay",
            json={**body, "archive_id": archive_id},
            headers={"X-Aquant-Subject": "user:a12"},
        )
        assert replay_response.status_code == 200, replay_response.text
        replay = replay_response.json()
        replay_call = next(item for item in model_calls(state.con)
                           if item["model_call_id"] == replay["modelCallId"])
        assert replay["text"] == first["text"]
        assert replay["citationIds"] == first["citationIds"]
        assert replay_call["content_hash"] == archive.output_hash
        assert replay_call["artifact_hash"] == archive.artifact_hash

        registrations = list_model_runs(state.con)
        relevant = [
            item for item in registrations
            if item["model_call_id"] in {first["modelCallId"], replay["modelCallId"]}
        ]
        assert len(relevant) == 2, registrations
        assert {item["run_kind"] for item in relevant} == {"ORIGINAL", "REPLAY"}
        assert len({item["model_run_id"] for item in relevant}) == 2
        assert len({item["model_call_id"] for item in relevant}) == 2
        assert {item["archive_id"] for item in relevant} == {archive_id}
        assert {item["output_hash"] for item in relevant} == {archive.output_hash}
        assert {item["artifact_hash"] for item in relevant} == {archive.artifact_hash}
        assert {item["subject_id"] for item in relevant} == {"user:a12"}

        # The archive ID alone is not an authorization token.  A different
        # authenticated subject cannot read or replay this output.
        foreign = client.post(
            "/api/v1/assistant/replay",
            json={**body, "archive_id": archive_id},
            headers={"X-Aquant-Subject": "user:other"},
        )
        assert foreign.status_code == 404, foreign.text
        assert foreign.json()["detail"]["code"] == "ARCHIVE_NOT_FOUND"

        own_calls = client.get(
            "/api/v1/assistant/calls",
            headers={"X-Aquant-Subject": "user:a12"},
        )
        foreign_calls = client.get(
            "/api/v1/assistant/calls",
            headers={"X-Aquant-Subject": "user:other"},
        )
        assert own_calls.status_code == 200
        assert foreign_calls.status_code == 200
        assert own_calls.json()["count"] == 2
        assert foreign_calls.json()["count"] == 0


def test_replay_rejects_changed_fixed_input(tmp_path):
    state = build_state(tmp_path)
    state.model_provider = FixedResearchProvider()
    app = create_app(state=state)
    source_text = "固定输入材料：F10=42"
    record_evidence(
        state.con, instrument_id="SYN.A.600519", source_id="synthetic-fixture",
        source_url="https://example.invalid/fixed-input.pdf", source_title="A12 合成证据",
        source_text=source_text,
        available_at=datetime(2026, 9, 7, 15, 0, tzinfo=timezone.utc),
        fact_summary="固定输入 F10=42", verification_status="VERIFIED",
        extra={"announced_on": "2026-09-07", "citations": [source_text]},
    )
    with TestClient(app) as client:
        card_response = client.get(
            "/api/v1/instruments/SYN.A.600519/research",
            params={"trading_day": "2026-09-08"},
        )
        assert card_response.status_code == 200, card_response.text
        card = card_response.json()
        body = {
            "card_id": card["cardId"],
            "question_id": "EVIDENCE_SUMMARY",
            "citation_ids": [next(item["citationId"] for item in card["evidence"]
                                  if item["quote"] == source_text)],
            "confirm_external_send": True,
        }
        first_response = client.post(
            "/api/v1/assistant/messages", json=body,
            headers={"X-Aquant-Subject": "user:a12"},
        )
        assert first_response.status_code == 200, first_response.text
        first = first_response.json()
        original = next(
            item for item in model_calls(state.con)
            if item["model_call_id"] == first["modelCallId"]
        )
        changed = {**body, "question_id": "EVIDENCE_LIMITATIONS"}
        response = client.post(
            "/api/v1/assistant/replay",
            json={**changed, "archive_id": original["archive_id"]},
            headers={"X-Aquant-Subject": "user:a12"},
        )
        assert response.status_code == 409, response.text
        assert response.json()["detail"]["code"] == "REPLAY_INPUT_MISMATCH"


def test_schema_007_backfills_system_owner_and_preserves_legacy_query_scope(
    tmp_path,
):
    """Old calls stay visible to system/legacy readers without crossing owners."""

    con = sqlite3.connect(tmp_path / "meta.sqlite", isolation_level=None)
    con.row_factory = sqlite3.Row
    try:
        # Build the pre-A12 state first: model_call existed before its owner
        # side table, and one row already has an explicit verified binding.
        con.executescript((ROOT / "schema" / "001_metadata.sql").read_text(
            encoding="utf-8"
        ))
        con.execute(
            "INSERT INTO model_call "
            "(model_call_id,provider,model,prompt_version,outcome,called_at) "
            "VALUES (?,?,?,?,?,?)",
            (
                "mc-legacy-system",
                "legacy-provider",
                "legacy-model",
                "legacy-v1",
                "OK",
                "2026-09-01T00:00:00+00:00",
            ),
        )
        con.execute(
            "INSERT INTO model_call "
            "(model_call_id,provider,model,prompt_version,outcome,called_at) "
            "VALUES (?,?,?,?,?,?)",
            (
                "mc-legacy-owned",
                "legacy-provider",
                "legacy-model",
                "legacy-v1",
                "OK",
                "2026-09-02T00:00:00+00:00",
            ),
        )
        con.execute(
            "CREATE TABLE model_call_owner ("
            "model_call_id TEXT PRIMARY KEY REFERENCES model_call(model_call_id),"
            "subject_id TEXT NOT NULL, bound_at TEXT NOT NULL)"
        )
        con.execute(
            "INSERT INTO model_call_owner "
            "(model_call_id,subject_id,bound_at) VALUES (?,?,?)",
            (
                "mc-legacy-owned",
                "user:alice",
                "2026-09-02T00:00:01+00:00",
            ),
        )

        apply_migrations(con)
        owners_before = {
            row["model_call_id"]: (row["subject_id"], row["bound_at"])
            for row in con.execute(
                "SELECT model_call_id,subject_id,bound_at "
                "FROM model_call_owner ORDER BY model_call_id"
            )
        }
        assert owners_before == {
            "mc-legacy-owned": ("user:alice", "2026-09-02T00:00:01+00:00"),
            "mc-legacy-system": ("system", "2026-09-01T00:00:00+00:00"),
        }

        # A second migration pass must neither duplicate system bindings nor
        # overwrite a binding that was already verified before A12.
        apply_migrations(con)
        owners_after = {
            row["model_call_id"]: (row["subject_id"], row["bound_at"])
            for row in con.execute(
                "SELECT model_call_id,subject_id,bound_at "
                "FROM model_call_owner ORDER BY model_call_id"
            )
        }
        assert owners_after == owners_before

        # ``subject_id='system'`` is the explicit legacy/system scope.  Omitting
        # the subject preserves the historical unscoped query for local tools.
        assert [row["model_call_id"] for row in model_calls(
            con, subject_id="system"
        )] == ["mc-legacy-system"]
        assert {
            row["model_call_id"] for row in model_calls(con)
        } == {"mc-legacy-system", "mc-legacy-owned"}
        assert [row["model_call_id"] for row in model_calls(
            con, subject_id="user:alice"
        )] == ["mc-legacy-owned"]
        assert model_calls(con, subject_id="user:other") == []
    finally:
        con.close()
