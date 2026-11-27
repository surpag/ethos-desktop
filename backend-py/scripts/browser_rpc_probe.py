"""浏览器 RPC 探针（被 `desktop/scripts/spikes/rpc-smoke.cjs` 当子进程拉起）。

为什么要单独一个进程：方案阶段 3 的门禁是「真实 Electron + Python 双进程通信」。
在同一个进程里 import 客户端再调用，测不到 `EDITH_BROWSER_RPC_*` 注入、子进程环境、
以及「后端重启后能重新连上」这几件事。

输入：环境变量
  ETHOS_BROWSER_RPC_URL    形如 http://127.0.0.1:<port>
  ETHOS_BROWSER_RPC_TOKEN  每次启动随机生成
  BROWSER_PROBE_TARGET     导航目标，默认 https://example.org/

输出：最后一行 `PROBE_RESULT {json}`，退出码 0 表示全部通过。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

# 允许直接 `python scripts/browser_rpc_probe.py` 运行（不依赖外部 PYTHONPATH）。
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from edith_backend.internal.browser import BrowserRpcClient, BrowserRpcError  # noqa: E402


async def run() -> dict:
    base_url = os.environ.get("ETHOS_BROWSER_RPC_URL", "").strip()
    token = os.environ.get("ETHOS_BROWSER_RPC_TOKEN", "").strip()
    target = os.environ.get("BROWSER_PROBE_TARGET", "https://example.org/").strip()

    result: dict = {"ok": False, "stage": "init", "steps": []}
    if not base_url or not token:
        result["error"] = "缺少 ETHOS_BROWSER_RPC_URL / ETHOS_BROWSER_RPC_TOKEN"
        return result

    try:
        async with BrowserRpcClient(base_url, token) as client:
            result["stage"] = "health"
            health = await client.health()
            result["steps"].append({"step": "health", "result": health})

            result["stage"] = "active"
            active = await client.active_tab()
            result["steps"].append({"step": "active", "result": active})

            result["stage"] = "before"
            before = await client.current_url()
            result["steps"].append({"step": "before", "result": before})

            result["stage"] = "navigate"
            navigated = await client.navigate(target)
            result["steps"].append({"step": "navigate", "result": navigated})

            result["stage"] = "after"
            after = await client.current_url()
            result["steps"].append({"step": "after", "result": after})

            # 「导航真的发生了」以回读的 URL 为准，不看 navigate 的返回值——
            # 如果只信返回体，服务端把结果抄错一遍也能通过。
            result["ok"] = target.rstrip("/") in str(after.get("url", ""))
            result["stage"] = "done"
    except BrowserRpcError as error:
        result["error"] = {"code": error.code, "message": error.message, "retryable": error.retryable}
    except Exception as error:  # noqa: BLE001 - 探针要把任何意外都变成可读结果
        result["error"] = {"code": "unexpected", "message": repr(error)}
    return result


def main() -> int:
    outcome = asyncio.run(run())
    sys.stdout.write("PROBE_RESULT " + json.dumps(outcome, ensure_ascii=False) + "\n")
    sys.stdout.flush()
    return 0 if outcome.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
