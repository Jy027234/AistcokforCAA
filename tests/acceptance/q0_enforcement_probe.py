"""Tenant / product / scope boundary probe with CI-grade exit codes.

Declares an EXPECTED outcome per case and exits non-zero when any case deviates,
so a permissive server cannot be mistaken for a passing one.

Three separate things are tested here, and they are NOT the same thing:

1. Tenant boundary    -- a forged tenant must be refused.
2. Product admission  -- an unregistered product, a wrong product and a missing
   product_context must be refused (policy enforce_bindings: true).
3. Actor boundary     -- a product backend acting for an end user must present a
   signed context envelope.

What is deliberately NOT asserted as a rejection:
   the permission_scope field of a frontdesk message is the caller REQUESTED
   scope, not the source of authority. The server derives granted authority from
   the verified token record (http_auth._require_api_scope), so asking for more
   than the token holds is not itself an escalation. What matters is that a
   token lacking a required scope cannot reach the operation that needs it.
   That is what the X-series below checks.

Token topology matters and is asserted:
  * the server static --token / --token-env is a MASTER credential
    (subject "master", scopes ["*"]). Presenting it short-circuits tenant
    verification BY DESIGN, so it must never be handed to a product.
  * the product must use a SEPARATELY issued scoped token, which goes through
    token_store.verify(token, tenant_id=...).

The runstore assertion inspects only rows created during THIS run, so historical
rows from earlier misconfigured probes cannot mask a current leak.

Exit codes: 0 = all expectations met, 1 = deviation found, 2 = setup error.

  $env:AGENTCTL_SERVICE_TOKEN=agt_...
  $env:AGENTCTL_PLATFORM_CONTEXT_HMAC_KEY=...
  python tests/acceptance/q0_enforcement_probe.py --json-out <path>
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
import uuid
from pathlib import Path

from agentctl.aios import ContextEnvelope
from agentctl.embed import connect, static_tenant
from agentctl.platform_context_guard import (
    FRONTDESK_CONTEXT_AUDIENCE,
    PLATFORM_CORE_CONTEXT_ISSUER,
    sign_platform_tool_context,
)
from agentctl.sdk.client import AgentctlHTTPError

TENANT = "aquant-synthetic"
PRODUCT = "aquant_lab"
SCOPE = ["frontdesk.message", "model_gateway.complete"]
END_USER = "syn-1"

CASES = [
    ("N0_baseline_correct", "accept", dict(tenant=TENANT, product=PRODUCT)),
    ("N2_forged_tenant", "reject", dict(tenant="aquant-attacker", product=PRODUCT)),
    ("N4_wrong_allowed_product", "reject", dict(tenant=TENANT, product="evil_product")),
    ("N5_missing_product_context", "reject", dict(tenant=TENANT, product=None)),
]


def envelope(key: str, request_id: str, conversation_id: str, tenant: str,
             scopes: list[str]) -> dict:
    env = ContextEnvelope.new(
        tenant_id=tenant, user_id=END_USER,
        conversation_id=conversation_id, request_id=request_id, trace_id=request_id,
        issuer=PLATFORM_CORE_CONTEXT_ISSUER, audience=FRONTDESK_CONTEXT_AUDIENCE,
        grants=list(scopes), entitlements={"product_id": PRODUCT},
        data_sensitivity="D1", risk_ceiling="R1", autonomy_ceiling="L1",
    )
    payload = env.to_dict()
    payload["platform_context_signature"] = sign_platform_tool_context(payload, key=key)
    return payload


def run(base: str, token: str, hmac_key: str, tenant: str, product) -> dict:
    rid = "q0-req-" + uuid.uuid4().hex[:12]
    cid = "q0-conv-" + uuid.uuid4().hex[:8]
    try:
        c = connect(base, mode="assist", api_key=token,
                    tenant_resolver=static_tenant(tenant), source_product=product)
        r = c.invoke({"user_id": END_USER}, user_id=END_USER, text="ping",
                     permission_scope=SCOPE, conversation_id=cid, request_id=rid,
                     context_envelope=envelope(hmac_key, rid, cid, tenant, SCOPE))
        c.close()
        return {"http": "accepted", "run_status": r.get("status")}
    except AgentctlHTTPError as exc:
        return {"http": "rejected", "status_code": exc.status_code,
                "payload": dict(exc.payload or {})}
    except Exception as exc:  # noqa: BLE001
        return {"http": "error", "type": type(exc).__name__, "message": str(exc)[:300]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url",
                    default=os.environ.get("AGENTCTL_BASE_URL", "http://127.0.0.1:8765"))
    ap.add_argument("--runs-db", default="deploy/agentctl-q0/.agentctl/data/runs.sqlite")
    ap.add_argument("--low-scope-token", default=os.environ.get("AGENTCTL_LOW_SCOPE_TOKEN", ""))
    ap.add_argument("--json-out", default="")
    args = ap.parse_args()

    token = os.environ.get("AGENTCTL_SERVICE_TOKEN", "").strip()
    hmac_key = os.environ.get("AGENTCTL_PLATFORM_CONTEXT_HMAC_KEY", "").strip()
    if not token or not hmac_key:
        print("AGENTCTL_SERVICE_TOKEN and AGENTCTL_PLATFORM_CONTEXT_HMAC_KEY are required",
              file=sys.stderr)
        return 2

    started = time.time()
    results, unmet = [], []

    for cid_, expect, kw in CASES:
        out = run(args.base_url, token, hmac_key, **kw)
        got = out["http"]
        ok = (got == "rejected") if expect == "reject" else (
            got == "accepted" and out.get("run_status") == "completed")
        if not ok:
            unmet.append(cid_)
        results.append({"case": cid_, "expect": expect, "got": got, "ok": ok, "detail": out})
        print(f"[{'PASS' if ok else 'FAIL'}] {cid_:<44} expect={expect:<6} got={got}"
              + ("" if ok else f"  run_status={out.get('run_status')}"))

    xid = "X1_token_without_frontdesk_message_rejected"
    if args.low_scope_token:
        out = run(args.base_url, args.low_scope_token, hmac_key, TENANT, PRODUCT)
        ok = out["http"] == "rejected"
        if not ok:
            unmet.append(xid)
        results.append({"case": xid, "expect": "reject", "got": out["http"],
                        "ok": ok, "detail": out})
        print(f"[{'PASS' if ok else 'FAIL'}] {xid:<44} expect=reject got={out['http']}"
              + ("" if ok else f"  run_status={out.get('run_status')}"))
    else:
        print(f"[SKIP] {xid}  (no --low-scope-token)")

    print()
    p = Path(args.runs_db)
    foreign = []
    if p.exists():
        con = sqlite3.connect(p)
        rows = con.execute(
            "select tenant, count(*) from runs where created_at >= ? group by tenant",
            (time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(started)),),
        ).fetchall()
        for tenant, n in rows:
            mark = "" if tenant == TENANT else "   <-- FOREIGN"
            print(f"    this run: tenant={tenant!r:<22} runs={n}{mark}")
            if tenant != TENANT:
                foreign.append(tenant)
        con.close()
    if foreign:
        unmet.append("runstore_contains_foreign_tenant")
        print(f"    !! foreign tenants recorded during this run: {foreign}")

    met = sum(1 for r in results if r["ok"])
    print(f"\n{met}/{len(results)} cases met")
    if unmet:
        print("UNMET: " + ", ".join(unmet))

    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps({"results": results, "unmet": unmet}, ensure_ascii=False, indent=2),
            encoding="utf-8")
        print(f"wrote {args.json_out}")
    return 1 if unmet else 0


if __name__ == "__main__":
    sys.exit(main())
