import { currentUserId } from "@/lib/auth/current-user";

const runtimeURL = process.env.EDITH_RUNTIME_URL ?? "http://127.0.0.1:8080";

// PUT /api/images/{imageId}/blob — 浏览器上传图片字节的落点。
//
// 上游这里是 COS 的预签名地址，浏览器绕过 BFF 直传对象存储；桌面端没有对象
// 存储，字节必须由本进程收下再写盘。走 BFF 而不是让浏览器直连 runtime 的原因
// 有两条：同源（不引入 CORS 预检）、以及 userId 只能在服务端注入。
//
// 用 arrayBuffer 而不是流式转发：单图上限 10 MB，缓冲的代价是有界的，而
// 流式转发在 Node fetch 上要 `duplex: "half"` 且一旦上游提前关闭就会静默截断
// ——截断的图片在确认阶段才会被发现，错误会离现场很远。
export async function PUT(
  request: Request,
  context: RouteContext<"/api/images/[imageId]/blob">,
) {
  const userId = await currentUserId();

  const { imageId } = await context.params;
  const body = await request.arrayBuffer();
  if (body.byteLength === 0) {
    return Response.json({ error: "Empty image body" }, { status: 400 });
  }

  const url = new URL(
    `/internal/images/${encodeURIComponent(imageId)}/blob`,
    runtimeURL,
  );
  url.searchParams.set("userId", userId);

  try {
    const response = await fetch(url, {
      method: "PUT",
      headers: {
        "Content-Type":
          request.headers.get("content-type") ?? "application/octet-stream",
      },
      body,
      cache: "no-store",
    });
    return new Response(response.body, {
      status: response.status,
      headers: {
        "Content-Type":
          response.headers.get("Content-Type") ?? "application/json",
        "Cache-Control": "no-store",
      },
    });
  } catch {
    return Response.json({ error: "ETHOS runtime is unavailable" }, { status: 502 });
  }
}
