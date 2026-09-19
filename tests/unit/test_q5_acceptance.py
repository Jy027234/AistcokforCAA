from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.acceptance import q5_acceptance as q5


def test_q5_matrix_is_complete_and_starts_uncovered() -> None:
    checks = q5._check_matrix()

    assert [item.case for item in checks] == [f"A{i:02d}" for i in range(1, 17)]
    assert all(item.status == "uncovered" for item in checks)
    assert all(item.evidence_kind == "not_executed" for item in checks)


def test_redact_removes_nested_credentials() -> None:
    payload = {
        "stdout": "token=secret-token",
        "nested": ["secret-hmac", {"value": "secret-token"}],
    }

    redacted = q5.redact(payload, ("secret-token", "secret-hmac"))

    assert redacted == {
        "stdout": "token=<redacted>",
        "nested": ["<redacted>", {"value": "<redacted>"}],
    }


def test_profile_mismatch_does_not_promote_local_exception_to_refusal(monkeypatch) -> None:
    class FakeClient:
        def status(self):
            return {
                "mode_compatible": False,
                "client_mode": "growth",
                "server_mode": "assist",
            }

        def invoke(self, *_args, **_kwargs):
            raise ValueError("client-side validation")

        def close(self):
            pass

    monkeypatch.setattr(q5, "connect", lambda *_args, **_kwargs: FakeClient())

    result = q5._profile_mismatch_probe("http://example.invalid", "token", "hmac")

    assert result["status_mismatch"] is True
    assert result["explicit_refusal"] is False
    assert result["refusal_evidence"] == "status_only_or_non_http_failure"
    assert result["invocation"]["transport"] == "client_or_network_error"


def test_profile_mismatch_requires_live_http_4xx(monkeypatch) -> None:
    from agentctl.sdk.client import AgentctlHTTPError

    class FakeClient:
        def status(self):
            return {"mode_compatible": False, "client_mode": "growth", "server_mode": "assist"}

        def invoke(self, *_args, **_kwargs):
            raise AgentctlHTTPError(
                status_code=403,
                method="POST",
                path="/frontdesk/messages",
                request_id="r",
                payload={"error": "profile_mode_refused"},
            )

        def close(self):
            pass

    monkeypatch.setattr(q5, "connect", lambda *_args, **_kwargs: FakeClient())

    result = q5._profile_mismatch_probe("http://example.invalid", "token", "hmac")

    assert result["explicit_refusal"] is True
    assert result["refusal_evidence"] == "http_4xx"


def test_research_summary_requires_snapshot_data_and_execution_receipt() -> None:
    probe = {
        "accepted": True,
        "http_status": 200,
        "arguments": {"snapshot_id": "snap-universe", "instrument_id": "SH.600519"},
        "response": {
            "capability_id": q5.RESEARCH_CAPABILITY_ID,
            "invocation_id": "invoke-1",
            "trace_id": "trace-1",
            "idempotency_key": "idem-1",
            "output": {
                "ok": True,
                "snapshot_id": "snap-universe",
                "as_of_time": "2026-09-14T07:00:00+00:00",
                "data_mode": "PRODUCTION",
                "watermark": "REAL",
                "instrument_id": "SH.600519",
                "display_name": "贵州茅台",
                "factors": [],
                "limitations": ["read only"],
            },
        },
    }

    summary = q5._summarize_research_capability_probe(probe)

    assert summary["research_evidence_observed"] is True
    assert summary["snapshot_observed"] is True
    assert summary["data_observed"] is True
    assert summary["execution_associated"] is True

    probe["response"].pop("trace_id")
    assert q5._summarize_research_capability_probe(probe)["research_evidence_observed"] is False


def test_secret_scan_covers_runtime_log_dist_and_final_outputs(tmp_path: Path) -> None:
    capabilities = tmp_path / "capabilities.yaml"
    config = tmp_path / "runtime.yaml"
    log = tmp_path / "server.log"
    dist = tmp_path / "dist"
    evidence = tmp_path / "evidence.json"
    report = tmp_path / "report.md"
    for path in (capabilities, config, log, evidence, report):
        path.write_text("safe", encoding="utf-8")
    dist.mkdir()
    (dist / "bundle.js").write_text("const x='agt_abcdefghijklmnop';", encoding="utf-8")

    clean = q5._static_secret_scan(
        capabilities,
        config,
        evidence=evidence,
        report=report,
        server_log=log,
        frontend_dist=dist,
        exact_secrets=("runtime-hmac",),
    )
    assert clean["clean"] is False
    assert any("agt-key-shape" in item for item in clean["hits"])

    log.write_text("runtime-hmac", encoding="utf-8")
    dirty = q5._static_secret_scan(
        capabilities,
        config,
        evidence=evidence,
        report=report,
        server_log=log,
        frontend_dist=dist,
        exact_secrets=("runtime-hmac",),
    )
    assert dirty["exact_secret_hits"] == ["server.log:runtime_secret"]


