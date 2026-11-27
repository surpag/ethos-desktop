"""浏览器工具集的契约测试。

起的是**真实本地 HTTP 服务**（模拟 Electron RPC 的协议），工具集会经过
`BrowserService → BrowserRpcClient → HTTP` 全链路，只有端点是我们自己写的桩。
这样既能验「参数怎么出去」，也能验「错误怎么回来、怎么分类」。

Electron 侧的真实页面操作（快照/点击/填写是否真的作用于 Chromium）在
`desktop/scripts/spikes/tools-smoke.cjs`，这里不重复。
"""

from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from edith_backend.internal.browser.service import BrowserService
from edith_backend.internal.browser.tools import BrowserToolSet
from edith_backend.internal.permissions import catalog
from edith_backend.internal.permissions.types import Capability

TOKEN = "tools-token"
OBS = "obs_abcdef01"


class _Handler(BaseHTTPRequestHandler):
    server_version = "TestBrowserTools/1.0"

    def log_message(self, *_args: object) -> None:
        return

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 约定
        state = self.server.state  # type: ignore[attr-defined]
        if self.headers.get("authorization") != f"Bearer {TOKEN}":
            self._json(401, {"ok": False, "error": {"code": "unauthorized", "message": "令牌无效"}})
            return
        length = int(self.headers.get("content-length") or 0)
        payload = json.loads(self.rfile.read(length) or b"{}")
        method = payload.get("method")
        params = payload.get("params") or {}
        state["calls"].append(
            {"method": method, "params": params, "request_id": payload.get("request_id")}
        )

        if method == "page.url":
            self._json(200, {"ok": True, "result": {"tabId": "tab_1", "url": state["url"], "title": state["title"]}})
        elif method == "page.navigate":
            state["url"] = str(params.get("url"))
            self._json(200, {"ok": True, "result": {"tabId": "tab_1", "url": state["url"], "title": "Test Page"}})
        elif method == "page.snapshot":
            self._json(
                200,
                {
                    "ok": True,
                    "result": {
                        "observationId": OBS,
                        "tabId": "tab_1",
                        "pageVersion": 1,
                        "url": state["url"],
                        "title": state["title"],
                        "elements": [
                            {"ref": "el_001", "role": "link", "name": "更多信息", "tag": "a", "disabled": False},
                            {"ref": "el_002", "role": "textbox", "name": "搜索", "tag": "input", "disabled": False},
                        ],
                        "truncated": False,
                        "omitted": 0,
                    },
                },
            )
        elif method == "page.click":
            if params.get("observation_id") == "obs_busy":
                self._json(409, {"ok": False, "error": {"code": "browser_busy", "message": "另一个任务正在操作"}})
            elif params.get("observation_id") == "obs_user":
                self._json(409, {"ok": False, "error": {"code": "user_took_control", "message": "用户刚接管"}})
            elif params.get("observation_id") != OBS:
                self._json(409, {"ok": False, "error": {"code": "stale_observation", "message": "快照已过期"}})
            elif params.get("ref") == "el_003":
                self._json(409, {"ok": False, "error": {"code": "element_changed", "message": "元素内容已变化"}})
            elif params.get("ref") != "el_001":
                self._json(409, {"ok": False, "error": {"code": "element_missing", "message": "找不到元素"}})
            else:
                self._json(200, {"ok": True, "result": {"clicked": True, "ref": "el_001", "tag": "a", "name": "更多信息"}})
        elif method == "page.fill":
            if params.get("observation_id") != OBS:
                self._json(409, {"ok": False, "error": {"code": "stale_observation", "message": "快照已过期"}})
            else:
                value = str(params.get("value") or "")
                self._json(200, {"ok": True, "result": {"filled": True, "ref": params.get("ref"), "length": len(value)}})
        elif method == "page.press":
            self._json(200, {"ok": True, "result": {"pressed": True, "key": params.get("key"), "submitted": False}})
        elif method == "page.scroll":
            self._json(
                200,
                {"ok": True, "result": {"direction": params.get("direction"), "before": 0, "after": 600, "height": 4000}},
            )
        else:
            self._json(404, {"ok": False, "error": {"code": "method_not_allowed", "message": str(method)}})

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture()
def rpc(monkeypatch: pytest.MonkeyPatch):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    httpd.state = {  # type: ignore[attr-defined]
        "url": "https://example.com/",
        "title": "Example Domain",
        "calls": [],
    }
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{httpd.server_address[1]}"
    monkeypatch.setenv("ETHOS_BROWSER_RPC_URL", base_url)
    monkeypatch.setenv("ETHOS_BROWSER_RPC_TOKEN", TOKEN)
    try:
        yield httpd.state
    finally:
        httpd.shutdown()
        httpd.server_close()


def _call(
    toolset: BrowserToolSet,
    name: str,
    arguments: dict | None = None,
    *,
    request_id: str = "",
) -> dict:
    """同步跑一次工具调用（项目里的异步测试统一用 asyncio.run 包装）。"""

    async def body() -> dict:
        raw = await toolset.execute(
            name, arguments or {}, "u1", "s1", request_id=request_id
        )
        return json.loads(raw)

    return asyncio.run(body())


