"""端到端验证：本地沙箱是否真的能当作 E2B 用。

两轮，同一个会话：
  第一轮 —— 写文件、用 shell 确认、读回；
  第二轮 —— **不重新创建**，只确认上一轮的文件还在（跨 run 持久化）。

跑法：.venv\\Scripts\\python.exe ..\\scripts\\e2e_check.py http://127.0.0.1:<port>

注意 `trust_env=False`：本机 shell 的 http_proxy 会把 loopback 请求也送去代理，
表现为"同一连接第二个请求 404"——那是代理劫持，不是后端问题。
"""
from __future__ import annotations

import json
import sys
import time
import uuid

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8080"
USER = "local-user"
MODEL = "custom.model:b7f5d46d"  # kimi-k3.1-pro

TURN1 = (
    "请做三件事，每件都必须用工具实际执行：\n"
    "1) 在工作区根目录创建文件 e2e-check.txt，内容为 hello-from-desktop；\n"
    "2) 用 shell 命令列出工作区文件，确认 e2e-check.txt 存在；\n"
    "3) 读回 e2e-check.txt 的内容并原样贴出来。\n"
    "完成后简短汇报每一步实际结果。"
)
TURN2 = (
    "不要创建任何文件，也不要重写它。请只做一件事：确认上一轮创建的 e2e-check.txt "
    "现在是否还在，并原样贴出它当前的内容。如果不在，明确说\"文件已丢失\"。"
)


def main() -> int:
    with httpx.Client(timeout=httpx.Timeout(600.0, connect=5.0), trust_env=False) as c:
        r = c.post(
            f"{BASE}/internal/workspaces",
            json={"userId": USER, "name": f"e2e-{uuid.uuid4().hex[:6]}"},
        )
        r.raise_for_status()
        ws = r.json()["workspaceId"]
        sid = f"e2e-sess-{uuid.uuid4().hex[:8]}"
        print(f"[workspace] {ws}\n[session  ] {sid}")

        t1 = run_turn(c, ws, sid, TURN1, "第一轮")
        t2 = run_turn(c, ws, sid, TURN2, "第二轮（跨 run 持久化）")

        print("\n=== 汇总 ===")
        for label, stat in (("第一轮", t1), ("第二轮", t2)):
            print(
                f"{label}: 工具 {stat['tools']} 次 / 失败 {stat['failed']} 次 / "
                f"{stat['elapsed']:.1f}s / 证据 {stat['evidence']}"
            )
        print(f"第二轮回复含 hello-from-desktop: {t2['saw_content']}")
        return 0 if t2["saw_content"] else 2


def run_turn(
    c: httpx.Client, ws: str, sid: str, message: str, label: str
) -> dict:
    req_id = f"e2e-{uuid.uuid4().hex[:10]}"
    body = {
        "requestId": req_id,
        "userId": USER,
        "sessionId": sid,
        "workspaceId": ws,
        "modelId": MODEL,
        "message": message,
    }
    tools: list[str] = []
    failed = 0
    text: list[str] = []
    evidence = ""
    started = time.time()
    print(f"\n----- {label} -----")

    with c.stream("POST", f"{BASE}/internal/gateway/messages:stream", json=body) as resp:
        if resp.status_code != 200:
            print("HTTP", resp.status_code, resp.read().decode()[:500])
            raise SystemExit(1)
        buf: list[str] = []
        for line in resp.iter_lines():
            if line.startswith("data:"):
                buf.append(line[5:].strip())
            elif line == "" and buf:
                payload = "\n".join(buf)
                buf = []
                try:
                    event = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                etype = event.get("type", "")
                if etype == "tool.started":
                    name = event.get("toolName") or "?"
                    tools.append(name)
                    print(f"  → {name} {_brief(event)}")
                elif etype == "tool.finished":
                    if event.get("toolStatus") != "completed" or _is_error(event):
                        failed += 1
                        print(f"    ✗ {_brief(event, 260)}")
                elif etype in ("text.delta", "assistant.delta", "message.delta"):
                    text.append(event.get("delta") or event.get("text") or "")
                elif etype in ("run.completed", "run.failed", "run.aborted"):
                    evidence = event.get("completionEvidence") or etype

    reply = "".join(text)
    print(f"  回复: {reply[:600]}")
    return {
        "tools": len(tools),
        "failed": failed,
        "elapsed": time.time() - started,
        "evidence": evidence,
        "saw_content": "hello-from-desktop" in reply,
    }


def _brief(event: dict, limit: int = 160) -> str:
    payload = event.get("arguments") or event.get("toolResult") or ""
    if isinstance(payload, str) and len(payload) > limit:
        return payload[:limit] + "…"
    return json.dumps(payload, ensure_ascii=False)[:limit] if not isinstance(payload, str) else payload


def _is_error(event: dict) -> bool:
    raw = event.get("toolResult") or ""
    if not isinstance(raw, str):
        return False
    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        return False
    if not isinstance(result, dict):
        return False
    return bool(result.get("kind") == "command_error" or result.get("exitCode") not in (0, None))


if __name__ == "__main__":
    raise SystemExit(main())
