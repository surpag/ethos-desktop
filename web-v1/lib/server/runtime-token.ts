// BFF → ETHOS Runtime 的入站令牌注入。
//
// 为什么在 fetch 这一层做：39 个 route 里 33 个是手写 `fetch(`${runtimeURL}${path}`)`，
// 逐个加 header 既容易漏，也会随着新路由继续漏。`instrumentation.register()` 在
// 服务进程启动时跑一次，这里包一层**只对 Runtime origin 生效**的 fetch，
// 新加的路由自动继承——「注令牌」从此只有一个地方可以忘记。

export const RUNTIME_TOKEN_HEADER = "X-ETHOS-RUNTIME-TOKEN";

function configuredToken(): string {
  return process.env.EDITH_RUNTIME_TOKEN?.trim() ?? "";
}

/** Runtime 的 origin（`http://127.0.0.1:<port>`）；未配置或非法时返回 null。 */
export function runtimeOrigin(): string | null {
  const base = process.env.EDITH_RUNTIME_URL?.trim();
  if (!base) return null;
  try {
    return new URL(base).origin;
  } catch {
    return null;
  }
}

/**
 * 在已有 header 上补令牌。
 *
 * 保留原 header 的全部内容并返回 `Headers`：调用方既有传对象字面量的，
 * 也有传 `Headers` 实例的，统一过一次 `Headers` 才不会漏掉其中一种。
 */
export function withRuntimeToken(
  headers: HeadersInit | undefined,
  token: string = configuredToken(),
): Headers {
  const merged = new Headers(headers);
  if (token) merged.set(RUNTIME_TOKEN_HEADER, token);
  return merged;
}

let installed = false;

/** 幂等：装一次就够，重复调用（多进程/热重载）不会叠加包装。 */
export function installRuntimeToken(): void {
  const token = configuredToken();
  const origin = runtimeOrigin();
  if (installed || !token || !origin) return;
  installed = true;

  const original = globalThis.fetch;
  globalThis.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
    const url =
      typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
    // 只碰 Runtime：Next 自身的静态资源、外网请求都不动。
    if (!url.startsWith(`${origin}/`)) return original(input as RequestInfo, init);
    if (input instanceof Request) {
      const headers = withRuntimeToken(input.headers, token);
      return original(new Request(input, { headers }), init);
    }
    return original(input as RequestInfo, {
      ...init,
      headers: withRuntimeToken(init?.headers, token),
    });
  }) as typeof fetch;
}