def _toolset() -> BrowserToolSet:
    return BrowserToolSet(BrowserService())


def test_definitions_cover_seven_tools() -> None:
    names = {item["function"]["name"] for item in _toolset().tool_definitions()}
    assert names == {
        "browser_open_url",
        "browser_get_page_snapshot",
        "browser_click",
        "browser_fill",
        "browser_press",
        "browser_scroll",
        "browser_get_url",
    }


def test_required_arguments_declared() -> None:
    definitions = {item["function"]["name"]: item for item in _toolset().tool_definitions()}
    assert definitions["browser_open_url"]["function"]["parameters"]["required"] == ["url"]
    assert definitions["browser_click"]["function"]["parameters"]["required"] == [
        "observation_id",
        "ref",
    ]
    assert definitions["browser_fill"]["function"]["parameters"]["required"] == [
        "observation_id",
        "ref",
        "value",
    ]
    assert definitions["browser_get_url"]["function"]["parameters"]["required"] == []


def test_get_url(rpc: dict) -> None:
    result = _call(_toolset(), "browser_get_url")
    assert result["url"] == "https://example.com/"
    assert rpc["calls"][-1]["method"] == "page.url"


def test_open_url_passes_url(rpc: dict) -> None:
    result = _call(_toolset(), "browser_open_url", {"url": "https://example.org"})
    assert result["url"] == "https://example.org"
    assert rpc["calls"][-1]["params"]["url"] == "https://example.org"


def test_open_url_missing_argument_is_self_healable() -> None:
    result = _call(_toolset(), "browser_open_url", {})
    assert result["kind"] == "command_error"
    assert result["retryable"] is True
    assert result["code"] == "invalid_arguments"
    assert "url" in result["suggestion"] or "参数" in result["suggestion"]


def test_snapshot_returns_observation_and_refs(rpc: dict) -> None:
    result = _call(_toolset(), "browser_get_page_snapshot")
    assert result["observationId"] == OBS
    assert [item["ref"] for item in result["elements"]] == ["el_001", "el_002"]


def test_snapshot_passes_cursor_and_page_size(rpc: dict) -> None:
    """分页参数必须原样到达 Electron：cursor 是 opaque 字符串，工具层不解析、不改写。"""
    _call(
        _toolset(),
        "browser_get_page_snapshot",
        {"max_elements": 5, "cursor": "obs_abcdef01:120"},
    )
    params = rpc["calls"][-1]["params"]
    assert params == {"max_elements": 5, "cursor": "obs_abcdef01:120"}


def test_snapshot_ignores_blank_cursor(rpc: dict) -> None:
    _call(_toolset(), "browser_get_page_snapshot", {"cursor": "   "})
    assert "cursor" not in rpc["calls"][-1]["params"]


def test_snapshot_definition_documents_pagination() -> None:
    definitions = {item["function"]["name"]: item for item in _toolset().tool_definitions()}
    properties = definitions["browser_get_page_snapshot"]["function"]["parameters"]["properties"]
    assert "cursor" in properties
    assert "nextCursor" in definitions["browser_get_page_snapshot"]["function"]["description"]


def test_click_ok(rpc: dict) -> None:
    result = _call(_toolset(), "browser_click", {"observation_id": OBS, "ref": "el_001"})
    assert result["clicked"] is True


def test_click_with_stale_observation_is_retryable(rpc: dict) -> None:
    result = _call(_toolset(), "browser_click", {"observation_id": "obs_dead", "ref": "el_001"})
    assert result["kind"] == "command_error"
    assert result["retryable"] is True
    assert result["code"] == "stale_observation"
    # 建议里必须明确指向「重新取快照」，否则模型会尝试换 ref 硬试。
    assert "snapshot" in result["suggestion"]


def test_click_with_unknown_ref_is_retryable(rpc: dict) -> None:
    result = _call(_toolset(), "browser_click", {"observation_id": OBS, "ref": "el_999"})
    assert result["retryable"] is True
    assert result["code"] == "element_missing"


def test_click_on_changed_element_is_retryable(rpc: dict) -> None:
    """元素级复核失败（同 ref、内容已变）也必须让模型重新取快照，而不是当成服务故障。"""
    result = _call(_toolset(), "browser_click", {"observation_id": OBS, "ref": "el_003"})
    assert result["kind"] == "command_error"
    assert result["retryable"] is True
    assert result["code"] == "element_changed"
    assert "snapshot" in result["suggestion"]


def test_fill_passes_value(rpc: dict) -> None:
    result = _call(
        _toolset(),
        "browser_fill",
        {"observation_id": OBS, "ref": "el_002", "value": "你好 hello"},
    )
    assert result["filled"] is True
    assert result["length"] == 8
    assert rpc["calls"][-1]["params"]["value"] == "你好 hello"


