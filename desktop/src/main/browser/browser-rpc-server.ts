import { randomBytes, timingSafeEqual } from "node:crypto";
import { createServer, type IncomingMessage, type Server, type ServerResponse } from "node:http";

import type { BrowserManager } from "./browser-manager";
import { SnapshotError } from "./browser-snapshot";

/**
 * 浏览器 RPC：Python 后端操作共享浏览器的唯一入口。
 *
 * 为什么是「Electron 起服务、Python 当客户端」而不是反过来：
 * 浏览器资源（WebContentsView）只能在主进程里碰，Electron 是天然的宿主；
 * 而 Python 的启动参数已经由 Electron 注入（`EDITH_RUNTIME_TOKEN` 就是这么传的），
 * 多塞两个 env 比让 Python 反向监听、再让 Electron 去连要简单得多。
 *
 * 安全边界（与 Runtime HTTP 令牌**刻意分开**）：
 * - 只监听 127.0.0.1，端口由系统分配（`listen(0)`），不硬编码；
 * - 独立随机令牌，每次启动重新生成，只存在于 backend 子进程的 env 里；
 * - 单端点 + 方法白名单，不存在「任意 IPC / 任意 JS / 任意系统命令」的能力；
 * - 请求体上限、频率上限、单次处理超时，避免恶意/异常客户端拖死主进程；
 * - 令牌比较用 `timingSafeEqual`，且**从不写日志**。
 *
 * 阶段 3 只开放只读与导航；点击/输入等写操作在阶段 4 随工具一起开放。
 */

const MAX_BODY_BYTES = 64 * 1024;
/** 超过这个量就不再往内存里收——防止一个畸形请求把主进程内存打满。 */
const HARD_CAP_BYTES = 1024 * 1024;
const HANDLER_TIMEOUT_MS = 15_000;
const RATE_LIMIT_PER_SECOND = 20;

/** 这些方法的结果里没有 tabId，需要 RPC 层补（trace 归档用）。 */
const TABID_LESS_METHODS = new Set([
  "page.click",
  "page.fill",
  "page.press",
  "page.scroll",
]);

const METHODS = new Set([
  "health",
  "tabs.list",
  "tabs.active",
  "page.navigate",
  "page.url",
  "page.snapshot",
  "page.click",
  "page.fill",
  "page.press",
  "page.scroll",
  "control.release",
]);

export interface BrowserRpcHandle {
  /** 形如 `http://127.0.0.1:<port>`，不含路径。 */
  readonly url: string;
  readonly token: string;
  /** 浏览器管理器建好之后再接上；在此之前所有方法返回 `browser_not_ready`。 */
  attach(manager: BrowserManager): void;
  dispose(): void;
}

class RpcError extends Error {
  constructor(
    readonly code: string,
    message: string,
    readonly status = 400,
  ) {
    super(message);
  }
}

function sendJson(res: ServerResponse, status: number, payload: unknown): void {
  const body = Buffer.from(JSON.stringify(payload), "utf8");
  res.writeHead(status, {
    "content-type": "application/json; charset=utf-8",
    "content-length": String(body.byteLength),
    // 明确禁掉缓存：状态是实时的，缓存一份就是错的。
    "cache-control": "no-store",
  });
  res.end(body);
}

function fail(res: ServerResponse, status: number, code: string, message: string): void {
  sendJson(res, status, { ok: false, error: { code, message } });
}

/**
 * 读请求体。
 *
 * 超过 `MAX_BODY_BYTES` 之后**继续读完但不再缓存**，然后返回 413。
 * 为什么不像常见做法那样读到超限就 `req.destroy()`：那样客户端拿到的是连接重置
 * （`ECONNRESET`），根本读不到我们精心写的错误码，调用方只能报「网络错误」。
 * 真正的防护交给 `HARD_CAP_BYTES`：单个请求最多吞 1 MiB，再大就断。
 */
function readBody(req: IncomingMessage): Promise<string> {
  return new Promise((resolve, reject) => {
    const chunks: Buffer[] = [];
    let size = 0;
    let overflow = false;
    req.on("data", (chunk: Buffer) => {
      size += chunk.byteLength;
      if (size > HARD_CAP_BYTES) {
        reject(new RpcError("request_too_large", `请求体超过 ${HARD_CAP_BYTES} 字节`, 413));
        req.destroy();
        return;
      }
      if (size > MAX_BODY_BYTES) {
        overflow = true;
        chunks.length = 0;
        return;
      }
      chunks.push(chunk);
    });
    req.on("end", () => {
      if (overflow) {
        reject(new RpcError("request_too_large", `请求体超过 ${MAX_BODY_BYTES} 字节`, 413));
        return;
      }
      resolve(Buffer.concat(chunks).toString("utf8"));
    });
    req.on("error", (error) => reject(error));
  });
}

