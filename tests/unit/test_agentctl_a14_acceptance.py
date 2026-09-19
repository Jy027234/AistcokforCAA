from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from tests.acceptance import q5_acceptance as q5


def _turn_projection_response(
    *,
    conversation_id: str = "placeholder",
    request_id: str = "request-placeholder",
) -> dict[str, Any]:
    return {
        "application_event_projection": {
            "schema_version": "aios.application_integration_projection.v1",
            "ready": True,
            "stream_scope": "turn_projection",
            "authoritative_conversation_log": False,
        },
        "application_event_page": {
            "schema_version": "aios.application_interaction_event_page.v1",
            "tenant_id": q5.TENANT,
            "product_id": q5.PRODUCT,
            "conversation_id": conversation_id,
            "stream_scope": "turn_projection",
            "after_sequence": 0,
            "events": [
                {
                    "schema_version": "aios.application_interaction_event.v1",
                    "event_id": "evt-a14",
                    "sequence": 1,
                    "cursor": "cursor-a14",
                    "event_type": "assistant.turn",
                    "occurred_at": "2026-09-19T00:00:00+00:00",
                    "tenant_id": q5.TENANT,
                    "product_id": q5.PRODUCT,
                    "conversation_id": conversation_id,
                    "request_id": request_id,
                    "trace_id": "trace-a14",
                    "producer": "agentctl",
                    "payload": {"message": "answer-a14"},
                    "replayable": True,
                }
            ],
            "next_cursor": None,
            "has_more": False,
            "gap_detected": False,
            "heartbeat_after_ms": 15000,
        },
    }


def test_a14_live_probe_records_fail_closed_boundary_without_core_transport(monkeypatch) -> None:
    class FakeClient:
        def status(self):
            return {"client_mode": "assist", "server_mode": "assist"}

        def invoke(self, *_args, **kwargs):
            return _turn_projection_response(
                conversation_id=kwargs["conversation_id"],
                request_id=kwargs["request_id"],
            )

        def close(self):
            return None

    monkeypatch.setattr(q5, "connect", lambda *_args, **_kwargs: FakeClient())
    monkeypatch.delenv(q5.A14_REPLAY_BASE_URL_ENV, raising=False)
    monkeypatch.delenv(q5.A14_REPLAY_SERVICE_TOKEN_ENV, raising=False)
    monkeypatch.delenv(q5.A14_REPLAY_READ_TOKEN_ENV, raising=False)

    result = q5._a14_session_recovery_probe("http://example.invalid", "token", "hmac")

    assert result["strict_replay_rejected"] is True
    assert result["strict_replay_reason"] == "authoritative_conversation_replay_unavailable"
    assert result["passed"] is False
    assert result["blocker"]["blocker_code"] == "conversation_replay_transport_unconfigured"


def test_a14_configured_publish_failure_is_failed(monkeypatch) -> None:
    class FakeClient:
        def status(self):
            return {"client_mode": "assist", "server_mode": "assist"}

        def invoke(self, *_args, **kwargs):
            return _turn_projection_response(
                conversation_id=kwargs["conversation_id"],
                request_id=kwargs["request_id"],
            )

        def close(self):
            return None

    monkeypatch.setattr(q5, "connect", lambda *_args, **_kwargs: FakeClient())
    monkeypatch.setenv(q5.A14_REPLAY_BASE_URL_ENV, "https://core.example")
    monkeypatch.setenv(q5.A14_REPLAY_SERVICE_TOKEN_ENV, "service-token")
    monkeypatch.setenv(q5.A14_REPLAY_READ_TOKEN_ENV, "read-token")

    def fail_publish(*_args, **_kwargs):
        raise q5.ConversationReplayUnavailable(
            "CONVERSATION_REPLAY_TRANSPORT_FAILED",
            "publish failed",
            "retry",
            reason="conversation_replay_transport_failed",
        )

    monkeypatch.setattr(q5, "publish_turn_projection", fail_publish)

    result = q5._a14_session_recovery_probe("http://example.invalid", "token", "hmac")

    assert result["passed"] is False
    assert result["blocker"]["blocker_code"] == "conversation_replay_transport_failed"
    assert q5._a14_is_structural_uncovered(result) is False


