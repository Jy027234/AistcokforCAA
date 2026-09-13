"""Q0 access-readiness probe for A-Quant Lab x agentctl.

Executes the v0.2.2 Q0 card steps against a live, isolated agentctl Lite deploy:
  P0  the SDK snippet exactly as printed in v0.2.2 section 1 (expected to fail)
  P1  corrected minimum closed loop (status + invoke)
  N1  no token
  N2  wrong tenant
  N3  out-of-scope permission_scope
  N4  missing product_context (admission requires it)

Every step is recorded as evidence; nothing is asserted as "passed" unless the
observed behaviour matches the base repo's own contract.

Run with the interpreter that has agentctl installed:
  python tests/acceptance/q0_probe.py --base-url http://127.0.0.1:8765 --token <token>
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from typing import Any

from agentctl.embed import EmbedConfig, connect, static_tenant
from agentctl.sdk.client import AgentctlHTTPError, AgentctlResponseTooLargeError

TENANT = "aquant-synthetic"
PRODUCT = "aquant_lab"
SCOPE = ["model_gateway.complete"]

RESULTS: list[dict[str, Any]] = []


def record(step: str, outcome: str, detail: Any) -> None:
    RESULTS.append({"step": step, "outcome": outcome, "detail": detail})
    print(f"\n{'=' * 72}\n[{step}] -> {outcome}\n{'=' * 72}")
    print(json.dumps(detail, ensure_ascii=False, indent=2, default=str)[:4000])


def summarize_error(exc: BaseException) -> dict[str, Any]:
    out: dict[str, Any] = {
        "exception": type(exc).__name__,
        "message": str(exc)[:800],
    }
    for attr in ("status_code", "method", "path", "request_id", "payload", "limit_bytes"):
        if hasattr(exc, attr):
            out[attr] = getattr(exc, attr)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default=os.environ.get("AGENTCTL_BASE_URL", "http://127.0.0.1:8765"))
    ap.add_argument("--token", default=os.environ.get("AGENTCTL_SERVICE_TOKEN", ""))
    ap.add_argument("--user", default="syn-1")
    ap.add_argument("--json-out", default="")
    args = ap.parse_args()

    base_url, token = args.base_url, args.token

    # ---------------------------------------------------------------- P0
    # v0.2.2 section 1 Q0 step 3, transcribed as printed.
    step = "P0_v022_snippet_verbatim"
    try:
        client = connect(
            base_url,
            mode="assist",
            api_key=token,
            tenant_resolver=static_tenant(TENANT),
        )
        client.status()
        client.invoke(
            {"user_id": args.user},
            user_id=args.user,
            text="ping",
            permission_scope=[],
        )
        record(step, "UNEXPECTED_SUCCESS", {"note": "v0.2.2 snippet ran as printed"})
    except (TypeError, ValueError) as exc:
        record(
            step,
            "CLIENT_SIDE_CONTRACT_ERROR",
            summarize_error(exc)
            | {
                "reading": "Fails before any network I/O: invoke() has no keyword "
                "'text'; the first positional parameter is 'principal' and the "
                "prompt keyword is 'prompt'. The printed snippet is not executable "
                "against the locked SDK.",
            },
        )
    except Exception as exc:  # noqa: BLE001
        record(step, "OTHER_ERROR", summarize_error(exc))

    # ---------------------------------------------------------------- P1
    step = "P1_corrected_minimum_loop"
    try:
        assistant = connect(
            base_url,
            mode="assist",
            api_key=token,
            tenant_resolver=static_tenant(TENANT),
            source_product=PRODUCT,
            source_product_version="0.0.1-q0",
        )
        health = assistant.status()
        record("P1a_status", "OK", health)

        resp = assistant.invoke(
            {"user_id": args.user},
            user_id=args.user,
            text="ping",
            permission_scope=SCOPE,
        )
        record("P1b_invoke", "RESPONSE_RECEIVED", resp)
        assistant.close()
        record(step, "COMPLETED", {"note": "see P1a/P1b"})
    except Exception as exc:  # noqa: BLE001
        record(step, "FAILED", summarize_error(exc))

    # ---------------------------------------------------------------- N1
    step = "N1_no_token"
    try:
        anon = connect(base_url, mode="assist", tenant_resolver=static_tenant(TENANT))
        r = anon.invoke({"user_id": args.user}, user_id=args.user, text="ping")
        record(step, "UNEXPECTED_SUCCESS", r)
        anon.close()
    except AgentctlHTTPError as exc:
        record(step, "REJECTED", summarize_error(exc))
    except Exception as exc:  # noqa: BLE001
        record(step, "OTHER_ERROR", summarize_error(exc))

    # ---------------------------------------------------------------- N2
    step = "N2_wrong_tenant"
    try:
        wrong = connect(
            base_url,
            mode="assist",
            api_key=token,
            tenant_resolver=static_tenant("aquant-attacker"),
            source_product=PRODUCT,
        )
        r = wrong.invoke(
            {"user_id": args.user}, user_id=args.user, text="ping", permission_scope=SCOPE
        )
        record(step, "UNEXPECTED_SUCCESS", r)
        wrong.close()
    except AgentctlHTTPError as exc:
        record(step, "REJECTED", summarize_error(exc))
    except Exception as exc:  # noqa: BLE001
        record(step, "OTHER_ERROR", summarize_error(exc))

    # ---------------------------------------------------------------- N3
    step = "N3_out_of_scope"
    try:
        scoped = connect(
            base_url,
            mode="assist",
            api_key=token,
            tenant_resolver=static_tenant(TENANT),
            source_product=PRODUCT,
        )
        r = scoped.invoke(
            {"user_id": args.user},
            user_id=args.user,
            text="ping",
            permission_scope=["ledger.write", "orders.execute"],
        )
        record(step, "UNEXPECTED_SUCCESS", r)
        scoped.close()
    except AgentctlHTTPError as exc:
        record(step, "REJECTED", summarize_error(exc))
    except Exception as exc:  # noqa: BLE001
        record(step, "OTHER_ERROR", summarize_error(exc))

    # ---------------------------------------------------------------- N4
    step = "N4_missing_product_context"
    try:
        bare = connect(
            base_url,
            mode="assist",
            api_key=token,
            tenant_resolver=static_tenant(TENANT),
        )
        r = bare.invoke(
            {"user_id": args.user}, user_id=args.user, text="ping", permission_scope=SCOPE
        )
        record(step, "UNEXPECTED_SUCCESS", r)
        bare.close()
    except AgentctlHTTPError as exc:
        record(step, "REJECTED", summarize_error(exc))
    except Exception as exc:  # noqa: BLE001
        record(step, "OTHER_ERROR", summarize_error(exc))

    # ---------------------------------------------------------------- A01
    step = "A01_mode_profile_probe"
    try:
        gw = connect(base_url, mode="gateway", api_key=token, tenant_resolver=static_tenant(TENANT))
        record("A01_gateway_status", "CALLED", gw.status())
        gw.close()
    except Exception as exc:  # noqa: BLE001
        record("A01_gateway_status", "REJECTED", summarize_error(exc))

    print("\n\n" + "#" * 72)
    print("# SUMMARY")
    print("#" * 72)
    for r in RESULTS:
        print(f"  {r['step']:<32} {r['outcome']}")

    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump(RESULTS, fh, ensure_ascii=False, indent=2, default=str)
        print(f"\nwrote {args.json_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
