"""浏览器工具探针：由 `desktop/scripts/spikes/tools-smoke.cjs` 当子进程拉起。

走的是**真实工具链**：`BrowserToolSet → BrowserService → BrowserRpcClient → HTTP →
Electron BrowserManager → Chromium`。页面是 smoke 脚本起的本地固定测试页，
不依赖任何外部站点。

验证顺序：打开页面 → 取快照 → 填写 → 点击提交 → 回读 URL（必须带上填的值）
→ 用**旧快照**再点一次（必须被判定为 stale_observation）。

输出最后一行 `TOOLS_PROBE {json}`。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from edith_backend.internal.browser import BrowserService, BrowserToolSet  # noqa: E402


async def run() -> dict:
    target = os.environ.get("BROWSER_TOOLS_TARGET", "").strip()
    result: dict = {"ok": False, "steps": [], "error": ""}
    if not target:
        result["error"] = "缺少 BROWSER_TOOLS_TARGET"
        return result

    service = BrowserService()
    tools = BrowserToolSet(service)

    async def call(name: str, arguments: dict | None = None) -> dict:
        raw = await tools.execute(name, arguments or {}, "probe-user", "probe-session")
        data = json.loads(raw)
        result["steps"].append({"tool": name, "args": arguments, "result": data})
        return data

    try:
        await call("browser_open_url", {"url": target})

        snapshot = await call("browser_get_page_snapshot")
        observation_id = snapshot.get("observationId", "")
        elements = snapshot.get("elements") or []
        textbox = next((item for item in elements if item.get("role") == "textbox"), None)
        button = next((item for item in elements if item.get("tag") == "button"), None)
        if not observation_id or not textbox or not button:
            result["error"] = f"快照缺少必要元素：obs={bool(observation_id)} textbox={bool(textbox)} button={bool(button)}"
            return result

        await call(
            "browser_fill",
            {"observation_id": observation_id, "ref": textbox["ref"], "value": "hello"},
        )
        await call("browser_click", {"observation_id": observation_id, "ref": button["ref"]})

        # 点击触发的是表单 GET 导航，等 URL 真的变过去（最多 8 秒）。
        final_url = ""
        for _ in range(40):
            current = await call("browser_get_url")
            final_url = str(current.get("url") or "")
            if "q=hello" in final_url:
                break
            await asyncio.sleep(0.2)

        # 旧快照必须失效：这条是「模型点错东西」防线里最关键的一条。
        stale = await call(
            "browser_click", {"observation_id": observation_id, "ref": button["ref"]}
        )

        result["final_url"] = final_url
        result["stale_code"] = stale.get("code")
        result["ok"] = "q=hello" in final_url and stale.get("code") == "stale_observation"
        if not result["ok"]:
            result["error"] = "导航结果或旧快照失效判定不符合预期"
    except Exception as error:  # noqa: BLE001 - 探针要把意外变成可读结果
        result["error"] = repr(error)
    finally:
        await service.aclose()
    return result


def main() -> int:
    outcome = asyncio.run(run())
    sys.stdout.write("TOOLS_PROBE " + json.dumps(outcome, ensure_ascii=False) + "\n")
    sys.stdout.flush()
    return 0 if outcome.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
