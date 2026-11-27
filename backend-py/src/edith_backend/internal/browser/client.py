"""调用 Electron 浏览器 RPC 的 HTTP 客户端。

只做一件事：把「本地 HTTP 调用」归一化成 `BrowserRpcError`，让上层不用关心
httpx 的异常层级、也不用关心对端返回的是 4xx 还是 5xx。

**刻意不做的两件事**：
1. 不重试。重试策略应该由调用方（工具层）决定——导航重试会重复提交表单，
   而读状态重试无害，两者不能用同一个开关。
2. 不走 `netguard`。那套是给「用户提供的 URL」用的出站校验；这里是去本机回环，
   与外部网络策略无关，硬套会把浏览器 RPC 判成「访问了内网地址」。
"""

from __future__ import annotations

import httpx

from .types import BrowserRpcError

DEFAULT_TIMEOUT = 10.0
#: 导航要等页面 load 完成，慢站点上 10 秒不够。与 Electron 侧 15 秒处理超时对齐。
NAVIGATE_TIMEOUT = 25.0


class BrowserRpcClient:
    def __init__(self, base_url: str, token: str, *, timeout: float = DEFAULT_TIMEOUT) -> None:
        if not base_url:
            raise ValueError("缺少浏览器 RPC 地址")
        if not token:
            raise ValueError("缺少浏览器 RPC 令牌")
        self._timeout = timeout
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=httpx.Timeout(timeout, connect=3.0),
            headers={
                "authorization": f"Bearer {token}",
                "content-type": "application/json",
            },
        )

    async def call(
        self,
        method: str,
        params: dict | None = None,
        *,
        timeout: float | None = None,
        request_id: str = "",
    ) -> dict:
        """调用一个 RPC 方法，返回 `result` 字段。

        失败一律抛 `BrowserRpcError`：
        - 连不上 / 超时 → `unavailable` / `timeout`（可重试）
        - 对端返回 `{ok:false}` → 用对端的 code（它比 HTTP 状态码更精确）
        - 返回体不是 JSON → `invalid_response`（可重试：可能是对端正在重启）

        `request_id` 放在请求体顶层而不是 `params`：它是**请求级身份**（哪个 run
        在操作浏览器），不是某个方法的业务参数。放在 params 里会让每个方法都得
        记得转发一遍，漏一个就退化成「匿名调用」。空值表示探针/旧客户端，不参与
        控制权判定。
        """
        body: dict = {"method": method, "params": params or {}}
        run = (request_id or "").strip()
        if run:
            body["request_id"] = run
        try:
            response = await self._client.post(
                "/rpc",
                json=body,
                timeout=timeout if timeout is not None else self._timeout,
            )
        except httpx.TimeoutException as exc:
            raise BrowserRpcError("timeout", f"浏览器 RPC 超时（{method}）：{exc}") from exc
        except httpx.HTTPError as exc:
            # 连接被拒、连接被重置、DNS 之类：浏览器服务没起来或已退出。
            raise BrowserRpcError("unavailable", f"浏览器 RPC 不可用（{method}）：{exc}") from exc

        try:
            payload = response.json()
        except ValueError as exc:
            raise BrowserRpcError(
                "invalid_response",
                f"浏览器 RPC 返回了非 JSON（HTTP {response.status_code}）",
                status=response.status_code,
            ) from exc

        if not response.is_success or not payload.get("ok"):
            error = payload.get("error") if isinstance(payload, dict) else None
            code = "invalid_response"
            message = f"浏览器 RPC 调用失败（HTTP {response.status_code}）"
            if isinstance(error, dict):
                code = str(error.get("code") or code)
                message = str(error.get("message") or message)
            raise BrowserRpcError(code, message, status=response.status_code)

        result = payload.get("result")
        if not isinstance(result, dict):
            raise BrowserRpcError("invalid_response", f"浏览器 RPC 返回结构异常（{method}）")
        return result

    # ── 便利方法：只读与导航 ──────────────────────────────────────────────

    async def health(self) -> dict:
        return await self.call("health")

    async def tabs(self) -> list[dict]:
        result = await self.call("tabs.list")
        tabs = result.get("tabs")
        return tabs if isinstance(tabs, list) else []

    async def active_tab(self) -> dict:
        return await self.call("tabs.active")

    async def navigate(self, url: str) -> dict:
        return await self.call("page.navigate", {"url": url}, timeout=NAVIGATE_TIMEOUT)

    async def current_url(self) -> dict:
        return await self.call("page.url")

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> BrowserRpcClient:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()