def test_a14_reader_missing_is_the_only_reader_uncovered_case(monkeypatch) -> None:
    class FakeClient:
        def status(self):
            return {"client_mode": "assist", "server_mode": "assist"}

        def invoke(self, *_args, **kwargs):
            return _turn_projection_response(
                conversation_id=kwargs["conversation_id"],
                request_id=kwargs["request_id"],
            )

        def close(self):
            return None

    monkeypatch.setattr(q5, "connect", lambda *_args, **_kwargs: FakeClient())
    monkeypatch.setenv(q5.A14_REPLAY_BASE_URL_ENV, "https://core.example")
    monkeypatch.setenv(q5.A14_REPLAY_SERVICE_TOKEN_ENV, "service-token")
    monkeypatch.delenv(q5.A14_REPLAY_READ_TOKEN_ENV, raising=False)
    monkeypatch.setattr(
        q5,
        "publish_turn_projection",
        lambda *_args, **_kwargs: SimpleNamespace(stream_scope="conversation_replay"),
    )

    result = q5._a14_session_recovery_probe("http://example.invalid", "token", "hmac")

    assert result["passed"] is False
    assert result["blocker"]["blocker_code"] == "conversation_replay_reader_unconfigured"
    assert q5._a14_is_structural_uncovered(result) is True


def test_a14_configured_reader_failure_is_failed(monkeypatch) -> None:
    class FakeClient:
        def status(self):
            return {"client_mode": "assist", "server_mode": "assist"}

        def invoke(self, *_args, **kwargs):
            return _turn_projection_response(
                conversation_id=kwargs["conversation_id"],
                request_id=kwargs["request_id"],
            )

        def close(self):
            return None

    monkeypatch.setattr(q5, "connect", lambda *_args, **_kwargs: FakeClient())
    monkeypatch.setenv(q5.A14_REPLAY_BASE_URL_ENV, "https://core.example")
    monkeypatch.setenv(q5.A14_REPLAY_SERVICE_TOKEN_ENV, "service-token")
    monkeypatch.setenv(q5.A14_REPLAY_READ_TOKEN_ENV, "read-token")
    monkeypatch.setattr(
        q5,
        "publish_turn_projection",
        lambda *_args, **_kwargs: SimpleNamespace(stream_scope="conversation_replay"),
    )
    monkeypatch.setattr(
        q5,
        "read_conversation_replay",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            q5.ConversationReplayUnavailable(
                "CONVERSATION_REPLAY_READ_FAILED",
                "read failed",
                "retry",
                reason="conversation_replay_read_transport_failed",
            )
        ),
    )

    result = q5._a14_session_recovery_probe("http://example.invalid", "token", "hmac")

    assert result["passed"] is False
    assert result["blocker"]["blocker_code"] == "conversation_replay_reader_failed"
    assert q5._a14_is_structural_uncovered(result) is False


def test_a14_recovery_rejects_unrelated_nonempty_event(monkeypatch) -> None:
    class FakeClient:
        def status(self):
            return {"client_mode": "assist", "server_mode": "assist"}

        def invoke(self, *_args, **kwargs):
            return _turn_projection_response(
                conversation_id=kwargs["conversation_id"],
                request_id=kwargs["request_id"],
            )

        def close(self):
            return None

    monkeypatch.setattr(q5, "connect", lambda *_args, **_kwargs: FakeClient())
    monkeypatch.setenv(q5.A14_REPLAY_BASE_URL_ENV, "https://core.example")
    monkeypatch.setenv(q5.A14_REPLAY_SERVICE_TOKEN_ENV, "service-token")
    monkeypatch.setenv(q5.A14_REPLAY_READ_TOKEN_ENV, "read-token")
    monkeypatch.setattr(
        q5,
        "publish_turn_projection",
        lambda *_args, **_kwargs: SimpleNamespace(stream_scope="conversation_replay"),
    )

    def read_unrelated(*_args, **_kwargs):
        return [
            SimpleNamespace(
                stream_scope="conversation_replay",
                events=[
                    SimpleNamespace(
                        request_id="other-request",
                        conversation_id="other-conversation",
                    )
                ],
            )
        ]

    monkeypatch.setattr(q5, "read_conversation_replay", read_unrelated)

    result = q5._a14_session_recovery_probe("http://example.invalid", "token", "hmac")

    assert result["reopened_event_count"] == 1
    assert result["reopened_event_bound"] is False
    assert result["reopened_replay_observed"] is False
    assert result["passed"] is False
    assert result["blocker"]["blocker_code"] == "conversation_replay_evidence_incomplete"
    assert q5._a14_is_structural_uncovered(result) is False
