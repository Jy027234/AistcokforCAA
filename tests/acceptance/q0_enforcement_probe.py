"""Q0 negative/positive enforcement probe: tenant, product, scope boundaries.

Determines whether a forged tenant, wrong allowed_product, out-of-scope
permission_scope, or a missing product_context are rejected by the server, and
whether the tenant that reaches the runstore still matches the token's tenant.

The token is read from the AGENTCTL_SERVICE_TOKEN environment variable and is
never written to disk, logs or the JSON evidence file.

  $env:AGENTCTL_SERVICE_TOKEN='agt_...'
  python tests/acceptance/q0_enforcement_probe.py --json-out deploy/agentctl-q0/q0-enforcement-evidence.json
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

from agentctl.embed import connect, static_tenant
from agentctl.sdk.client import AgentctlHTTPError

TENANT = "aquant-synthetic"
PRODUCT = "aquant_lab"


def probe(base: str, token: str, tenant: str, product: str | None, scopes: list[str]) -> dict:
    try:
        c = connect(base, mode="assist", api_key=token,
                    tenant_resolver=static_tenant(tenant), source_product=product)
        resp = c.invoke({"user_id": "syn-1"}, user_id="syn-1", text="ping",
                        permission_scope=scopes)
        c.close()
        return {
            "http": "accepted",
            "run_status": resp.get("status"),
            "tenant_echo": resp.get("tenant"),
            "reply_head": str(resp.get("reply") or "")[:160],
        }
    except AgentctlHTTPError as exc:
        return {"http": "REJECTED", "status_code": exc.status_code,
                "payload": dict(exc.payload or {})}
    except Exception as exc:  # noqa: BLE001
        return {"http": "ERROR", "type": type(exc).__name__, "message": str(exc)[:300]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default=os.environ.get("AGENTCTL_BASE_URL", "http://127.0.0.1:8765"))
    ap.add_argument("--runs-db", default="deploy/agentctl-q0/.agentctl/data/runs.sqlite")
    ap.add_argument("--json-out", default="")
    args = ap.parse_args()

    token = os.environ.get("AGENTCTL_SERVICE_TOKEN", "").strip()
    if not token:
        print("AGENTCTL_SERVICE_TOKEN is required", file=sys.stderr)
        return 2

    cases = [
        ("N0_baseline_correct_tenant_and_product", TENANT, PRODUCT, ["model_gateway.complete"]),
        ("N2_forged_tenant", "aquant-attacker", PRODUCT, ["model_gateway.complete"]),
        ("N4_wrong_allowed_product", TENANT, "evil_product", ["model_gateway.complete"]),
        ("N3_out_of_scope", TENANT, PRODUCT, ["ledger.write", "execute_order"]),
        ("N5_no_product_context", TENANT, None, ["model_gateway.complete"]),
    ]

    results = []
    for label, tenant, product, scopes in cases:
        out = probe(args.base_url, token, tenant, product, scopes)
        out.update({"case": label, "requested_tenant": tenant, "requested_product": product})
        results.append(out)
        print(f"{label:<44} -> {out['http']:<9} {out.get('run_status') or out.get('status_code') or ''}")

    print()
    print("runstore tenants:")
    p = Path(args.runs_db)
    if p.exists():
        con = sqlite3.connect(p)
        for tenant, n, statuses in con.execute(
            "select tenant, count(*), group_concat(distinct status) from runs group by tenant"
        ):
            print(f"    tenant={tenant!r:<22} runs={n} statuses={statuses}")
        con.close()
    else:
        print("    (runs db not found)")

    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nwrote {args.json_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
