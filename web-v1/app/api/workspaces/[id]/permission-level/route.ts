import { currentUserId } from "@/lib/auth/current-user";

const runtimeURL = process.env.EDITH_RUNTIME_URL ?? "http://127.0.0.1:8080";

// 三档 + 空串（清除）。严格校验而不是透传：
// 非法值在 BFF 就拒，用户能立刻看到「这个值不合法」，而不是等后端 400
// 变成一个通用的请求失败提示。
const LEVELS = new Set(["", "read_only", "approval", "full"]);

// PUT /api/workspaces/{id}/permission-level
// body: { permissionLevel: "" | "read_only" | "approval" | "full" }
// 语义是**上限**：只能把该工作区的档位收得更严，不能放宽用户级设置。
export async function PUT(
  request: Request,
  context: RouteContext<"/api/workspaces/[id]/permission-level">,
) {
  const userId = await currentUserId();
  if (!userId) return Response.json({ error: "Unauthorized" }, { status: 401 });

  const { id } = await context.params;
  let value: unknown;
  try {
    value = await request.json();
  } catch {
    return Response.json({ error: "Invalid body" }, { status: 400 });
  }
  const body = value as { permissionLevel?: unknown };
  const level =
    typeof body.permissionLevel === "string" ? body.permissionLevel.trim() : "";
  if (!LEVELS.has(level)) {
    return Response.json(
      { error: "permissionLevel must be read_only / approval / full or empty" },
      { status: 400 },
    );
  }

  const url = new URL(
    `/internal/workspaces/${encodeURIComponent(id)}/permission-level`,
    runtimeURL,
  );
  try {
    const response = await fetch(url, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ userId, permissionLevel: level }),
      cache: "no-store",
    });
    return new Response(response.body, {
      status: response.status,
      headers: {
        "Content-Type": response.headers.get("Content-Type") ?? "application/json",
        "Cache-Control": "no-store",
      },
    });
  } catch {
    return Response.json({ error: "ETHOS runtime is unavailable" }, { status: 502 });
  }
}
