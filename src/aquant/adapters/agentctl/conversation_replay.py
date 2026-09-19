"""A-Quant Lab's narrow boundary for durable assistant conversation replay.

The agentctl application-events response is a *turn projection*.  It is useful
for rendering the response that just completed, but it is not a conversation
log.  A-Quant Lab must therefore keep the distinction explicit:

* ``parse_turn_projection`` validates the page returned by Frontdesk;
* ``publish_turn_projection`` uses the agentctl supplied Platform Core
  transport and only returns the page that Core accepted as
  ``conversation_replay``;
* ``require_conversation_replay`` fails closed when the transport is absent.

This module deliberately has no local replay fallback.  A local cache could be
useful for UI hints, but presenting it as authoritative history would hide a
missing Core transport and would violate A14's acceptance boundary.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, urlopen


class ConversationReplayUnavailable(RuntimeError):
    """The requested authoritative conversation replay cannot be proven."""

    def __init__(
        self,
        code: str,
        message: str,
        repair_action: str,
        *,
        reason: str | None = None,
    ) -> None:
        self.code = str(code)
        self.message = str(message)
        self.repair_action = str(repair_action)
        self.reason = str(reason or self.code)
        super().__init__(self.message)

    def to_dict(self) -> dict[str, str]:
        return {
            "code": self.code,
            "message": self.message,
            "repair_action": self.repair_action,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class ConversationReplayTransportConfig:
    """Configuration for the product-to-Core publish boundary.

    The service token is intentionally passed at call time by the deployment
    boundary in normal use.  Keeping it out of evidence objects also makes it
    harder for an acceptance runner to accidentally persist credentials.
    """

    base_url: str
    service_token: str
    subject_user_id: str
    retention_seconds: int = 604800
    timeout_seconds: float = 3.0


@dataclass(frozen=True, slots=True)
class ConversationReplayReadConfig:
    """Authenticated Core page reader settings.

    Core deployments may use a service token for a BFF-side read or a bearer
    token for an end-user read.  The header and prefix are explicit so the
    product does not guess an authority scheme.
    """

    base_url: str
    auth_token: str
    tenant_id: str
    product_id: str
    conversation_id: str
    auth_header: str = "Authorization"
    auth_prefix: str = "Bearer "
    page_limit: int = 200
    timeout_seconds: float = 3.0


def _sdk_application() -> tuple[Any, Any, Any, Any]:
    """Load the external SDK lazily so domain imports stay agentctl-free."""

    try:
        from agentctl.sdk.application import (
            ApplicationConversationTransportError,
            ApplicationEventProjectionUnavailable,
            PlatformCoreApplicationConversationTransport,
            parse_application_event_page,
        )
    except ImportError as exc:  # pragma: no cover - exercised by deployment checks
        raise ConversationReplayUnavailable(
            "agentctl_application_sdk_missing",
            "agentctl application-events SDK is not installed",
            "install the locked agentctl runtime before enabling assistant replay",
        ) from exc
    return (
        ApplicationConversationTransportError,
        ApplicationEventProjectionUnavailable,
        PlatformCoreApplicationConversationTransport,
        parse_application_event_page,
    )


def parse_turn_projection(response: Mapping[str, Any]) -> Any:
    """Validate and return the page emitted by a Frontdesk turn.

    The returned page may only have ``turn_projection`` scope.  Callers that
    need history after reopening must call :func:`require_conversation_replay`.
    """

    _transport_error, _projection_error, _transport, parser = _sdk_application()
    return parser(response)


def require_conversation_replay(response: Mapping[str, Any]) -> Any:
    """Parse a response only when it proves an authoritative replay page.

    In particular, a valid ``turn_projection`` is rejected.  This is the
    product-side fail-closed seam required by A14; the exception reason is
    retained for diagnostics but raw provider payload is never copied into it.
    """

    _transport_error, projection_error, _transport, parser = _sdk_application()
    try:
        return parser(response, require_conversation_replay=True)
    except projection_error as exc:
        reason = str(getattr(exc, "reason", "authoritative_conversation_replay_unavailable"))
        raise ConversationReplayUnavailable(
            "CONVERSATION_REPLAY_UNAVAILABLE",
            "持久会话回放不可用；当前响应不是权威 conversation_replay",
            "配置并启用 Platform Core conversation_replay 传输后重试",
            reason=reason,
        ) from exc


def publish_turn_projection(
    response: Mapping[str, Any],
    config: ConversationReplayTransportConfig,
) -> Any:
    """Publish one validated turn through the real Platform Core transport.

    No transport configuration means an explicit blocker.  The function does
    not create a local ``conversation_replay`` page and does not reinterpret a
    turn projection as history.
    """

    page = parse_turn_projection(response)
    if page.stream_scope == "conversation_replay":
        # This branch is only valid when the upstream response itself already
        # proves replay.  Re-parse through the strict SDK guard so a future SDK
        # cannot silently weaken the boundary.
        return require_conversation_replay(response)
    if not str(config.base_url or "").strip() or not str(config.service_token or "").strip():
        raise ConversationReplayUnavailable(
            "CONVERSATION_REPLAY_TRANSPORT_UNCONFIGURED",
            "未配置 Platform Core conversation_replay 传输",
            "配置 conversation replay Core 地址与服务令牌后重试",
            reason="conversation_replay_transport_unconfigured",
        )
    if not str(config.subject_user_id or "").strip():
        raise ConversationReplayUnavailable(
            "CONVERSATION_REPLAY_SUBJECT_MISSING",
            "持久会话回放缺少主体身份",
            "使用已认证的终端用户主体重试",
            reason="subject_user_id_required",
        )
    _transport_error, _projection_error, transport_type, _parser = _sdk_application()
    try:
        replay_page = transport_type(
            base_url=config.base_url,
            service_token=config.service_token,
            timeout_seconds=config.timeout_seconds,
        ).publish(
            page,
            subject_user_id=config.subject_user_id,
            retention_seconds=config.retention_seconds,
        )
    except _transport_error as exc:
        raise ConversationReplayUnavailable(
            "CONVERSATION_REPLAY_TRANSPORT_FAILED",
            "Platform Core conversation_replay 传输失败",
            "检查 Core replay endpoint、主体授权与保留策略后重试",
            reason="conversation_replay_transport_failed",
        ) from exc
    if getattr(replay_page, "stream_scope", None) != "conversation_replay":
        raise ConversationReplayUnavailable(
            "CONVERSATION_REPLAY_TRANSPORT_INVALID",
            "Platform Core 未返回权威 conversation_replay 页面",
            "修复 Core replay endpoint 的页面合同后重试",
            reason="conversation_replay_response_invalid",
        )
    return replay_page


def paginate_conversation_replay(
    fetch_page: Callable[[str | None], Any],
    *,
    tenant_id: str,
    product_id: str,
    conversation_id: str,
    max_pages: int = 100,
) -> list[Any]:
    """Read every retained replay page through an injected real transport.

    ``fetch_page`` receives the cursor returned by the previous page and must
    return either an ``ApplicationInteractionEventPage`` or its JSON mapping.
    Keeping the network operation injected makes the pagination contract
    testable without inventing a fake replay server; production callers pass a
    Core HTTP reader.  Every page is still parsed by agentctl's native DTO and
    must carry ``conversation_replay`` scope.
    """

    if isinstance(max_pages, bool) or max_pages <= 0 or max_pages > 10_000:
        raise ValueError("max_pages must be between 1 and 10000")
    _transport_error, _projection_error, _transport, _parser = _sdk_application()
    from agentctl.aios.application_integration_contracts import (
        ApplicationInteractionEventPage,
    )

    pages: list[Any] = []
    seen_event_ids: set[str] = set()
    seen_sequences: set[int] = set()
    expected_cursor: str | None = None
    expected_sequence = 0
    for _ in range(max_pages):
        try:
            raw_page = fetch_page(expected_cursor)
            page = (
                raw_page
                if isinstance(raw_page, ApplicationInteractionEventPage)
                else ApplicationInteractionEventPage.from_dict(raw_page)
            )
        except ConversationReplayUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 - transport/contract boundary
            raise ConversationReplayUnavailable(
                "CONVERSATION_REPLAY_PAGE_INVALID",
                "conversation_replay 页面无效",
                "检查 Core replay 分页响应后重试",
                reason="conversation_replay_page_invalid",
            ) from exc

        if page.stream_scope != "conversation_replay":
            raise ConversationReplayUnavailable(
                "CONVERSATION_REPLAY_UNAVAILABLE",
                "分页传输没有返回权威 conversation_replay",
                "配置并启用 Platform Core conversation_replay 传输后重试",
                reason="authoritative_conversation_replay_unavailable",
            )
        if (
            page.tenant_id != tenant_id
            or page.product_id != product_id
            or page.conversation_id != conversation_id
        ):
            raise ConversationReplayUnavailable(
                "CONVERSATION_REPLAY_IDENTITY_MISMATCH",
                "conversation_replay 页面身份与请求不匹配",
                "使用同一租户、产品和会话身份重试",
                reason="conversation_replay_identity_mismatch",
            )
        if page.after_cursor != expected_cursor or page.after_sequence != expected_sequence:
            raise ConversationReplayUnavailable(
                "CONVERSATION_REPLAY_CURSOR_INVALID",
                "conversation_replay 分页游标未连续推进",
                "从有效的 retained cursor 重新读取会话",
                reason="conversation_replay_cursor_invalid",
            )
        if page.gap_detected:
            raise ConversationReplayUnavailable(
                "CONVERSATION_REPLAY_GAP",
                "conversation_replay 检测到历史缺口",
                "重新建立会话或按 Core retention 策略补取缺失页",
                reason="conversation_replay_gap_detected",
            )
        for event in page.events:
            event_id = str(event.event_id)
            if event_id in seen_event_ids or event.sequence in seen_sequences:
                raise ConversationReplayUnavailable(
                    "CONVERSATION_REPLAY_DUPLICATE",
                    "conversation_replay 页面包含重复事件",
                    "从有效的 retained cursor 重新读取会话",
                    reason="conversation_replay_duplicate_event",
                )
            seen_event_ids.add(event_id)
            seen_sequences.add(event.sequence)
        pages.append(page)
        if not page.has_more:
            return pages
        next_cursor = page.next_cursor
        if not next_cursor or next_cursor == expected_cursor:
            raise ConversationReplayUnavailable(
                "CONVERSATION_REPLAY_CURSOR_INVALID",
                "conversation_replay 页面声明继续读取但没有新游标",
                "检查 Core replay 分页响应后重试",
                reason="conversation_replay_cursor_not_advanced",
            )
        expected_cursor = next_cursor
        if page.events:
            expected_sequence = page.events[-1].sequence
        elif page.next_cursor is None:
            raise ConversationReplayUnavailable(
                "CONVERSATION_REPLAY_CURSOR_INVALID",
                "conversation_replay 空页面没有可继续读取的游标",
                "检查 Core replay 分页响应后重试",
                reason="conversation_replay_empty_page_cursor_missing",
            )
    raise ConversationReplayUnavailable(
        "CONVERSATION_REPLAY_PAGE_LIMIT",
        "conversation_replay 分页超过读取上限",
        "缩小会话范围或分批读取 retained replay",
        reason="conversation_replay_page_limit",
    )


def read_conversation_replay(config: ConversationReplayReadConfig) -> list[Any]:
    """Fetch and validate all retained Core replay pages over HTTP.

    This is intentionally a bounded reader.  A response body, provider error,
    or non-replay page is converted to a public reason without echoing its
    body, and no page is cached locally as a substitute for Core persistence.
    """

    base_url = str(config.base_url or "").strip().rstrip("/")
    parsed = urlsplit(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ConversationReplayUnavailable(
            "CONVERSATION_REPLAY_CONFIG_INVALID",
            "conversation_replay Core 地址无效",
            "配置绝对 HTTP(S) Core replay 地址后重试",
            reason="conversation_replay_base_url_invalid",
        )
    if not str(config.auth_token or "").strip():
        raise ConversationReplayUnavailable(
            "CONVERSATION_REPLAY_AUTH_MISSING",
            "conversation_replay 读取缺少认证令牌",
            "配置与 Core 读取合同匹配的认证令牌后重试",
            reason="conversation_replay_auth_missing",
        )
    if isinstance(config.page_limit, bool) or not 1 <= config.page_limit <= 200:
        raise ValueError("page_limit must be between 1 and 200")

    path = (
        f"/application-conversations/{quote(config.product_id, safe='')}/"
        f"{quote(config.conversation_id, safe='')}/events"
    )

    def fetch(after_cursor: str | None) -> Mapping[str, Any]:
        query: dict[str, str] = {"limit": str(config.page_limit)}
        if after_cursor:
            query["after_cursor"] = after_cursor
        target = base_url + path + "?" + urlencode(query)
        headers = {"Accept": "application/json"}
        headers[config.auth_header] = config.auth_prefix + config.auth_token
        request = Request(target, headers=headers, method="GET")
        try:
            with urlopen(request, timeout=config.timeout_seconds) as response:
                raw = response.read(16 * 1024 * 1024 + 1)
                if len(raw) > 16 * 1024 * 1024:
                    raise ConversationReplayUnavailable(
                        "CONVERSATION_REPLAY_RESPONSE_TOO_LARGE",
                        "conversation_replay 响应超过大小上限",
                        "按 Core 分页合同缩小页面后重试",
                        reason="conversation_replay_response_too_large",
                    )
        except ConversationReplayUnavailable:
            raise
        except HTTPError as exc:
            raise ConversationReplayUnavailable(
                "CONVERSATION_REPLAY_READ_FAILED",
                "读取 Platform Core conversation_replay 失败",
                "检查 Core replay endpoint、主体授权与游标后重试",
                reason=f"http_{int(exc.code)}",
            ) from exc
        except (OSError, URLError) as exc:
            raise ConversationReplayUnavailable(
                "CONVERSATION_REPLAY_READ_FAILED",
                "读取 Platform Core conversation_replay 失败",
                "检查 Core replay endpoint、网络与认证配置后重试",
                reason="conversation_replay_read_transport_failed",
            ) from exc
        try:
            decoded = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ConversationReplayUnavailable(
                "CONVERSATION_REPLAY_PAGE_INVALID",
                "Platform Core 返回的 conversation_replay 页面不是有效 JSON",
                "检查 Core replay 页面合同后重试",
                reason="conversation_replay_json_invalid",
            ) from exc
        if not isinstance(decoded, Mapping):
            raise ConversationReplayUnavailable(
                "CONVERSATION_REPLAY_PAGE_INVALID",
                "Platform Core 返回的 conversation_replay 页面不是对象",
                "检查 Core replay 页面合同后重试",
                reason="conversation_replay_payload_invalid",
            )
        page = decoded.get("page")
        return page if isinstance(page, Mapping) else decoded

    return paginate_conversation_replay(
        fetch,
        tenant_id=config.tenant_id,
        product_id=config.product_id,
        conversation_id=config.conversation_id,
    )


__all__ = [
    "ConversationReplayTransportConfig",
    "ConversationReplayReadConfig",
    "ConversationReplayUnavailable",
    "paginate_conversation_replay",
    "parse_turn_projection",
    "publish_turn_projection",
    "read_conversation_replay",
    "require_conversation_replay",
]
