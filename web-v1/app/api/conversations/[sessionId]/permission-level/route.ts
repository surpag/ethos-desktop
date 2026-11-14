import { currentUserId } from "@/lib/auth/current-user";

const runtimeURL = process.env.EDITH_RUNTIME_URL ?? "http://127.0.0.1:8080";

const LEVELS = new Set(["", "read_only", "approval", "full"]);

// PUT /api/conversations/{sessionId}/permission-level
// body: { permissionLevel: "" | "read_only" | "approval" | "full" }
// 语义是**上限**：只能把这段对话的档位收得更严，不能放宽用户级设置。
// 响应带 settings 与 effective 两个值 —— 两者不同时界面必须说明原因，
// 否则用户会以为自己选的档位没生效。
export async function PUT(
  request: Request,
  context: RouteContext<"/api/conversations/[sessionId]/permission-level">,
) {
  const userId = await currentUserId();
  if (!userId) return Response.json({ error: "Unauthorized" }, { status: 401 });

  const { sessionId } = await context.params;
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
    `/internal/conversations/${encodeURIComponent(sessionId)}/permission-level`,
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
