/**
 * 标签页控制权：谁在操作这个浏览器。
 *
 * 三种归属与它们的来源：
 * 1. `user` —— 用户在工具栏/标签栏动手（IPC），或 Agent 的租约过期；
 * 2. `agent` —— 某个 Agent run（`request_id`）正在通过 RPC 操作；
 * 3. 租约过期 —— run 崩溃/被取消后没人来释放，只能靠超时兜底。
 *
 * 为什么需要它（而不是「谁先到谁操作」）：
 * 多个 run 同时点同一个页面时，A 取到的 ref 会在 B 导航后指到别的元素上。
 * 元素级复核能兜住一部分，但两个 run 互相踩会让模型陷入「取快照 → 被踩 →
 * 再取」的死循环。串行化是最便宜也最可解释的做法。
 *
 * 两条刻意的产品行为：
 * - **用户永远优先**：任何 IPC 操作立即夺回控制权，Agent 的下一步写操作会被拒；
 * - **不让 Agent 反复反抢**：用户刚操作后的 GRAB_GUARD_MS 内，Agent 拿到的是
 *   `user_took_control`（可自救），过了这段时间才允许自动接管。否则用户点一下
 *   地址栏，Agent 立刻抢回去，界面上会看到控制权来回闪。
 */

export type ControlOwner = "user" | "agent";

export interface BrowserControlState {
  owner: ControlOwner;
  /** 持有控制权的 Agent run；`user` 时恒为 null。 */
  requestId: string | null;
}

/** Agent 多久没有任何操作就自动释放。run 崩了没人调 release，只能靠超时。 */
const AGENT_LEASE_MS = 20_000;

/** 用户动手后的这段时间内，Agent 不许自动反抢。 */
const USER_GRAB_GUARD_MS = 1_500;

export type AcquireResult = "granted" | "busy" | "user_took_control";

export interface BrowserControlOptions {
  /** 仅供验收脚本缩短等待；生产路径用默认值。 */
  leaseMs?: number;
  grabGuardMs?: number;
}

export class BrowserControl {
  private owner: ControlOwner = "user";
  private requestId: string | null = null;
  private leasedAt = 0;
  private userActedAt = 0;
  private readonly leaseMs: number;
  private readonly grabGuardMs: number;

  constructor(options: BrowserControlOptions = {}) {
    this.leaseMs = options.leaseMs ?? AGENT_LEASE_MS;
    this.grabGuardMs = options.grabGuardMs ?? USER_GRAB_GUARD_MS;
  }

  /** 当前归属（惰性处理租约过期，读操作不会改页面，但状态要准确）。 */
  snapshot(now = Date.now()): BrowserControlState {
    this.expireIfNeeded(now);
    return { owner: this.owner, requestId: this.requestId };
  }

  /** 用户动手：立即夺回，并压制 Agent 的反抢窗口。 */
  userTakeover(now = Date.now()): void {
    this.owner = "user";
    this.requestId = null;
    this.leasedAt = 0;
    this.userActedAt = now;
  }

  /**
   * Agent 想操作页面。
   *
   * - 空 `requestId`：兼容探针/旧调用，直接放行且不改变归属；
   * - 同一 run 重入：刷新租约；
   * - 另一个 run 持有：`busy`（不可自救，模型应停下）；
   * - 用户刚操作过：`user_took_control`（可自救，稍后重试）；
   * - 其余：接管。
   */
  acquire(requestId: string, now = Date.now()): AcquireResult {
    const run = (requestId || "").trim();
    if (!run) return "granted";
    this.expireIfNeeded(now);

    if (this.owner === "agent" && this.requestId && this.requestId !== run) {
      return "busy";
    }
    if (this.owner === "user" && now - this.userActedAt < this.grabGuardMs) {
      return "user_took_control";
    }

    this.owner = "agent";
    this.requestId = run;
    this.leasedAt = now;
    return "granted";
  }

  /**
   * 主动释放（run 结束/取消时由 Python 调用）。
   *
   * 只释放自己持有的租约，别人的请求幂等忽略：一个刚启动的 run 不该因为
   * 上一个 run 的收尾调用而丢掉刚拿到的控制权。
   */
  release(requestId: string): boolean {
    const run = (requestId || "").trim();
    if (!run) return false;
    if (this.owner !== "agent" || this.requestId !== run) return false;
    this.owner = "user";
    this.requestId = null;
    this.leasedAt = 0;
    return true;
  }

  /** 退出/销毁时清空，避免窗口重建后还挂着一个旧 run。 */
  reset(): void {
    this.owner = "user";
    this.requestId = null;
    this.leasedAt = 0;
    this.userActedAt = 0;
  }

  private expireIfNeeded(now: number): void {
    if (this.owner !== "agent") return;
    if (now - this.leasedAt <= this.leaseMs) return;
    this.owner = "user";
    this.requestId = null;
    this.leasedAt = 0;
  }
}