def test_isolated_config_redirects_all_runtime_stores(tmp_path: Path) -> None:
    config_path = q5._isolated_config(
        q5.DEFAULT_CONFIG,
        tmp_path / "aquant-q5-runtime-test",
    )
    text = config_path.read_text(encoding="utf-8")

    assert "runtime.config.yaml" in str(config_path)
    assert "tokens.sqlite" in text
    assert "runs.sqlite" in text
    assert str(q5.DEFAULT_CONFIG.parent) not in text


def test_enforcement_summary_requires_all_five_cases() -> None:
    payload = {
        "results": [
            {"case": "N0_baseline_correct", "ok": True, "got": "accepted"},
            {"case": "N2_forged_tenant", "ok": True},
            {"case": "N4_wrong_allowed_product", "ok": True},
            {"case": "N5_missing_product_context", "ok": True},
            {"case": "X1_token_without_frontdesk_message_rejected", "ok": True},
        ],
        "unmet": [],
    }

    ok, detail = q5._summarize_q0_enforcement(payload)

    assert ok is True
    assert detail["passed"] == detail["total"] == 5


def test_enforcement_summary_does_not_accept_incomplete_evidence() -> None:
    ok, detail = q5._summarize_q0_enforcement(
        {"results": [{"case": "N2_forged_tenant", "ok": True}]}
    )

    assert ok is False
    assert detail["total"] == 1


def test_a05_a09_keep_uncovered_with_explicit_manifest_blockers() -> None:
    blockers = q5._q5_domain_blockers(q5.DEFAULT_CAPABILITIES)

    assert list(blockers) == ["A05", "A06", "A07", "A08", "A09"]
    assert all(item["blocker"] is True for item in blockers.values())
    for detail in blockers.values():
        if detail["missing_capabilities"]:
            assert detail["blocker_code"] == "capability_not_declared"
        elif detail["missing_handlers"]:
            assert detail["blocker_code"] == "handler_not_declared"
        else:
            # A declaration alone is not acceptance evidence.  Keep the row
            # uncovered until the product-owned entrypoint is exercised.
            assert detail["blocker_code"] == "product_entrypoint_not_bound"

    checks = q5._check_matrix()
    q5._apply_q5_domain_blockers(checks, blockers)
    by_case = {item.case: item for item in checks}
    for case in ("A05", "A06", "A07", "A08", "A09"):
        assert by_case[case].status == "uncovered"
        assert by_case[case].evidence_kind == "not_executed"
        assert by_case[case].detail["blocker"] is True

    rendered = q5.render_report(
        {
            "generated_at": "2026-09-19T00:00:00+00:00",
            "topology": "live_http",
            "server": {"host": "127.0.0.1", "port": 18765, "stopped": True},
            "credentials": {"revoked": True},
            "blockers": blockers,
            "checks": [item.to_dict() for item in checks],
        }
    )
    assert "A05–A09 真实拓扑阻断" in rendered
    assert "aquant.event_evidence.read" in rendered
    assert "aquant.simulation_plan.preview" in rendered
    assert "不能冒充 agentctl 真实拓扑通过" in rendered


def test_render_report_lists_all_cases_and_distinguishes_scope() -> None:
    report = {
        "generated_at": "2026-09-19T00:00:00+00:00",
        "topology": "live_http",
        "server": {"host": "127.0.0.1", "port": 18765, "stopped": True},
        "credentials": {"revoked": True},
        "checks": [
            item.to_dict()
            for item in q5._check_matrix()
        ],
    }
    report["checks"][0]["status"] = "passed"
    report["checks"][0]["evidence_kind"] = "live_topology"
    report["checks"][1]["evidence_kind"] = "offline_contract"
    rendered = q5.render_report(report)

    assert rendered.count("| A") == 16
    assert "live_topology" in rendered
    assert "offline_contract" in rendered
    assert "A01–A16" in rendered
    assert "历史 Q0/Q5 文件不作为本次结论输入" in rendered


def test_port_occupied_error_is_distinct() -> None:
    assert issubclass(q5.PortOccupiedError, RuntimeError)
    assert q5.EXIT_PORT_BUSY != q5.EXIT_SETUP
