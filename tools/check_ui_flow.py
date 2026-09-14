"""界面写路径检查：真实浏览器 + 隔离账本。

为什么需要这一层
----------------
后端测试用 TestClient，它跑在同一个进程里、共享同一个连接，因此
**证明不了**这些事：

  * 前端真的把请求发出去了（而不是只更新了本地状态）；
  * 前端读得懂服务端的应答字段；
  * 冻结、执行、估值、对账在真实往返之后仍然一致。

而且真实浏览器会暴露只在浏览器里成立的问题。这个脚本第一次运行时
就抓到一个：重复冻结同一份计划时，界面点一次"确认并冻结"会拿到
HTTP 500——因为 `simulation_plan.idempotency_key` 的唯一约束直接
冒到了 API 层。那个 bug 在 TestClient 里也能复现，但**是先被浏览器
发现的**，因为只有真实的界面会重新预览、重新取令牌、再冻一次。

隔离
----
每次运行都用全新的临时数据目录启动独立 API，绝不触碰任何既有账本：
上一次运行冻结的计划如果留到下一次，重复运行的结论就不可信了。

用法
----
    python tools/check_ui_flow.py

前置：`apps/web/dist` 已构建（`npm run build`）。

退出码：0 = 全部断言通过；1 = 有断言失败；2 = 环境没起来。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "apps" / "web"
PYTHON = Path(sys.executable)

API_PORT = 8123
WEB_PORT = 4174


def _wait_http(url: str, *, timeout: float = 40.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=3) as resp:
                if resp.status < 500:
                    return True
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(0.4)
    return False


def main() -> int:
    if not (WEB / "dist" / "index.html").exists():
        print("缺少 apps/web/dist —— 先运行 npm run build")
        return 2

    browser_check = WEB / "tools" / "browser_check.mjs"
    if not browser_check.exists():
        print(f"缺少界面检查脚本：{browser_check}")
        return 2

    data_dir = Path(tempfile.mkdtemp(prefix="aquant-uiflow-"))
    api_env = dict(os.environ)
    api_env["PYTHONPATH"] = str(ROOT / "src")
    api_env["AQUANT_DATA_DIR"] = str(data_dir)
    api_env["AQUANT_RESET_DATA"] = "1"
    # 前端与 API 不同端口，是真正的跨源请求——必须显式放行这个来源。
    # 这同时也验证了"前后端分开部署"这条路径确实可用。
    api_env["AQUANT_CORS_ORIGINS"] = f"http://127.0.0.1:{WEB_PORT}"

    # 每次都用新目录，但仍然显式重置：万一以后改成固定目录，
    # 这一行保证语义不变——"这次运行从零开始"。
    api = subprocess.Popen(
        [str(PYTHON), "-m", "uvicorn", "main:app", "--app-dir", "apps/api",
         "--host", "127.0.0.1", "--port", str(API_PORT)],
        cwd=ROOT, env=api_env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    web = subprocess.Popen(
        ["npx", "--yes", "vite", "preview", "--port", str(WEB_PORT), "--strictPort"],
        cwd=WEB, shell=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )

    try:
        api_url = f"http://127.0.0.1:{API_PORT}/api/v1/health"
        web_url = f"http://127.0.0.1:{WEB_PORT}/"
        if not _wait_http(api_url):
            print(f"API 未就绪：{api_url}")
            return 2
        if not _wait_http(web_url):
            print(f"前端未就绪：{web_url}")
            return 2
        print(f"隔离后端 {api_url.replace('/api/v1/health', '')}")
        print(f"前端 {web_url}")

        proc = subprocess.run(
            ["node", str(browser_check),
             "--url", f"http://127.0.0.1:{WEB_PORT}",
             "--api", f"http://127.0.0.1:{API_PORT}"],
            cwd=WEB, text=True,
        )
        return proc.returncode
    finally:
        for p in (web, api):
            p.terminate()
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()
        shutil.rmtree(data_dir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
