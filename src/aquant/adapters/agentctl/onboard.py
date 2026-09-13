"""Register A-Quant Lab in the agentctl Product Integration Registry.

Why this exists
---------------
The Q0 probe proved that `agentctl serve --config <runtime.config.yaml>` runs
with `product_integration.admission.enforce_bindings: true`. A manifest applied
via `agentctl integration apply` creates the *product* and its *operations*,
but admission additionally requires an active `ProductCapabilityBinding` for the
exact (product_id, product_operation, tenant_id, agent_id) tuple. Without it the
frontdesk path fails closed with HTTP 403:

    no active ProductCapabilityBinding matches this product operation

This module performs that onboarding step explicitly and idempotently, mirroring
the shape agentctl itself uses for built-in platform apps
(`product_integration/__init__.py:_seed_platform_app_integrations`).

Scope and safety
----------------
* Only `frontdesk.message` is bound. That grants the product the right to submit
  a governed assistant turn -- nothing about orders, ledgers, SQL, shell or
  arbitrary fetch.
* The binding is tenant-scoped. `allowed_tenants` is NOT `*`, so a forged tenant
  cannot ride on this registration (acceptance case A03).
* Idempotent: existing records are left untouched, so repeated runs are safe.

Usage (needs an interpreter with agentctl importable):
    python -m aquant.adapters.agentctl.onboard --config <runtime.config.yaml>
    python -m aquant.adapters.agentctl.onboard --config <...> --json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from agentctl.config import load_runtime_config
from agentctl.product_integration import build_product_integration_store
from agentctl.product_integration.models import (
    ProductCapabilityBinding,
    ProductIntegrationRecord,
    ProductOperationSpec,
)

PRODUCT_ID = "aquant_lab"
DISPLAY_NAME = "A-Quant Lab"
TENANT_ID = "aquant-synthetic"
FRONTDESK_AGENT_ID = "frontdesk"


def onboard(config_path: Path, *, tenant_id: str = TENANT_ID) -> dict:
    config = load_runtime_config(config_path)
    store = build_product_integration_store(config)
    created: list[str] = []
    existing: list[str] = []

    # 1. product record ------------------------------------------------------
    if store.get_product(PRODUCT_ID) is None:
        store.save_product(ProductIntegrationRecord(
            product_id=PRODUCT_ID,
            display_name=DISPLAY_NAME,
            product_type="custom",
            owner="aquant_lab",
            environments=["dev", "pilot"],
            default_tenant_strategy="external_tenant_mapped",
            supported_contracts=[
                "product_context",
                "product_capabilities.v1",
                "frontdesk",
                "model_gateway.complete",
            ],
            allowed_tenants=[tenant_id],   # NOT "*" -- see module docstring
            status="pilot_ready",          # required_stage in the policy is pilot_ready
            source="aquant_lab.onboard",
            aliases=["aquant", "a-quant-lab"],
            metadata={"managed_by": "aquant.adapters.agentctl.onboard"},
        ))
        created.append("product:" + PRODUCT_ID)
    else:
        existing.append("product:" + PRODUCT_ID)

    # 2. frontdesk.message operation ----------------------------------------
    if store.get_operation(PRODUCT_ID, "frontdesk.message") is None:
        store.save_operation(ProductOperationSpec(
            product_id=PRODUCT_ID,
            product_operation="frontdesk.message",
            display_name="A-Quant Lab assistant turn",
            description=(
                "Allows the A-Quant Lab backend to submit one governed assistant "
                "turn. No order, ledger, SQL, shell or arbitrary-fetch authority."
            ),
            execution_mode="frontdesk.message",
            required_scopes=["frontdesk.message"],
            default_artifact_type="assistant_turn_result",
            risk_level="R1",
            data_sensitivity="D1",
            status="active",
            metadata={"source": "aquant_lab.onboard", "definition_owner": "agentctl.frontdesk"},
        ))
        created.append("operation:frontdesk.message")
    else:
        existing.append("operation:frontdesk.message")

    # 3. binding ------------------------------------------------------------
    binding_id = "bind_aquant_lab_frontdesk_message"
    if store.get_binding(PRODUCT_ID, binding_id) is None:
        store.save_binding(ProductCapabilityBinding(
            binding_id=binding_id,
            product_id=PRODUCT_ID,
            product_operation="frontdesk.message",
            tenant_id=tenant_id,
            agent_id=FRONTDESK_AGENT_ID,
            risk_level="R1",
            data_sensitivity="D1",
            status="active",
            metadata={"source": "aquant_lab.onboard"},
        ))
        created.append("binding:" + binding_id)
    else:
        existing.append("binding:" + binding_id)

    return {
        "kind": "AQuantLabOnboardingReport",
        "product_id": PRODUCT_ID,
        "tenant_id": tenant_id,
        "created": created,
        "already_present": existing,
        "status": "pass",
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--tenant", default=TENANT_ID)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    report = onboard(Path(args.config), tenant_id=args.tenant)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"AQuantLabOnboardingReport: {report['status'].upper()}")
        print(f"product: {report['product_id']}  tenant: {report['tenant_id']}")
        print(f"created: {report['created'] or '(none)'}")
        print(f"present: {report['already_present'] or '(none)'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
