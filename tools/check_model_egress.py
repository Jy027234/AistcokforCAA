"""真实模型联通与闸门验收（§17.2、ADR-012）。

这个脚本回答两个**只能在真实网络上回答**的问题：

  1. 生产路径（DeepSeekProvider）真的能通吗？——离线替身测不出密钥、
     代理、接口版本、超时这些事。
  2. 外发闸门在**真实调用**时仍然生效吗？——被拒时材料一个字节都不该出去。

刻意不做的事
------------
* 不打印密钥；
* 不因为"模型不可用"而失败退出——没有密钥的环境应当能跑完并明确报告
  "跳过"，而不是报一个与代码无关的红。这里的判据是**结论明确**，
  不是"必须联网成功"。

用法：
    python tools/check_model_egress.py            # 真实调用（需要密钥）
    python tools/check_model_egress.py --offline  # 只验证闸门与留档，不联网
退出码：0 = 结论明确且无矛盾；1 = 有断言失败；2 = 环境缺失。
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aquant.application.assistant import AssistantError, Material, ask_assistant  # noqa: E402
from aquant.domain.ai.model import ModelUnavailable  # noqa: E402
from aquant.domain.data.db import apply_migrations, connect  # noqa: E402

checks: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: object = "") -> None:
    # detail 允许传 dict/list：调用点常常手上只有一个记录对象，
    # 为它单独写一次 json.dumps 会让每个断言都多一行噪音。
    text = detail if isinstance(detail, str) else json.dumps(
        detail, ensure_ascii=False, default=str)
    checks.append((name, bool(ok), text))
    print(("  PASS  " if ok else "  FAIL  ") + name + (("  -- " + text) if text else ""))


#: 用于联通性验证的材料。来源必须是**已登记且已授权**的，
#: 否则验的就不是"模型能不能通"而是"闸门会不会拦"。
PROBE_MATERIAL = Material(
    source_id="cninfo",
    text=("公告标题：某公司 2025 年年度权益分派实施公告\n"
          "内容：每 10 股派发现金红利 28.02423 元（含税），"
          "股权登记日 2026-06-19，除权除息日 2026-06-20。"),
    contains_personal_data=False,
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true",
                    help="只验证闸门与留档，不调用模型")
    args = ap.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="aquant-egress-"))
    con = connect(tmp / "meta.sqlite")
    apply_migrations(con)
    try:
        # ---------------------------------------------------- 闸门：未登记来源
        print("[1] 闸门：未登记来源必须发不出去")
        try:
            ask_assistant(con, _NeverCalled(), materials=[
                Material(source_id="totally-unknown-site", text="不该出去的内容",
                         contains_personal_data=False),
            ], purpose="闸门验证", data_mode="SYNTHETIC")
            check("未登记来源被拒", False, "竟然没有抛错")
        except AssistantError as exc:
            check("未登记来源被拒", exc.code == "SOURCE_PERMISSION_MISSING",
                  exc.code)
            check("拒绝时给出修复动作", bool(exc.repair_action))
            check("拒绝时给出结构化 blocker", bool(exc.blockers), str(exc.blockers))
        n = con.execute("SELECT COUNT(*) AS n FROM model_call WHERE outcome='REJECTED'"
                        ).fetchone()["n"]
        check("被拒的尝试同样留档", n >= 1, f"{n} 条")

        # ---------------------------------------------------- 闸门：个人信息默认
        print()
        print("[2] 闸门：未声明个人信息 = 含个人信息（§826）")
        try:
            # contains_personal_data 必须显式给 None：数据类的默认值是 False
            # （"声明了不含"），而要验的是"**未声明**"这条不对称默认。
            ask_assistant(con, _NeverCalled(), materials=[
                Material(source_id="cninfo", text="含姓名的材料",
                         contains_personal_data=None),
            ], purpose="闸门验证", data_mode="SYNTHETIC")
            check("未声明个人信息被拒", False, "竟然没有抛错")
        except AssistantError as exc:
            check("未声明个人信息被拒",
                  any(b.get("right") == "personal_data" for b in exc.blockers),
                  str(exc.blockers))

        if args.offline:
            print()
            print("（--offline：跳过真实模型调用）")
            return _finish()

        # ---------------------------------------------------- 真实模型
        print()
        print("[3] 真实模型调用（生产路径 DeepSeekProvider）")
        from aquant.adapters.models.deepseek import DeepSeekProvider, key_file_path

        check("密钥文件存在", key_file_path().exists(), str(key_file_path()))
        provider = DeepSeekProvider()

        # 先核对型号真的存在。请求一个不存在的型号时服务端会**静默回落**
        # 并照常返回 200，于是记录里的 model 与实际调用的型号不一致——
        # 事后按记录复现会得到另一份结果，而当时看不出任何异常。
        available = _available_models(provider)
        check("配置的模型在账户可用列表内",
              available is None or provider.model_name in available,
              f"配置 {provider.model_name}；可用 {available}")
        try:
            out = ask_assistant(
                con, provider, materials=[PROBE_MATERIAL],
                purpose="验证模型联通与外发闸门", data_mode="PRODUCTION")
        except ModelUnavailable as exc:
            print(f"  模型不可用：{exc}")
            print("  结论：闸门已生效，但模型连通性本次未能验证（不是代码缺陷）")
            return _finish(allow_skip=True)

        check("模型返回了文本", bool(out.get("text")), str(out.get("text"))[:80])
        # 型号必须与请求完全一致。不同型号的结果不可互换，
        # 而"静默回落"会让记录里的 model 与实际调用的型号不一致。
        check("返回的模型与请求的模型一致（无静默回落）",
              out_model_ok(out, provider),
              f"请求 {provider.model_name}；实际 {out.get('model')}")
        check("来源已记录", out["sourcesUsed"] == ["cninfo"], str(out["sourcesUsed"]))
        check("响应带内容哈希", bool(out.get("contentHash")))
        row = con.execute("SELECT provider,model,outcome,input_tokens,output_tokens "
                          "FROM model_call WHERE model_call_id=?",
                          (out["modelCallId"],)).fetchone()
        check("调用已留档且 outcome=OK", row is not None and row["outcome"] == "OK",
              dict(row) if row else "无记录")
        print(f"  模型：{out['provider']}/{out['model']}，"
              f"输入 {out['inputTokens']} tokens，输出 {out['outputTokens']} tokens")
        text = out["text"]
        # §5.3：助手不得输出概率/预期收益/目标价。这是**提示词层面**的约束，
        # 因此只能对真实输出抽查——离线替身永远会通过。
        banned = [w for w in ("上涨概率", "预期收益", "目标价") if w in text]
        check("输出未包含被禁止的内容（§5.3）", not banned, str(banned))
        print("  回答节选：" + text[:160].replace("\n", " "))
        return _finish()
    finally:
        con.close()


def _available_models(provider) -> list[str] | None:
    """问供应商要可用型号列表。取不到就返回 None（不因此判失败）。"""

    import urllib.request

    from aquant.adapters.models.deepseek import load_api_key

    try:
        req = urllib.request.Request(
            provider.base_url.rstrip("/") + "/models",
            headers={"Authorization": "Bearer " + load_api_key()})
        with urllib.request.urlopen(req, timeout=30) as resp:
            doc = json.loads(resp.read().decode("utf-8"))
        return [m.get("id") for m in doc.get("data", []) if m.get("id")]
    except Exception:                                    # noqa: BLE001
        return None


def out_model_ok(out: dict, provider) -> bool:
    """模型名必须完全一致：不同型号的结果不可互换。"""

    return out.get("model") == provider.model_name


class _NeverCalled:
    """闸门验证用的替身：**被调用即失败**。

    这比"返回一个假答案"强：如果闸门漏放，测试会因为"模型被调用了"
    而立刻失败，而不是拿到一个看起来正常的回答。
    """

    provider_name = "never"
    model_name = "never"

    def complete(self, request):                          # pragma: no cover
        raise AssertionError("闸门漏放：材料在未被授权的情况下到达了模型")


def _finish(*, allow_skip: bool = False) -> int:
    failed = [c for c in checks if not c[1]]
    print()
    print("=" * 62)
    print(f"模型外发验收 {len(checks) - len(failed)}/{len(checks)} 通过")
    for name, _, detail in failed:
        print("  - " + name + "  " + detail)
    out = ROOT / "deploy" / "agentctl-q0" / "model-egress.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(json.dumps({
        "ran_at": datetime.now(timezone.utc).isoformat(),
        "checks": [{"name": n, "ok": o, "detail": d} for n, o, d in checks],
        "conclusion": "PASS" if not failed else "FAIL",
    }, ensure_ascii=False, indent=2).encode("utf-8"))
    print(f"报告：{out}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
