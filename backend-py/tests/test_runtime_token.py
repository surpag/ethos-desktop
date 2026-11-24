"""本地 Runtime 的入站鉴权与 CORS 收紧。

守两件事：

1. 配了 `EDITH_RUNTIME_TOKEN` 之后，没有令牌的请求（包括跨站请求）一律 401；
   `/health` 与 `OPTIONS` 例外——前者是启动探针，后者由 CORS 层处理。
2. CORS 不再 `*`：跨站页面即使拿到端口也读不到响应。

不配令牌时保持放行（开发直连 / pytest），这条也在用例里钉住，
否则"本地开发能跑"会在某次改动后悄悄坏掉。
"""

from __future__ import annotations

from fastapi.testclient import TestClient

TOKEN = "test-runtime-token-0123456789"


def test_open_when_token_not_configured(client: TestClient, monkeypatch) -> None:
    monkeypatch.delenv("EDITH_RUNTIME_TOKEN", raising=False)
    assert client.get("/health").status_code == 200
    assert client.get("/internal/models").status_code == 200


def test_missing_token_is_rejected(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("EDITH_RUNTIME_TOKEN", TOKEN)
    response = client.get("/internal/models")
    assert response.status_code == 401
    assert response.json()["type"] == "unauthorized"


def test_wrong_token_is_rejected(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("EDITH_RUNTIME_TOKEN", TOKEN)
    response = client.get("/internal/models", headers={"X-ETHOS-RUNTIME-TOKEN": "nope"})
    assert response.status_code == 401


def test_correct_token_is_accepted(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("EDITH_RUNTIME_TOKEN", TOKEN)
    response = client.get("/internal/models", headers={"X-ETHOS-RUNTIME-TOKEN": TOKEN})
    assert response.status_code == 200


def test_bearer_and_basic_are_accepted(client: TestClient, monkeypatch) -> None:
    """兼容两种 Authorization：桌面端现在用自定义头，Basic 留给 URL 带凭据的形态。"""
    monkeypatch.setenv("EDITH_RUNTIME_TOKEN", TOKEN)
    bearer = client.get("/internal/models", headers={"Authorization": f"Bearer {TOKEN}"})
    assert bearer.status_code == 200
    import base64

    basic_value = base64.b64encode(f"ethos:{TOKEN}".encode()).decode()
    basic = client.get("/internal/models", headers={"Authorization": f"Basic {basic_value}"})
    assert basic.status_code == 200


def test_health_stays_open_with_token(client: TestClient, monkeypatch) -> None:
    """Electron 在拿到令牌之前就要用 /health 判断进程起没起来。"""
    monkeypatch.setenv("EDITH_RUNTIME_TOKEN", TOKEN)
    assert client.get("/health").status_code == 200


def test_preflight_is_not_blocked_by_token(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("EDITH_RUNTIME_TOKEN", TOKEN)
    response = client.options(
        "/internal/models",
        headers={
            "Origin": "http://127.0.0.1:51234",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://127.0.0.1:51234"


def test_cors_does_not_allow_foreign_origin(client: TestClient, monkeypatch) -> None:
    """收紧前这里是 `*`：任何网页都能读走本机 Runtime 的响应。"""
    monkeypatch.delenv("EDITH_RUNTIME_TOKEN", raising=False)
    response = client.get("/internal/models", headers={"Origin": "https://evil.example.com"})
    assert response.headers.get("access-control-allow-origin") != "*"
    assert "evil.example.com" not in response.headers.get("access-control-allow-origin", "")
