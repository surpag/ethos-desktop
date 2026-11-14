import { currentUserId } from "@/lib/auth/current-user";

const runtimeURL = process.env.EDITH_RUNTIME_URL ?? "http://127.0.0.1:8080";

// PUT /api/workspaces/{id}/permission-level
// 设置 / 清除项目级执行档位。body: { permissionLevel: string }
// 三档之一 = 设为项目级；空串 = 清除（回到跟随用户设置）。
//
// 语义是**覆盖**不是上限：项目设了就以它为准，既可能更严也可能更宽。
// 这是有意的——项目级是用户针对某个仓库做的更具体的一次授权。
export async function PUT(
  request: Request,
  context: RouteContext<"/api/workspaces/[id]/permission-level">,
) {
  const userId = await currentUserId();
  if (!userId) return Response.json({ error: "Unauthorized" }, { status: 401 });

  let value: unknown;
  try {
    value = await request.json();
  } catch {
    return Response.json({ error: "Invalid body" }, { status: 400 });
  }
  const body = value as { permissionLevel?: unknown };
  const permissionLevel =
    typeof body.permissionLevel === "string" ? body.permissionLevel.trim() : "";

  const { id } = await context.params;
  return forward(
    `${runtimeURL}/internal/workspaces/${encodeURIComponent(id)}/permission-level`,
    {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ userId, permissionLevel }),
    },
    request,
  );
}

async function forward(url: string | URL, init?: RequestInit, request?: Request) {
  const requestId =
    request?.headers.get("X-Request-ID") ?? crypto.randomUUID();
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
