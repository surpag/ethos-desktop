"""浏览器操作在 trace_steps 里的完整形状（阶段 8）。

这里不经过 LLM，但走的是**真实的 trace 写入链路**：`Tracer.step(TOOL_CALL)` →
工具执行 → ContextVar 元数据 → `Store.insert_steps` → 查询回来。

真实 run 的验证需要模型（见阶段 8 报告：未运行 → 结论 PARTIAL）。本测试守住
「存储层能按顺序还原一次浏览器操作链」与「不落敏感内容」这两条底线。
"""

from __future__ import annotations

import asyncio
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import aiosqlite

from edith_backend.internal.browser import BrowserService, BrowserToolSet
from edith_backend.internal.tools import (
    record_tool_trace,
    redact_tool_input,
    redact_tool_output,
    tool_trace_scope,
)
from edith_backend.internal.trace import TOOL_CALL, new_tracer
from edith_backend.internal.trace.schema import create_schema
from edith_backend.internal.trace.store import Store

OBS = "obs_trace01"
SECRET = "hunter2-do-not-store"


class _Handler(BaseHTTPRequestHandler):
    calls: list[dict] = []

    def do_POST(self) -> None:  # noqa: N802 - HTTP 方法名
        length = int(self.headers.get("content-length") or 0)
        payload = json.loads(self.rfile.read(length) or b"{}")
        method = payload.get("method")
        params = payload.get("params") or {}
        self.calls.append({"method": method, "params": params})

        if method == "page.snapshot":
            self._json(
                200,
                {
                    "ok": True,
                    "result": {
                        "observationId": OBS,
                        "tabId": "tab_trace",
                        "pageVersion": 1,
                        "url": "https://example.com/app",
                        "title": "trace",
                        "elements": [
                            {"ref": "el_001", "role": "textbox", "name": "搜索", "tag": "input"}
                        ],
                        "elements_count": 1,
                        "truncated": False,
                    },
                },
            )
            return
        if method == "page.fill":
            self._json(200, {"ok": True, "result": {"filled": True, "ref": "el_001", "length": 11}})
            return
        if method == "page.click":
            if params.get("ref") == "el_999":
                self._json(
                    409,
                    {"ok": False, "error": {"code": "element_missing", "message": "找不到元素"}},
                )
                return
            self._json(200, {"ok": True, "result": {"clicked": True, "ref": "el_001"}})
            return
        self._json(404, {"ok": False, "error": {"code": "method_not_allowed", "message": method}})

    def log_message(self, *_args) -> None:
        return

    def _json(self, status: int, body: dict) -> None:
        raw = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def _start() -> tuple[ThreadingHTTPServer, str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def test_trace_steps_restore_browser_chain(tmp_path) -> None:
    server, url = _start()

    async def body() -> list[dict]:
        db = await aiosqlite.connect(tmp_path / "trace.db")
        # Store 的查询用 dict(row)：没设 row_factory 时 aiosqlite 返回元组，
        # dict(tuple) 会抛 TypeError（诊断脚本里踩到过）。
        db.row_factory = aiosqlite.Row
        await create_schema(db)
        store = Store(db)
        tracer = new_tracer(store, "req-trace-1", "u1", "s1")
        await tracer.begin()

        service = BrowserService(base_url=url, token="test-token")
        tools = BrowserToolSet(service)
        calls = [
            ("browser_get_page_snapshot", {}, "call-1"),
            (
                "browser_fill",
                {"observation_id": OBS, "ref": "el_001", "value": SECRET},
                "call-2",
            ),
            ("browser_click", {"observation_id": OBS, "ref": "el_999"}, "call-3"),
        ]
        for name, arguments, tool_call_id in calls:
            async with tracer.step(
                TOOL_CALL,
                name,
                input=redact_tool_input(name, json.dumps(arguments)),
                metadata={"source": "builtin"},
            ) as step:
                with tool_trace_scope() as meta:
                    raw = await tools.execute(
                        name,
                        arguments,
                        "u1",
                        "s1",
                        tool_call_id=tool_call_id,
                        request_id="req-trace-1",
                    )
                    record_tool_trace(note="e2e")  # 工具已写过，这里不覆盖
                step.output = redact_tool_output(name, raw)
                if meta:
                    meta.setdefault("run_id", "req-trace-1")
                    meta.setdefault("session_id", "s1")
                    meta.setdefault("tool_call_id", tool_call_id)
                    step.metadata.update(meta)

        try:
            await tracer.flush()
            return await store.get_steps("req-trace-1")
        finally:
            # 异常路径也必须关连接：aiosqlite 的后台线程会让 pytest 挂住。
            await service.aclose()
            await db.close()

    try:
        steps = asyncio.run(body())
    finally:
        server.shutdown()

    assert [step["name"] for step in steps] == [
        "browser_get_page_snapshot",
        "browser_fill",
        "browser_click",
    ]
    assert [step["seq"] for step in steps] == sorted(step["seq"] for step in steps)

    first, second, third = (json.loads(step["metadata"]) for step in steps)
    assert first["action"] == "snapshot"
    assert first["run_id"] == "req-trace-1"
    assert first["session_id"] == "s1"
    assert first["tool_call_id"] == "call-1"
    assert first["tab_id"] == "tab_trace"
    assert first["observation_id"] == OBS
    assert first["status"] == "ok"
    assert first["elements"] == 1
    assert first["rpc_ms"] >= 0
    assert first["tool_ms"] >= 0

    assert second["action"] == "fill"
    assert second["tool_call_id"] == "call-2"

    assert third["action"] == "click"
    assert third["status"] == "error"
    assert third["error_code"] == "element_missing"
    assert third["tool_call_id"] == "call-3"

    # 敏感内容不能出现在任何一步的任何列里（metadata / digest / 错误信息）。
    serialized = json.dumps(steps, ensure_ascii=False)
    assert SECRET not in serialized
