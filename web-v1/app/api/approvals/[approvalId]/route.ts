import { currentUserId } from "@/lib/auth/current-user";

const runtimeURL = process.env.EDITH_RUNTIME_URL ?? "http://127.0.0.1:8080";

/**
 * 审批决策的 BFF 入口。
 *
 * 浏览器只能说「批准 / 拒绝」以及作用域，userId 由服务端解析后附加——
 * 和所有其他 BFF 一样，身份不能由调用方决定（否则任何登录用户都能替别人批准）。
 *
 * 409 原样透传：它表示「你的点击来晚了」（另一个标签页先处理 / 已超时 / run 已取消），
 * 不是错误。前端据此静默关闭面板，等 `approval.resolved` 事件同步终态。
 */
export async function POST(
  request: Request,
  { params }: { params: Promise<{ approvalId: string }> },
) {
  const userId = await currentUserId();
  if (!userId) return Response.json({ error: "Unauthorized" }, { status: 401 });

  let value: unknown;
  try {
    value = await request.json();
  } catch {
    return Response.json({ error: "Invalid decision" }, { status: 400 });
  }
  const body = toDecision(value);
  if (!body) return Response.json({ error: "Invalid decision" }, { status: 400 });

  const { approvalId } = await params;
  const url = `${runtimeURL}/internal/approvals/${encodeURIComponent(approvalId)}`;
  const requestId = request.headers.get("X-Request-ID") ?? crypto.randomUUID();
  try {
    const response = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Request-ID": requestId },
      body: JSON.stringify({ ...body, userId }),
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

function toDecision(value: unknown): { decision: "approve" | "deny"; scope: "once" | "run" } | null {
  if (typeof value !== "object" || value === null) return null;
  const raw = value as Record<string, unknown>;
  const decision = raw.decision;
  if (decision !== "approve" && decision !== "deny") return null;
  // 作用域只对「批准」有意义；拒绝时固定 once，不让调用方传一个无意义的组合。
  const scope = decision === "approve" && raw.scope === "run" ? "run" : "once";
  return { decision, scope };
}
