"""阶段 6 验收探针：request_id 透传 + 两个 run 的控制权串行。

由 `desktop/scripts/spikes/control-smoke.cjs` 拉起，走真实工具链
（`BrowserToolSet → BrowserService → BrowserRpcClient → HTTP → Electron`）。

这里只覆盖「Python 侧到底把 request_id 送出去了，以及 Electron 是否按 run 串行」。
「用户夺回 + 抑制窗口 + 租约过期」由 spike 主进程直接驱动，因为那部分只有
Electron 进程能精确控制时间。

输出最后一行 `CONTROL_PROBE {json}`。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from edith_backend.internal.browser import BrowserService, BrowserToolSet  # noqa: E402

RUN_A = "req-probe-a"
RUN_B = "req-probe-b"


async def run() -> dict:
    origin = os.environ.get("BROWSER_CONTROL_ORIGIN", "").strip().rstrip("/")
    result: dict = {"ok": False, "cases": [], "steps": [], "error": ""}
    if not origin:
        result["error"] = "缺少 BROWSER_CONTROL_ORIGIN"
        return result

    def case(cid: str, name: str, ok: bool, detail=None) -> None:
        item = {"id": cid, "name": name, "ok": bool(ok)}
        if detail is not None:
            item["detail"] = detail
        result["cases"].append(item)

    service = BrowserService()
    tools = BrowserToolSet(service)

    async def call(name: str, arguments: dict | None, request_id: str) -> dict:
        raw = await tools.execute(
            name, arguments or {}, "probe-user", "probe-session", request_id=request_id
        )
        data = json.loads(raw)
        result["steps"].append(
            {"tool": name, "request_id": request_id, "args": arguments, "result": data}
        )
        return data

    try:
        # A 通过导航接管（page.navigate 是写操作）。
        opened = await call("browser_open_url", {"url": f"{origin}/control"}, RUN_A)
        case(
            "K1",
            "run A 的写操作成功并取得控制权",
            opened.get("tabId") is not None,
            opened.get("code") or opened.get("url"),
        )

        snap_a = await call("browser_get_page_snapshot", {}, RUN_A)
        case(
            "K2",
            "run A 取得快照",
            bool(snap_a.get("observationId")),
            snap_a.get("code"),
        )

        # B 在 A 的租约内不能操作页面：必须 busy，而不是点错元素。
        snap_b = await call("browser_get_page_snapshot", {}, RUN_B)
        case(
            "K3",
            "run B 在 A 持有控制权时被拒（browser_busy）",
            snap_b.get("code") == "browser_busy" and snap_b.get("kind") == "tool_failure",
            snap_b.get("code") or snap_b,
        )

        # 同一 run 重入不受影响。
        snap_a2 = await call("browser_get_page_snapshot", {}, RUN_A)
        case(
            "K4",
            "run A 重入仍然成功（不会把自己锁死）",
            bool(snap_a2.get("observationId")),
            snap_a2.get("code"),
        )

        # 只读的 page.url 不受控制权限制：任何 run 都可以看当前地址。
        url_b = await call("browser_get_url", {}, RUN_B)
        case(
            "K5",
            "run B 的只读 page.url 不被控制权拦截",
            bool(url_b.get("url")),
            url_b.get("code") or url_b.get("url"),
        )

        result["ok"] = all(item["ok"] for item in result["cases"])
        if not result["ok"]:
            result["error"] = "存在未通过的控制权用例"
    except Exception as error:  # noqa: BLE001 - 探针要把意外变成可读结果
        result["error"] = repr(error)
    finally:
        await service.aclose()
    return result


def main() -> int:
    outcome = asyncio.run(run())
    sys.stdout.write("CONTROL_PROBE " + json.dumps(outcome, ensure_ascii=False) + "\n")
    sys.stdout.flush()
    return 0 if outcome.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
