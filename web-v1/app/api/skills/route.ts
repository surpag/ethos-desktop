import { currentUserId } from "@/lib/auth/current-user";

const runtimeURL = process.env.EDITH_RUNTIME_URL ?? "http://127.0.0.1:8080";

// GET /api/skills 转发到 Runtime。桌面端只有一份 Skill 目录，没有作用域参数——
// 认证仍然过一遍 `currentUserId()`，是为了云端部署时这条路由不至于变成公开接口。
export async function GET(request: Request) {
  const userId = await currentUserId();
  if (!userId) return Response.json({ error: "Unauthorized" }, { status: 401 });

  return forward(new URL("/internal/skills", runtimeURL), request);
}

async function forward(url: string | URL, request?: Request) {
  const requestId =
    request?.headers.get("X-Request-ID") ?? crypto.randomUUID();
  try {
    const response = await fetch(url, {
      headers: { "X-Request-ID": requestId },
      cache: "no-store",
    });
    return new Response(response.body, {
      status: response.status,
      headers: {
        "Content-Type": response.headers.get("Content-Type") ?? "application/json",
        "Cache-Control": "no-store",
        "X-Request-ID": response.headers.get("X-Request-ID") ?? requestId,
      },
    });
  } catch {
    return Response.json({ error: "ETHOS runtime is unavailable" }, { status: 502 });
  }
}