function requiredString(raw: unknown, field: string): string {
  if (typeof raw !== "string" || !raw.trim()) {
    throw new RpcError("invalid_arguments", `缺少 ${field}`);
  }
  return raw.trim();
}

function safeEqual(a: string, b: string): boolean {
  const left = Buffer.from(a, "utf8");
  const right = Buffer.from(b, "utf8");
  if (left.byteLength !== right.byteLength) return false;
  return timingSafeEqual(left, right);
}

export async function startBrowserRpcServer(): Promise<BrowserRpcHandle> {
  const token = randomBytes(32).toString("hex");
  let manager: BrowserManager | null = null;
  let disposed = false;

  // 单令牌场景下的滑动窗口限频：记最近 1 秒的时间戳，超过就 429。
  const recentCalls: number[] = [];

  /**
   * 页面操作前的控制权闸门。
   *
   * 空 `request_id` 是兼容路径（探针、旧客户端），不参与控制权；
   * 真实 run 一律带 `request_id`，因此「两个任务同时点同一个页面」会被串行化。
   */
  function requireControl(requestId: string): void {
    if (!manager) return;
    const outcome = manager.acquireControl(requestId);
    if (outcome === "busy") {
      throw new RpcError(
        "browser_busy",
        "另一个任务正在操作这个浏览器，请等它结束后再试",
        409,
      );
    }
    if (outcome === "user_took_control") {
      throw new RpcError(
        "user_took_control",
        "用户刚刚接管了页面，请稍后重新取快照再操作",
        409,
      );
    }
  }

  async function dispatch(
    method: string,
    params: Record<string, unknown>,
    requestId: string,
  ): Promise<unknown> {
    if (method === "health") {
      return {
        backend: "electron",
        managerReady: manager !== null,
        tabs: manager ? manager.state().tabs.length : 0,
        activeTabId: manager ? manager.state().activeTabId : null,
      };
    }
    if (!manager) {
      throw new RpcError("browser_not_ready", "浏览器尚未初始化", 503);
    }
    const state = manager.state();
    switch (method) {
      case "tabs.list":
        return {
          tabs: state.tabs.map((tab) => ({
            tabId: tab.tabId,
            url: tab.url,
            title: tab.title,
            loading: tab.loading,
            active: tab.tabId === state.activeTabId,
          })),
        };
      case "tabs.active": {
        const active = state.tabs.find((tab) => tab.tabId === state.activeTabId);
        if (!active) throw new RpcError("no_active_tab", "当前没有选中的标签页", 409);
        return { tabId: active.tabId, url: active.url, title: active.title, loading: active.loading };
      }
      case "page.navigate": {
        requireControl(requestId);
        const url = params.url;
        if (typeof url !== "string" || !url.trim()) {
          throw new RpcError("invalid_arguments", "缺少 url");
        }
        // 只接受当前选中标签页的导航：阶段 4 起 Agent 默认操作「用户正在看的页面」，
        // 允许它指定任意 tabId 会让「用户看到的就是 Agent 操作的」这一前提失效。
        //
        // `loadURL` 的失败（目标端口没监听、DNS 解析不了、证书不对）**不是平台故障**。
        // 让它漏上去会被兜底成 500 Internal Server Error，模型只看到「内部错误」，
        // 而真实原因是「本地服务还没起来」——那恰是它自己能处理的信息。
        const tab = await manager.navigate(url).catch((error: unknown) => {
          const detail = error instanceof Error ? error.message : String(error);
          if (detail.includes("没有打开的标签页")) {
            throw new RpcError("no_active_tab", "当前没有选中的标签页", 409);
          }
          throw new RpcError("navigation_failed", `页面加载失败：${detail}`, 502);
        });
        return { tabId: tab.tabId, url: tab.url, title: tab.title };
      }
      case "page.url": {
        const active = state.tabs.find((tab) => tab.tabId === state.activeTabId);
        if (!active) throw new RpcError("no_active_tab", "当前没有选中的标签页", 409);
        return { tabId: active.tabId, url: active.url, title: active.title };
      }
      case "control.release":
        // 收尾调用不该被自己的租约挡住，也不 requireControl：
        // 只释放「自己持有的那一份」，别人的请求幂等忽略。
        return { released: manager.releaseControl(requestId) };
      case "page.snapshot":
        requireControl(requestId);
        return await manager.snapshot({
          maxElements: params.max_elements,
          cursor: params.cursor,
        });
      case "page.click":
        requireControl(requestId);
        return await manager.click(
          requiredString(params.observation_id, "observation_id"),
          requiredString(params.ref, "ref"),
        );
      case "page.fill":
        requireControl(requestId);
        return await manager.fill(
          requiredString(params.observation_id, "observation_id"),
          requiredString(params.ref, "ref"),
          typeof params.value === "string" ? params.value : "",
        );
      case "page.press":
        requireControl(requestId);
        return await manager.press(
          requiredString(params.observation_id, "observation_id"),
          requiredString(params.ref, "ref"),
          requiredString(params.key, "key"),
        );
      case "page.scroll":
        requireControl(requestId);
        return await manager.scroll(
          typeof params.direction === "string" ? params.direction : "down",
          typeof params.amount === "number" ? params.amount : 500,
        );
      default:
        throw new RpcError("method_not_allowed", `不支持的方法：${method}`, 404);
    }
  }

  const server: Server = createServer((req, res) => {
    void (async () => {
      try {
        if (disposed) {
          fail(res, 503, "server_closing", "服务正在关闭");
          return;
        }
        if (req.method !== "POST" || (req.url ?? "").split("?")[0] !== "/rpc") {
          fail(res, 404, "not_found", "只支持 POST /rpc");
          return;
        }

        // 鉴权先于一切解析：没带令牌的请求连 body 都不读。
        const header = req.headers.authorization ?? "";
        const presented = header.startsWith("Bearer ") ? header.slice(7) : "";
        if (!presented || !safeEqual(presented, token)) {
          // 不区分「没带」与「不对」，避免给出探测信号。
          fail(res, 401, "unauthorized", "令牌无效");
          return;
        }

        const now = Date.now();
        while (recentCalls.length > 0 && now - recentCalls[0] > 1000) recentCalls.shift();
        if (recentCalls.length >= RATE_LIMIT_PER_SECOND) {
          fail(res, 429, "rate_limited", "请求过于频繁");
          return;
        }
        recentCalls.push(now);

        const raw = await readBody(req);
        let payload: { method?: unknown; params?: unknown; request_id?: unknown };
        try {
          payload = JSON.parse(raw) as { method?: unknown; params?: unknown };
        } catch {
          throw new RpcError("invalid_json", "请求体不是合法 JSON");
        }
        const method = payload.method;
        if (typeof method !== "string" || !METHODS.has(method)) {
          throw new RpcError("method_not_allowed", `不支持的方法：${String(method)}`, 404);
        }
        const params = (payload.params ?? {}) as Record<string, unknown>;
        // 请求级身份：不是业务参数，不要混进 params（否则每个工具都要转发一遍）。
        const requestId =
          typeof payload.request_id === "string" ? payload.request_id.trim() : "";

        let result = await Promise.race([
          dispatch(method, params, requestId),
          new Promise((_resolve, reject) =>
            setTimeout(
              () => reject(new RpcError("timeout", `处理超时（${HANDLER_TIMEOUT_MS}ms）`, 504)),
              HANDLER_TIMEOUT_MS,
            ),
          ),
        ]);
        // click/fill/press/scroll 的结果原本不带 tabId；trace 要按标签页还原
        // 操作链，在这里统一补齐，避免每个 manager 方法各自重复一遍。
        if (
          TABID_LESS_METHODS.has(method) &&
          result !== null &&
          typeof result === "object" &&
          !Array.isArray(result) &&
          !("tabId" in (result as Record<string, unknown>)) &&
          manager
        ) {
          const activeTabId = manager.state().activeTabId;
          if (activeTabId) {
            result = { ...(result as Record<string, unknown>), tabId: activeTabId };
          }
        }
        sendJson(res, 200, { ok: true, result });
      } catch (error) {
        if (error instanceof RpcError) {
          fail(res, error.status, error.code, error.message);
          return;
        }
        // 快照/元素类错误是「业务上可以自救」的失败，不能算服务端内部错误：
        // 用 409 让调用方能把它与 500（真的坏了）区分开。
        if (error instanceof SnapshotError) {
          fail(res, error.code === "invalid_arguments" ? 400 : 409, error.code, error.message);
          return;
        }
        const message = error instanceof Error ? error.message : String(error);
        // 内部异常不把堆栈丢给客户端（那是给日志的），但要保留可读原因。
        fail(res, 500, "internal_error", message);
      }
    })();
  });

  await new Promise<void>((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => resolve());
  });

  const address = server.address();
  if (!address || typeof address === "string") {
    server.close();
    throw new Error("浏览器 RPC 无法获取监听端口");
  }
  const url = `http://127.0.0.1:${address.port}`;
  process.stderr.write(`[browser-rpc] listening ${url}\n`);

  return {
    url,
    token,
    attach(next: BrowserManager) {
      manager = next;
    },
    dispose() {
      if (disposed) return;
      disposed = true;
      manager = null;
      // closeAllConnections：keep-alive 连接不主动断开的话，退出会卡在这里等超时。
      server.closeAllConnections?.();
      server.close();
    },
  };
}
