"""阶段 5 验收探针：快照预算 / 分页 / 元素级复核。

由 `desktop/scripts/spikes/snapshot-budget-smoke.cjs` 当子进程拉起，走的是**真实
工具链**：`BrowserToolSet → BrowserService → BrowserRpcClient → HTTP → Electron
BrowserManager → Chromium`。页面是 smoke 脚本自己起的本地固定测试页。

11 条用例按 C1..C11 编号，与 `docs/design/browser-architecture.md` 的阶段 5
实施记录一一对应。输出最后一行 `SNAPSHOT_PROBE {json}`。
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
    origin = os.environ.get("BROWSER_SNAPSHOT_ORIGIN", "").strip().rstrip("/")
    result: dict = {"ok": False, "cases": [], "steps": [], "error": ""}
    if not origin:
        result["error"] = "缺少 BROWSER_SNAPSHOT_ORIGIN"
        return result

    def case(cid: str, name: str, ok: bool, detail=None) -> None:
        item = {"id": cid, "name": name, "ok": bool(ok)}
        if detail is not None:
            item["detail"] = detail
        result["cases"].append(item)

    service = BrowserService()
    tools = BrowserToolSet(service)

    async def call(name: str, arguments: dict | None = None) -> dict:
        raw = await tools.execute(name, arguments or {}, "probe-user", "probe-session")
        data = json.loads(raw)
        result["steps"].append({"tool": name, "args": arguments, "result": data})
        return data

    try:
        # ── 大列表页：默认预算 / 分页 / 跨页 ref ─────────────────────────
        await call("browser_open_url", {"url": f"{origin}/big?n=300"})

        default_snap = await call("browser_get_page_snapshot")
        elements = default_snap.get("elements") or []
        case(
            "C1",
            "默认预算：单次不超过 120 个元素，但页面总数不丢",
            1 <= len(elements) <= 120 and default_snap.get("page", {}).get("total") == 300,
            {"returned": len(elements), "total": default_snap.get("page", {}).get("total")},
        )

        page1 = await call("browser_get_page_snapshot", {"max_elements": 5})
        refs1 = [item.get("ref") for item in page1.get("elements") or []]
        case(
            "C4",
            "分页：max_elements=5 时返回 5 条并给出 nextCursor",
            len(refs1) == 5 and bool(page1.get("nextCursor")),
            {"refs": refs1, "nextCursor": page1.get("nextCursor")},
        )

        page2 = await call("browser_get_page_snapshot", {"cursor": page1.get("nextCursor")})
        refs2 = [item.get("ref") for item in page2.get("elements") or []]
        expected = [f"el_{index:03d}" for index in range(6, 11)]
        case(
            "C5",
            "分页：第二页 ref 全局连续（el_006..el_010），不与第一页重叠",
            refs2 == expected and not (set(refs1) & set(refs2)),
            {"refs": refs2, "expected": expected},
        )
        case(
            "C6",
            "分页：两页共享同一个 observation_id",
            page2.get("observationId") == page1.get("observationId"),
            {"page1": page1.get("observationId"), "page2": page2.get("observationId")},
        )

        # 跨页 ref：用第一页的 ref 在翻到第二页之后点击，必须仍然有效。
        clicked = await call(
            "browser_click",
            {"observation_id": page1.get("observationId"), "ref": refs1[0]},
        )
        # 页面 onclick 改 title 是异步事件，等它落到 tab 状态上再回读。
        await asyncio.sleep(0.4)
        current = await call("browser_get_url")
        case(
            "C7",
            "分页：用第一页的 ref 在第二页取回后仍可点击",
            clicked.get("clicked") is True and "clicked:B001" in str(current.get("title") or ""),
            {"clicked": clicked.get("clicked"), "title": current.get("title")},
        )

        # ── 导航后旧 cursor 必须作废 ────────────────────────────────────
        stale_cursor = page2.get("nextCursor")
        await call("browser_open_url", {"url": f"{origin}/big?n=7"})
        stale = await call("browser_get_page_snapshot", {"cursor": stale_cursor})
        case(
            "C11",
            "分页：导航之后旧 cursor 返回 stale_observation",
            stale.get("code") == "stale_observation",
            stale.get("code"),
        )

        # ── 最后一页：游标归 null、truncated 为 false ───────────────────
        last1 = await call("browser_get_page_snapshot", {"max_elements": 5})
        last2 = await call(
            "browser_get_page_snapshot", {"cursor": last1.get("nextCursor")}
        )
        case(
            "C8",
            "分页：取到末页时 nextCursor=null 且 truncated=false",
            len(last2.get("elements") or []) == 2
            and last2.get("nextCursor") is None
            and last2.get("truncated") is False,
            {
                "returned": len(last2.get("elements") or []),
                "nextCursor": last2.get("nextCursor"),
                "truncated": last2.get("truncated"),
            },
        )

        # ── 字符预算：超长名称不能把整页快照撑爆 ────────────────────────
        await call("browser_open_url", {"url": f"{origin}/huge?n=200&len=200"})
        huge = await call("browser_get_page_snapshot")
        huge_elements = huge.get("elements") or []
        budget = huge.get("budget") or {}
        case(
            "C2",
            "预算：超长名称下返回条数与 usedChars 都不超过上限",
            len(huge_elements) < 120
            and int(budget.get("usedChars") or 0) <= int(budget.get("maxChars") or 0),
            {
                "returned": len(huge_elements),
                "usedChars": budget.get("usedChars"),
                "maxChars": budget.get("maxChars"),
            },
        )
        case(
            "C3",
            "预算：被预算截掉的元素计入 omitted 且 truncated=true",
            int(huge.get("omitted") or 0) > 0
            and huge.get("truncated") is True
            and bool(huge.get("nextCursor")),
            {"omitted": huge.get("omitted"), "truncated": huge.get("truncated")},
        )

        # ── 元素级复核：局部刷新后旧 ref 不能被当成原元素 ────────────────
        await call("browser_open_url", {"url": f"{origin}/mutate"})
        mutate = await call("browser_get_page_snapshot")
        by_name = {item.get("name"): item for item in mutate.get("elements") or []}
        starter = by_name.get("开始变化")
        changing = by_name.get("稍后变化")
        vanishing = by_name.get("稍后消失")
        if not (starter and changing and vanishing):
            result["error"] = (
                f"/mutate 快照缺少元素：starter={bool(starter)} "
                f"changing={bool(changing)} vanishing={bool(vanishing)}"
            )
            return result

        # 触发页面自己的定时器：300ms 改文本、600ms 删除另一个元素。
        activated = await call(
            "browser_click",
            {"observation_id": mutate.get("observationId"), "ref": starter.get("ref")},
        )
        if activated.get("clicked") is not True:
            result["error"] = f"触发局部刷新失败：{activated}"
            return result
        await asyncio.sleep(1.2)

        changed = await call(
            "browser_click",
            {"observation_id": mutate.get("observationId"), "ref": changing.get("ref")},
        )
        case(
            "C9",
            "复核：同一 ref 的文案变了必须返回 element_changed",
            changed.get("code") == "element_changed",
            changed.get("code") or changed,
        )

        vanished = await call(
            "browser_click",
            {"observation_id": mutate.get("observationId"), "ref": vanishing.get("ref")},
        )
        case(
            "C10",
            "复核：元素被移除必须返回 element_missing（不是内部错误）",
            vanished.get("code") == "element_missing",
            vanished.get("code") or vanished,
        )

        result["ok"] = all(item["ok"] for item in result["cases"])
        if not result["ok"]:
            result["error"] = "存在未通过的验收用例"
    except Exception as error:  # noqa: BLE001 - 探针要把意外变成可读结果
        result["error"] = repr(error)
    finally:
        await service.aclose()
    return result


def main() -> int:
    outcome = asyncio.run(run())
    sys.stdout.write("SNAPSHOT_PROBE " + json.dumps(outcome, ensure_ascii=False) + "\n")
    sys.stdout.flush()
    return 0 if outcome.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