def test_press_passes_key(rpc: dict) -> None:
    result = _call(
        _toolset(), "browser_press", {"observation_id": OBS, "ref": "el_002", "key": "Enter"}
    )
    assert result["key"] == "Enter"
    assert rpc["calls"][-1]["params"]["key"] == "Enter"


def test_scroll_defaults_to_down(rpc: dict) -> None:
    result = _call(_toolset(), "browser_scroll", {})
    # 参数缺省时由 Electron 侧兜底成 down；这里断言请求确实发出去了。
    assert rpc["calls"][-1]["method"] == "page.scroll"
    assert result["after"] == 600


def test_click_carries_request_id(rpc: dict) -> None:
    """控制权绑定靠 request_id：它必须出现在 RPC 请求体顶层，而不是 params 里。"""
    _call(
        _toolset(),
        "browser_click",
        {"observation_id": OBS, "ref": "el_001"},
        request_id="req-42",
    )
    last = rpc["calls"][-1]
    assert last["request_id"] == "req-42"
    assert "request_id" not in last["params"]


def test_legacy_call_without_request_id_stays_anonymous(rpc: dict) -> None:
    _call(_toolset(), "browser_get_url")
    assert rpc["calls"][-1]["request_id"] is None


def test_browser_busy_is_tool_failure(rpc: dict) -> None:
    """另一个 run 持有控制权时，模型无法自救，必须停下而不是重试。"""
    result = _call(
        _toolset(),
        "browser_click",
        {"observation_id": "obs_busy", "ref": "el_001"},
        request_id="req-b",
    )
    assert result["kind"] == "tool_failure"
    assert result["retryable"] is False
    assert result["code"] == "browser_busy"


def test_user_took_control_is_retryable(rpc: dict) -> None:
    result = _call(
        _toolset(),
        "browser_click",
        {"observation_id": "obs_user", "ref": "el_001"},
        request_id="req-c",
    )
    assert result["kind"] == "command_error"
    assert result["retryable"] is True
    assert result["code"] == "user_took_control"
    assert "snapshot" in result["suggestion"]


def test_disabled_returns_tool_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ETHOS_BROWSER_RPC_URL", raising=False)
    monkeypatch.delenv("ETHOS_BROWSER_RPC_TOKEN", raising=False)
    result = _call(_toolset(), "browser_get_url")
    assert result["kind"] == "tool_failure"
    assert result["retryable"] is False
    assert result["code"] == "browser_disabled"


def test_unknown_tool_name_is_tool_failure() -> None:
    result = _call(_toolset(), "browser_launch_rocket")
    assert result["kind"] == "tool_failure"


# ── 能力声明：这两条锁住的是「不要为了省事把浏览器读工具标成纯 READ」──


def test_snapshot_and_url_are_not_pure_readonly() -> None:
    """只读工具会进 run 级结果缓存，快照被缓存 = 下一次点击用过期 ref。

    这条断言是那个决策的护栏：一旦有人把 `browser_get_page_snapshot` 简化成
    `{READ}`，它会在测试里立刻红，而不是在真实使用中表现为「第二次点击总失败」。
    """
    for name in ("browser_get_page_snapshot", "browser_get_url", "browser_scroll"):
        assert catalog.is_readonly(name) is False, f"{name} 不能是纯只读（会被缓存）"
        assert Capability.METADATA in catalog.TOOL_CAPABILITIES[name]


def test_browser_tool_records_trace_metadata(rpc: dict) -> None:
    """工具自报字段必须真正进入 trace 收集器（而不是只写在日志里）。"""
    from edith_backend.internal.tools import tool_trace_scope

    toolset = _toolset()

    async def body() -> dict:
        with tool_trace_scope() as meta:
            await toolset.execute(
                "browser_get_page_snapshot", {}, "u1", "s1", request_id="req-1"
            )
        return dict(meta)

    meta = asyncio.run(body())
    assert meta["action"] == "snapshot"
    assert meta["status"] == "ok"
    assert meta["tab_id"] == "tab_1"
    assert meta["observation_id"] == OBS
    assert meta["elements"] == 2
    assert meta["rpc_ms"] >= 0
    assert meta["tool_ms"] >= 0


def test_browser_tool_records_error_code(rpc: dict) -> None:
    from edith_backend.internal.tools import tool_trace_scope

    toolset = _toolset()

    async def body() -> dict:
        with tool_trace_scope() as meta:
            await toolset.execute(
                "browser_click",
                {"observation_id": OBS, "ref": "el_999"},
                "u1",
                "s1",
            )
        return dict(meta)

    meta = asyncio.run(body())
    assert meta["action"] == "click"
    assert meta["status"] == "error"
    assert meta["error_code"] == "element_missing"


def test_mutating_tools_declare_external() -> None:
    """导航/点击/填写/按键会改变外部站点状态：审批档必须拦得住。"""
    for name in ("browser_open_url", "browser_click", "browser_fill", "browser_press"):
        assert Capability.EXTERNAL in catalog.TOOL_CAPABILITIES[name], name
