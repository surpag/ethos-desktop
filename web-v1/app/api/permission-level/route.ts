import { currentUserId } from "@/lib/auth/current-user";

const runtimeURL = process.env.EDITH_RUNTIME_URL ?? "http://127.0.0.1:8080";

// PUT /api/permission-level
// body: { permissionLevel: string, workspaceId?: string }
//
// 一个入口，两个落点：
//   - 带 workspaceId → 改那个工作区的档位（覆盖用户级）；
//   - 不带           → 改用户级全局档位。
//
// 客户端只表达「这个对话现在按几档跑」，落点由服务端按有没有工作区决定；
// 让前端自己挑调用哪个接口，等于把这条规则复制两份。
export async function PUT(request: Request) {
  const userId = await currentUserId();
  if (!userId) return Response.json({ error: "Unauthorized" }, { status: 401 });

  let value: unknown;
  try {
    value = await request.json();
  } catch {
    return Response.json({ error: "Invalid body" }, { status: 400 });
  }
  const body = value as { permissionLevel?: unknown; workspaceId?: unknown };
  const permissionLevel =
    typeof body.permissionLevel === "string" ? body.permissionLevel.trim() : "";
  const workspaceId =
    typeof body.workspaceId === "string" ? body.workspaceId.trim() : "";

  const target = workspaceId
    ? `${runtimeURL}/internal/workspaces/${encodeURIComponent(workspaceId)}/permission-level`
    : `${runtimeURL}/internal/user-settings/permission-level`;

  return forward(
    target,
    {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ userId, permissionLevel }),
    },
    request,
  );
}

async function forward(url: string | URL, init?: RequestInit, request?: Request) {
  const requestId = request?.headers.get("X-Request-ID") ?? crypto.randomUUID();
  try {
    const response = await fetch(url, {
      ...init,
      headers: { ...init?.headers, "X-Request-ID": requestId },
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
