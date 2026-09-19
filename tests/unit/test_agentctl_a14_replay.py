from __future__ import annotations

from typing import Any

import pytest

from aquant.adapters.agentctl.conversation_replay import (
    ConversationReplayReadConfig,
    ConversationReplayTransportConfig,
    ConversationReplayUnavailable,
    paginate_conversation_replay,
    parse_turn_projection,
    publish_turn_projection,
    read_conversation_replay,
    require_conversation_replay,
)


def _response(*, stream_scope: str = "turn_projection") -> dict[str, Any]:
    return {
        "application_event_projection": {
            "schema_version": "aios.application_integration_projection.v1",
            "ready": True,
            "stream_scope": stream_scope,
            "authoritative_conversation_log": stream_scope == "conversation_replay",
        },
        "application_event_page": {
            "schema_version": "aios.application_interaction_event_page.v1",
            "tenant_id": "aquant-synthetic",
            "product_id": "aquant_lab",
            "conversation_id": "conv-a14",
            "stream_scope": stream_scope,
            "after_sequence": 0,
            "events": [],
            "next_cursor": None,
            "has_more": False,
            "gap_detected": False,
            "heartbeat_after_ms": 15000,
        },
    }


def test_a14_accepts_turn_projection_for_rendering_but_rejects_it_for_reopen() -> None:
    page = parse_turn_projection(_response())
    assert page.stream_scope == "turn_projection"

    with pytest.raises(ConversationReplayUnavailable) as exc_info:
        require_conversation_replay(_response())

    error = exc_info.value
    assert error.code == "CONVERSATION_REPLAY_UNAVAILABLE"
    assert error.reason == "authoritative_conversation_replay_unavailable"
    assert "raw_provider" not in str(error)


def test_a14_does_not_create_local_replay_when_core_transport_is_unconfigured() -> None:
    with pytest.raises(ConversationReplayUnavailable) as exc_info:
        publish_turn_projection(
            _response(),
            ConversationReplayTransportConfig(
                base_url="",
                service_token="",
                subject_user_id="user-a14",
            ),
        )

    assert exc_info.value.code == "CONVERSATION_REPLAY_TRANSPORT_UNCONFIGURED"
    assert exc_info.value.reason == "conversation_replay_transport_unconfigured"


def test_a14_replay_response_is_strictly_revalidated() -> None:
    page = require_conversation_replay(_response(stream_scope="conversation_replay"))
    assert page.stream_scope == "conversation_replay"


def _page(*, after_sequence: int, after_cursor: str | None, event_sequence: int,
          event_cursor: str, has_more: bool, next_cursor: str | None) -> dict[str, Any]:
    return {
        "schema_version": "aios.application_interaction_event_page.v1",
        "tenant_id": "aquant-synthetic",
        "product_id": "aquant_lab",
        "conversation_id": "conv-a14",
        "stream_scope": "conversation_replay",
        "after_sequence": after_sequence,
        "after_cursor": after_cursor,
        "events": [
            {
                "schema_version": "aios.application_interaction_event.v1",
                "event_id": f"evt-{event_sequence}",
                "sequence": event_sequence,
                "cursor": event_cursor,
                "event_type": "assistant.turn",
                "occurred_at": "2026-09-19T00:00:00+00:00",
                "tenant_id": "aquant-synthetic",
                "product_id": "aquant_lab",
                "conversation_id": "conv-a14",
                "request_id": f"req-{event_sequence}",
                "trace_id": f"trace-{event_sequence}",
                "producer": "agentctl",
                "payload": {"message": f"answer-{event_sequence}"},
                "replayable": True,
            }
        ],
        "next_cursor": next_cursor,
        "has_more": has_more,
        "gap_detected": False,
        "heartbeat_after_ms": 15000,
    }


def test_a14_pagination_requires_contiguous_authoritative_pages() -> None:
    pages = iter(
        [
            _page(
                after_sequence=0,
                after_cursor=None,
                event_sequence=1,
                event_cursor="c1",
                has_more=True,
                next_cursor="c1",
            ),
            _page(
                after_sequence=1,
                after_cursor="c1",
                event_sequence=2,
                event_cursor="c2",
                has_more=False,
                next_cursor="c2",
            ),
        ]
    )

    recovered = paginate_conversation_replay(
        lambda _cursor: next(pages),
        tenant_id="aquant-synthetic",
        product_id="aquant_lab",
        conversation_id="conv-a14",
    )

    assert [event.sequence for page in recovered for event in page.events] == [1, 2]


def test_a14_pagination_rejects_a_gap_in_persistent_history() -> None:
    with pytest.raises(ConversationReplayUnavailable) as exc_info:
        paginate_conversation_replay(
            lambda _cursor: _page(
                after_sequence=0,
                after_cursor=None,
                event_sequence=2,
                event_cursor="c2",
                has_more=False,
                next_cursor="c2",
            ),
            tenant_id="aquant-synthetic",
            product_id="aquant_lab",
            conversation_id="conv-a14",
        )

    assert exc_info.value.code == "CONVERSATION_REPLAY_PAGE_INVALID"


def test_a14_http_reader_replays_pages_with_explicit_auth(monkeypatch) -> None:
    responses = iter(
        [
            _page(
                after_sequence=0,
                after_cursor=None,
                event_sequence=1,
                event_cursor="c1",
                has_more=True,
                next_cursor="c1",
            ),
            _page(
                after_sequence=1,
                after_cursor="c1",
                event_sequence=2,
                event_cursor="c2",
                has_more=False,
                next_cursor="c2",
            ),
        ]
    )
    requests: list[Any] = []

    class _Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self, *_args):
            import json

            return json.dumps(next(responses)).encode("utf-8")

    def _urlopen(request, *, timeout):
        requests.append((request.full_url, dict(request.header_items()), timeout))
        return _Response()

    monkeypatch.setattr(
        "aquant.adapters.agentctl.conversation_replay.urlopen",
        _urlopen,
    )
    pages = read_conversation_replay(
        ConversationReplayReadConfig(
            base_url="https://core.example/",
            auth_token="read-token",
            tenant_id="aquant-synthetic",
            product_id="aquant_lab",
            conversation_id="conv-a14",
        )
    )

    assert len(pages) == 2
    assert pages[-1].events[-1].sequence == 2
    assert requests[0][1]["Authorization"] == "Bearer read-token"
    assert "after_cursor=c1" in requests[1][0]
