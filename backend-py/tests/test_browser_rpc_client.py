"""浏览器 RPC 客户端的协议契约测试。

这里起的是**真实的本地 HTTP 服务**（`http.server` + 线程），不是把 httpx 打桩：
客户端最容易出错的地方恰恰是「怎么解读对端的响应」，mock 掉传输层就等于把要测的
东西测没了。

覆盖：正常路径、鉴权失败、未知方法、超时、返回非 JSON、连接不可达、服务未启用。
真实 Electron ↔ Python 双进程联调在 `desktop/scripts/spikes/rpc-smoke.cjs`。
"""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from edith_backend.internal.browser import BrowserRpcClient, BrowserRpcError, BrowserService

TOKEN = "test-token"


class _Handler(BaseHTTPRequestHandler):
    server_version = "TestBrowserRpc/1.0"

    def log_message(self, *_args: object) -> None:  # 静默：测试输出里不需要访问日志
        return

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 的约定命名
        config = self.server.rpc_config  # type: ignore[attr-defined]
        if self.path != "/rpc":
            self._json(404, {"ok": False, "error": {"code": "not_found", "message": "只支持 POST /rpc"}})
            return

        if self.headers.get("authorization") != f"Bearer {config['token']}":
            self._json(401, {"ok": False, "error": {"code": "unauthorized", "message": "令牌无效"}})
            return

        length = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw)
        except ValueError:
            self._json(400, {"ok": False, "error": {"code": "invalid_json", "message": "不是合法 JSON"}})
            return

        method = payload.get("method")
        params = payload.get("params") or {}

        if method == "health":
            self._json(200, {"ok": True, "result": {"backend": "test", "tabs": 1, "activeTabId": "tab_1"}})
        elif method == "tabs.list":
            self._json(200, {"ok": True, "result": {"tabs": [_tab()]}})
        elif method == "tabs.active":
            self._json(200, {"ok": True, "result": _tab()})
        elif method == "page.navigate":
            self._json(200, {"ok": True, "result": {"tabId": "tab_1", "url": params.get("url"), "title": "Example"}})
        elif method == "page.url":
            self._json(200, {"ok": True, "result": {"tabId": "tab_1", "url": "https://example.com/", "title": "Example"}})
        elif method == "slow":
            time.sleep(float(config.get("slow", 1.5)))
            self._json(200, {"ok": True, "result": {"slept": True}})
        elif method == "broken":
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.end_headers()
            self.wfile.write(b"not-json")
        else:
            self._json(404, {"ok": False, "error": {"code": "method_not_allowed", "message": f"不支持：{method}"}})

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _tab() -> dict:
    return {
        "tabId": "tab_1",
        "url": "https://example.com/",
        "title": "Example Domain",
        "loading": False,
        "active": True,
    }


@pytest.fixture()
def rpc_server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    httpd.rpc_config = {"token": TOKEN, "slow": 1.5}  # type: ignore[attr-defined]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_health_happy_path(rpc_server: str) -> None:
    async def body() -> None:
        client = BrowserRpcClient(rpc_server, TOKEN)
        try:
            result = await client.health()
            assert result["backend"] == "test"
            assert result["tabs"] == 1
        finally:
            await client.aclose()

    asyncio.run(body())


def test_tabs_and_active_tab(rpc_server: str) -> None:
    async def body() -> None:
        async with BrowserRpcClient(rpc_server, TOKEN) as client:
            tabs = await client.tabs()
            assert [tab["tabId"] for tab in tabs] == ["tab_1"]
            active = await client.active_tab()
            assert active["title"] == "Example Domain"

    asyncio.run(body())


def test_navigate_sends_url(rpc_server: str) -> None:
    async def body() -> None:
        async with BrowserRpcClient(rpc_server, TOKEN) as client:
            result = await client.navigate("https://example.org/")
            assert result["url"] == "https://example.org/"

    asyncio.run(body())


def test_wrong_token_is_fatal(rpc_server: str) -> None:
    async def body() -> None:
        async with BrowserRpcClient(rpc_server, "wrong-token") as client:
            with pytest.raises(BrowserRpcError) as caught:
                await client.health()
            error = caught.value
            assert error.code == "unauthorized"
            assert error.status == 401
            # 令牌错是调用方问题，重试只会得到同样的 401。
            assert error.retryable is False

    asyncio.run(body())


def test_unknown_method_is_fatal(rpc_server: str) -> None:
    async def body() -> None:
        async with BrowserRpcClient(rpc_server, TOKEN) as client:
            with pytest.raises(BrowserRpcError) as caught:
                await client.call("does.not.exist")
            assert caught.value.code == "method_not_allowed"
            assert caught.value.retryable is False

    asyncio.run(body())


def test_timeout_is_retryable(rpc_server: str) -> None:
    async def body() -> None:
        async with BrowserRpcClient(rpc_server, TOKEN) as client:
            with pytest.raises(BrowserRpcError) as caught:
                await client.call("slow", timeout=0.2)
            assert caught.value.code == "timeout"
            assert caught.value.retryable is True

    asyncio.run(body())


def test_non_json_response_is_retryable(rpc_server: str) -> None:
    async def body() -> None:
        async with BrowserRpcClient(rpc_server, TOKEN) as client:
            with pytest.raises(BrowserRpcError) as caught:
                await client.call("broken")
            assert caught.value.code == "invalid_response"
            assert caught.value.retryable is True

    asyncio.run(body())


def test_connection_refused_is_unavailable() -> None:
    # 先占一个端口再释放，保证这个端口上确实没有服务在听。
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()

    async def body() -> None:
        async with BrowserRpcClient(f"http://127.0.0.1:{port}", TOKEN) as client:
            with pytest.raises(BrowserRpcError) as caught:
                await client.health()
            assert caught.value.code == "unavailable"
            assert caught.value.retryable is True

    asyncio.run(body())


def test_service_disabled_without_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ETHOS_BROWSER_RPC_URL", raising=False)
    monkeypatch.delenv("ETHOS_BROWSER_RPC_TOKEN", raising=False)
    service = BrowserService()
    assert service.enabled is False

    async def body() -> None:
        with pytest.raises(BrowserRpcError) as caught:
            await service.health()
        assert caught.value.code == "browser_disabled"
        assert caught.value.retryable is False

    asyncio.run(body())


def test_service_reads_env(monkeypatch: pytest.MonkeyPatch, rpc_server: str) -> None:
    monkeypatch.setenv("ETHOS_BROWSER_RPC_URL", rpc_server)
    monkeypatch.setenv("ETHOS_BROWSER_RPC_TOKEN", TOKEN)
    service = BrowserService()
    assert service.enabled is True

    async def body() -> None:
        try:
            assert (await service.health())["backend"] == "test"
        finally:
            await service.aclose()

    asyncio.run(body())
