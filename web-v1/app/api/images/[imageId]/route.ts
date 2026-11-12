import { currentUserId } from "@/lib/auth/current-user";

const runtimeURL = process.env.EDITH_RUNTIME_URL ?? "http://127.0.0.1:8080";

// GET /api/images/{imageId} — 浏览器 `<img src>` 用的稳定地址。
//
// 上游这里会 302 跳到一个 COS 签名地址（每次请求都要重签，所以只能在服务端
// 中转）；桌面端图片就在本机磁盘上，直接透传字节即可，不再需要跳转。
export async function GET(
  _request: Request,
  context: RouteContext<"/api/images/[imageId]">,
) {
  const userId = await currentUserId();

  const { imageId } = await context.params;
  const url = new URL(
    `/internal/images/${encodeURIComponent(imageId)}`,
    runtimeURL,
  );
  url.searchParams.set("userId", userId);
  try {
    const response = await fetch(url, { cache: "no-store" });
    return new Response(response.body, {
      status: response.status,
      headers: {
        "Content-Type":
          response.headers.get("Content-Type") ?? "application/json",
        "Cache-Control":
          response.headers.get("Cache-Control") ?? "no-store",
      },
    });
  } catch {
    return Response.json(
      { error: "ETHOS runtime is unavailable" },
      { status: 502 },
    );
  }
}
