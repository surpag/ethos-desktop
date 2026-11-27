"""阶段 8 E2E 探针：真实 Python 工具链下的浏览器操作验收。

由 `desktop/scripts/spikes/e2e-smoke.cjs` 拉起。按 `E2E_PHASE` 分阶段执行，
因为「崩溃」「RPC 断开」「多标签切换」需要 Electron 主进程先改环境：

- `main`：打开 → 快照 → 点击 → 填写 → 回车提交 → 滚动 → 旧快照失效
- `multitab`：确认操作的确实是当前激活的标签页
- `crash`：页面崩溃 → 明确报错 → 重新打开恢复
- `offline`：RPC 断开 → tool_failure（不可重试）

输出最后一行 `E2E_PROBE {json}`。
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
    origin = os.environ.get("BROWSER_E2E_ORIGIN", "").strip().rstrip("/")
    phase = os.environ.get("E2E_PHASE", "main").strip() or "main"
    run_id = os.environ.get("E2E_RUN_ID", "e2e-run").strip() or "e2e-run"
    result: dict = {"ok": False, "phase": phase, "cases": [], "steps": [], "error": ""}
    if not origin:
        result["error"] = "缺少 BROWSER_E2E_ORIGIN"
        return result

    def case(cid: str, name: str, ok: bool, detail=None) -> None:
        item = {"id": cid, "name": name, "ok": bool(ok)}
        if detail is not None:
            item["detail"] = detail
        result["cases"].append(item)

    service = BrowserService()
    tools = BrowserToolSet(service)

    async def call(name: str, arguments: dict | None = None) -> dict:
        raw = await tools.execute(
            name, arguments or {}, "e2e-user", "e2e-session", request_id=run_id
        )
        data = json.loads(raw)
        result["steps"].append({"tool": name, "args": arguments, "result": data})
        return data

    def find(snapshot: dict, name_part: str) -> dict | None:
        for item in snapshot.get("elements") or []:
            if name_part in str(item.get("name") or ""):
                return item
        return None

    try:
        if phase == "main":
            opened = await call("browser_open_url", {"url": f"{origin}/app"})
            case(
                "E1",
                "打开网页：返回 tabId 且 URL 落在测试页",
                bool(opened.get("tabId")) and "/app" in str(opened.get("url") or ""),
                opened,
            )

            snapshot = await call("browser_get_page_snapshot")
            search = find(snapshot, "搜索关键词")
            submit = find(snapshot, "提交")
            case(
                "E2",
                "获取快照：拿到 observation 与搜索框/提交按钮",
                bool(snapshot.get("observationId"))
                and search is not None
                and submit is not None,
                {"observationId": snapshot.get("observationId"), "elements": len(snapshot.get("elements") or [])},
            )

            switch = find(snapshot, "切换文字")
            clicked = await call(
                "browser_click",
                {"observation_id": snapshot.get("observationId"), "ref": switch.get("ref") if switch else ""},
            )
            await asyncio.sleep(0.3)
            title = str((await call("browser_get_url")).get("title") or "")
            case(
                "E3",
                "点击元素：页面状态随之变化（标题变为已切换）",
                clicked.get("clicked") is True and "已切换" in title,
                {"clicked": clicked.get("clicked"), "title": title},
            )

            snapshot2 = await call("browser_get_page_snapshot")
            search2 = find(snapshot2, "搜索关键词")
            filled = await call(
                "browser_fill",
                {
                    "observation_id": snapshot2.get("observationId"),
                    "ref": search2.get("ref") if search2 else "",
                    "value": "hello world",
                },
            )
            case(
                "E4",
                "填写表单：输入框写入 11 个字符",
                filled.get("filled") is True and int(filled.get("length") or 0) == 11,
                filled,
            )

            pressed = await call(
                "browser_press",
                {
                    "observation_id": snapshot2.get("observationId"),
                    "ref": search2.get("ref") if search2 else "",
                    "key": "Enter",
                },
            )
            submitted_url = ""
            for _ in range(50):
                submitted_url = str((await call("browser_get_url")).get("url") or "")
                if "/results" in submitted_url:
                    break
                await asyncio.sleep(0.1)
            case(
                "E5",
                "按键提交：回车后导航到结果页且带上查询参数",
                pressed.get("pressed") is True
                and "/results" in submitted_url
                and "q=hello" in submitted_url,
                {"pressed": pressed.get("pressed"), "url": submitted_url},
            )

            scrolled = await call("browser_scroll", {"direction": "down", "amount": 800})
            case(
                "E6",
                "页面滚动：滚动位置发生位移",
                scrolled.get("direction") == "down"
                and float(scrolled.get("after") or 0) > float(scrolled.get("before") or 0),
                {"before": scrolled.get("before"), "after": scrolled.get("after")},
            )

            # 旧快照失效：先拿到结果页的 observation，再导航，然后用旧 ref 点击。
            results_snapshot = await call("browser_get_page_snapshot")
            next_link = find(results_snapshot, "去第二页")
            await call("browser_open_url", {"url": f"{origin}/second"})
            await asyncio.sleep(0.2)
            stale = await call(
                "browser_click",
                {
                    "observation_id": results_snapshot.get("observationId"),
                    "ref": next_link.get("ref") if next_link else "el_001",
                },
            )
            case(
                "E8",
                "导航后旧快照失效：旧 observation + ref 返回 stale_observation",
                stale.get("code") == "stale_observation",
                stale.get("code") or stale,
            )

        elif phase == "multitab":
            current = await call("browser_get_url")
            snapshot = await call("browser_get_page_snapshot")
            second_btn = find(snapshot, "第二页按钮")
            clicked = await call(
                "browser_click",
                {
                    "observation_id": snapshot.get("observationId"),
                    "ref": second_btn.get("ref") if second_btn else "",
                },
            )
            await asyncio.sleep(0.3)
            title = str((await call("browser_get_url")).get("title") or "")
            case(
                "E7",
                "多标签切换：工具操作的是当前激活标签页（第二页）",
                "/second" in str(current.get("url") or "")
                and second_btn is not None
                and clicked.get("clicked") is True
                and "clicked-second" in title,
                {
                    "url": current.get("url"),
                    "clicked": clicked.get("clicked"),
                    "title": title,
                },
            )

        elif phase == "crash":
            crashed = await call("browser_get_page_snapshot")
            case(
                "E12a",
                "页面崩溃：快照返回 page_crashed（可自救的 command_error）",
                crashed.get("code") == "page_crashed"
                and crashed.get("kind") == "command_error"
                and crashed.get("retryable") is True,
                crashed.get("code") or crashed,
            )
            reopened = await call("browser_open_url", {"url": f"{origin}/app"})
            recovered = await call("browser_get_page_snapshot")
            case(
                "E12b",
                "页面崩溃后：重新打开即可恢复，快照正常",
                bool(reopened.get("tabId")) and bool(recovered.get("observationId")),
                {"url": reopened.get("url"), "observationId": recovered.get("observationId")},
            )

        elif phase == "offline":
            failed = await call("browser_get_page_snapshot")
            case(
                "E11",
                "RPC 断开：返回 tool_failure 且不可重试",
                failed.get("kind") == "tool_failure" and failed.get("retryable") is False,
                failed.get("code") or failed,
            )
        else:
            result["error"] = f"未知阶段：{phase}"
            return result

        result["ok"] = all(item["ok"] for item in result["cases"])
        if not result["ok"]:
            result["error"] = "存在未通过的 E2E 用例"
    except Exception as error:  # noqa: BLE001 - 探针要把意外变成可读结果
        result["error"] = repr(error)
    finally:
        await service.aclose()
    return result


def main() -> int:
    outcome = asyncio.run(run())
    sys.stdout.write("E2E_PROBE " + json.dumps(outcome, ensure_ascii=False) + "\n")
    sys.stdout.flush()
    return 0 if outcome.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
