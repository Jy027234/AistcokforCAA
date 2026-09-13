"""Q0 positive/negative probe with CI-grade exit codes.

Every step declares an EXPECTATION, and the process exits non-zero if any
expectation is not met. A response that merely arrives is NOT a pass: the
positive loop must reach status == "completed".

The positive path uses the SANCTIONED cross-user mechanism. A product backend
authenticates with its own scoped service token (subject "aquant-backend") and
acts for an end user, so agentctl requires a signed context envelope
(frontdesk_messages.py:208-215). We therefore mint one exactly the way the base
repo's own tests do:

    ContextEnvelope.new(... issuer=PLATFORM_CORE_CONTEXT_ISSUER ...)
    payload["platform_context_signature"] = sign_platform_tool_context(payload, key=...)

The shared HMAC key comes from AGENTCTL_PLATFORM_CONTEXT_HMAC_KEY and must be
the same value on the server. The envelope is short-lived (300 s default), so it
is minted per run rather than cached.

This is the correct pattern for the real product: A-Quant Lab's backend holds
the issuing key and signs the end-user context; the model never sees it.

Exit codes: 0 = all expectations met, 1 = at least one unmet, 2 = setup error.

  $env:AGENTCTL_SERVICE_TOKEN='agt_...'
  $env:AGENTCTL_PLATFORM_CONTEXT_HMAC_KEY='...'
  python tests/acceptance/q0_probe.py --json-out deploy/agentctl-q0/q0-probe-evidence.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from pathlib import Path

from agentctl.aios import ContextEnvelope
from agentctl.embed import connect, static_tenant
from agentctl.platform_context_guard import (
    FRONTDESK_CONTEXT_AUDIENCE,
    PLATFORM_CORE_CONTEXT_ISSUER,
    sign_platform_tool_context,
)
from agentctl.sdk.client import AgentctlHTTPError, AgentctlResponseTooLargeError

TENANT = "aquant-synthetic"
PRODUCT = "aquant_lab"
SCOPE = ["frontdesk.message", "model_gateway.complete"]
END_USER = "syn-1"

RESULTS: list[dict] = []


def record(step: str, ok: bool, detail: object, note: str = "") -> None:
    RESULTS.append({"step": step, "ok": ok, "note": note, "detail": detail})
    print(f"[{'PASS' if ok else 'FAIL'}] {step}" + (f"  -- {note}" if note else ""))


def err(exc: BaseException) -> dict:
    out = {"exception": type(exc).__name__, "message": str(exc)[:600]}
    for a in ("status_code", "method", "path", "request_id", "payload", "limit_bytes"):
        if hasattr(exc, a):
            out[a] = getattr(exc, a)
    return out


def signed_envelope(key: str, *, request_id: str, conversation_id: str,
                    user_id: str = END_USER, tenant: str = TENANT,
                    product: str = PRODUCT) -> dict:
    # request_id / conversation_id must match the message body exactly:
    # consuming the envelope rejects a mismatch with
    # "frontdesk_context_request_id_mismatch" (context_envelope.py).
    env = ContextEnvelope.new(
        tenant_id=tenant,
        user_id=user_id,
        conversation_id=conversation_id,
        request_id=request_id,
        trace_id=request_id,
        issuer=PLATFORM_CORE_CONTEXT_ISSUER,
        audience=FRONTDESK_CONTEXT_AUDIENCE,
        grants=list(SCOPE),
        entitlements={"product_id": product},
        data_sensitivity="D1",
        risk_ceiling="R1",
        autonomy_ceiling="L1",
    )
    payload = env.to_dict()
    payload["platform_context_signature"] = sign_platform_tool_context(payload, key=key)
    return payload


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default=os.environ.get("AGENTCTL_BASE_URL", "http://127.0.0.1:8765"))
    ap.add_argument("--json-out", default="")
    args = ap.parse_args()

    token = os.environ.get("AGENTCTL_SERVICE_TOKEN", "").strip()
    hmac_key = os.environ.get("AGENTCTL_PLATFORM_CONTEXT_HMAC_KEY", "").strip()
    if not token:
        print("AGENTCTL_SERVICE_TOKEN is required", file=sys.stderr)
        return 2
    if not hmac_key:
        print("AGENTCTL_PLATFORM_CONTEXT_HMAC_KEY is required", file=sys.stderr)
        return 2

    # -- P1: positive loop must actually COMPLETE ---------------------------
    try:
        a = connect(args.base_url, mode="assist", api_key=token,
                    tenant_resolver=static_tenant(TENANT),
                    source_product=PRODUCT, source_product_version="0.0.1-q0")
        st = a.status()
        record("P1a_status_mode_matches",
               st.get("mode_compatible") is True
               and st.get("client_mode") == st.get("server_mode") == "assist", st)

        rid = "q0-req-" + uuid.uuid4().hex[:12]
        cid = "q0-conv-" + uuid.uuid4().hex[:8]
        r = a.invoke({"user_id": END_USER}, user_id=END_USER, text="你好，请只回复 pong",
                     permission_scope=SCOPE,
                     conversation_id=cid, request_id=rid,
                     context_envelope=signed_envelope(hmac_key, request_id=rid, conversation_id=cid))
        a.close()
        ok = r.get("status") == "completed"
        record("P1b_run_completed", ok, {
            "status": r.get("status"), "reply": str(r.get("reply"))[:200],
            "usage_records": r.get("usage_records"), "warnings": r.get("warnings")},
            "" if ok else f"Run did not complete: status={r.get('status')!r}")
        reply = str(r.get("reply") or "")
        record("P1c_reply_is_real", bool(reply) and "未完成" not in reply,
               {"reply_head": reply[:120]})
    except (AgentctlHTTPError, AgentctlResponseTooLargeError) as exc:
        record("P1_positive_loop", False, err(exc), "positive path raised")
    except Exception as exc:  # noqa: BLE001
        record("P1_positive_loop", False, err(exc), "unexpected error")

    # -- P2: cross-user without a signed envelope MUST be refused ------------
    try:
        b = connect(args.base_url, mode="assist", api_key=token,
                    tenant_resolver=static_tenant(TENANT), source_product=PRODUCT)
        rb = b.invoke({"user_id": END_USER}, user_id=END_USER, text="ping",
                      permission_scope=SCOPE)
        b.close()
        record("P2_unsigned_cross_user_refused", False,
               {"http": "accepted", "status": rb.get("status")},
               "acting for another user was allowed without a signed envelope")
    except AgentctlHTTPError as exc:
        payload = dict(exc.payload or {})
        ok = exc.status_code == 403 and payload.get("error") == "read_assist_actor_mismatch"
        record("P2_unsigned_cross_user_refused", ok,
               {"status_code": exc.status_code, "payload": payload})
    except Exception as exc:  # noqa: BLE001
        record("P2_unsigned_cross_user_refused", False, err(exc), "unexpected error")

    # -- N-series: each MUST be rejected ------------------------------------
    def must_reject(step, *, tenant=TENANT, product=PRODUCT, scopes=SCOPE, token_override=None):
        try:
            c = connect(args.base_url, mode="assist",
                        api_key=token if token_override is None else token_override,
                        tenant_resolver=static_tenant(tenant), source_product=product)
            rid = "q0-req-" + uuid.uuid4().hex[:12]
            cid = "q0-conv-" + uuid.uuid4().hex[:8]
            resp = c.invoke({"user_id": END_USER}, user_id=END_USER, text="ping",
                            permission_scope=scopes,
                            conversation_id=cid, request_id=rid,
                            context_envelope=signed_envelope(hmac_key, request_id=rid, conversation_id=cid))
            c.close()
            record(step, False, {"http": "accepted", "run_status": resp.get("status")},
                   "request was ACCEPTED but a rejection was required")
        except AgentctlHTTPError as exc:
            record(step, True, {"status_code": exc.status_code, "payload": dict(exc.payload or {})})
        except Exception as exc:  # noqa: BLE001
            record(step, False, err(exc), "non-HTTP error")

    must_reject("N1_no_token_rejected", token_override="")
    must_reject("N2_forged_tenant_rejected", tenant="aquant-attacker")

    print()
    print("#" * 68)
    print("# SUMMARY  (expectations, not observations)")
    print("#" * 68)
    for r in RESULTS:
        print(f"  {'PASS' if r['ok'] else 'FAIL'}  {r['step']}")
    failed = [r["step"] for r in RESULTS if not r["ok"]]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} expectations met")
    if failed:
        print("UNMET: " + ", ".join(failed))

    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps(RESULTS, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        print(f"\nwrote {args.json_out}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
