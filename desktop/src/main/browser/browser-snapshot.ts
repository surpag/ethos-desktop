import { randomBytes } from "node:crypto";

/**
 * 页面观测（observation）的登记与校验。
 *
 * 为什么需要它：模型看到的是一份「文字版页面」，它下一次点的是 `ref`。从取快照到点击
 * 之间，页面可能已经导航、被脚本改掉、或者用户切了标签页。没有绑定关系的话，
 * `el_003` 在新页面上可能是一个「删除」按钮——这就是典型的「模型以为点的是搜索，
 * 实际点的是删除」。
 *
 * 校验四件事，缺一不可：
 *   1. 快照归这个标签页（tabId）；
 *   2. 快照之后页面没有发生主文档导航（pageVersion）；
 *   3. ref 出现在这份快照里（不是模型编的）；
 *   4. 操作时页面上的元素仍是快照里那个（`fingerprints`，页面侧复核）。
 *
 * 第 4 条是阶段 5 加的：`did-navigate-in-page`（history.pushState / hash 变化）
 * 不递增 pageVersion，但 SPA 可以把整块内容换掉。只靠前三条会漏。
 *
 * 分页（阶段 5）：一次「观察」可以分多页返回，所有页共享同一个 observationId，
 * 后一页的 ref 会并入同一份白名单——否则「翻到第二页后想点第一页的搜索框」会报
 * 快照过期，模型只能在两页之间反复重取。
 */

export class SnapshotError extends Error {
  constructor(
    readonly code: string,
    message: string,
  ) {
    super(message);
  }
}

export interface SnapshotElementInput {
  ref: string;
  /** 页面侧计算的指纹：`tag|role|稳定名称`。 */
  fp: string;
}

export interface Observation {
  observationId: string;
  tabId: string;
  pageVersion: number;
  refs: Set<string>;
  /** ref → 指纹。写操作前在页面侧重新计算并比对。 */
  fingerprints: Map<string, string>;
  /** 页面里可见可交互元素的总数（受 hard limit 截断）。 */
  total: number;
  /** 第一页用的每页大小；后续翻页沿用它，避免「第一页 5 条、第二页 120 条」。 */
  pageSize: number;
  /** 已返回的页数，用于给「无限翻页」封顶。 */
  pages: number;
  /** 下一页从第几个可见元素开始；cursor 必须等于它。 */
  nextOffset: number;
  createdAt: number;
  url: string;
  title: string;
}

/** 一份观察最多翻这么多页：防止模型对着一个巨型列表一直翻。 */
export const MAX_PAGES_PER_OBSERVATION = 5;

export class SnapshotStore {
  private readonly byId = new Map<string, Observation>();

  /**
   * 新建观察。
   *
   * **同一标签页只保留最新一份**：页面上的 `data-ethos-ref` 属性是全局命名空间，
   * 重新取快照时 ref 会被重新编号。若旧观察还留着，模型拿旧 ref 点击时快照校验
   * 会通过，但属性已经指到别的元素上——这正是「点错东西」。所以新快照一建立，
   * 同标签页的旧快照立即作废（模型必须用新返回的 ref）。
   */
  create(input: {
    tabId: string;
    pageVersion: number;
    elements: SnapshotElementInput[];
    total: number;
    pageSize: number;
    url: string;
    title: string;
  }): Observation {
    this.invalidateTab(input.tabId);
    const observation: Observation = {
      observationId: `obs_${randomBytes(4).toString("hex")}`,
      tabId: input.tabId,
      pageVersion: input.pageVersion,
      refs: new Set(input.elements.map((item) => item.ref)),
      fingerprints: new Map(input.elements.map((item) => [item.ref, item.fp])),
      total: input.total,
      pageSize: input.pageSize,
      pages: 1,
      nextOffset: input.elements.length,
      createdAt: Date.now(),
      url: input.url,
      title: input.title,
    };
    this.byId.set(observation.observationId, observation);
    return observation;
  }

  /**
   * 把下一页并入同一份观察。
   *
   * 调用方必须先用 `validate()` 校验 cursor 里的 observation / tab / pageVersion，
   * 这里再校验一次「cursor 指向的正是下一段」，避免模型自己拼一个 offset 跳着翻。
   */
  extend(
    observationId: string,
    input: { tabId: string; pageVersion: number; elements: SnapshotElementInput[] },
  ): Observation {
    const observation = this.validate(observationId, input.tabId, input.pageVersion);
    if (observation.pages >= MAX_PAGES_PER_OBSERVATION) {
      throw new SnapshotError(
        "invalid_arguments",
        `这份快照最多翻 ${MAX_PAGES_PER_OBSERVATION} 页；请先用 browser_scroll 或缩小范围后重新取快照`,
      );
    }
    for (const item of input.elements) {
      observation.refs.add(item.ref);
      observation.fingerprints.set(item.ref, item.fp);
    }
    observation.pages += 1;
    observation.nextOffset += input.elements.length;
    return observation;
  }

  /**
   * 取快照并校验归属；`ref` 给了就顺带校验它在不在快照里。
   *
   * 所有失败都用同一个 `stale_observation` 表达「这份观测作废了」，只说原因不说细节：
   * 对模型而言「重新取一次快照」是唯一正确的动作，区分「tab 不对」和「页面变了」
   * 只会让它尝试各种歪招。
   */
  validate(
    observationId: string,
    tabId: string,
    pageVersion: number,
    ref?: string,
  ): Observation {
    const observation = this.byId.get(observationId);
    if (!observation) {
      throw new SnapshotError(
        "stale_observation",
        "这份页面快照不存在或已过期，请重新调用 browser_get_page_snapshot",
      );
    }
    if (observation.tabId !== tabId) {
      throw new SnapshotError(
        "stale_observation",
        "快照属于另一个标签页，请对当前标签页重新取快照",
      );
    }
    if (observation.pageVersion !== pageVersion) {
      throw new SnapshotError(
        "stale_observation",
        "取快照之后页面已经跳转，请重新调用 browser_get_page_snapshot",
      );
    }
    if (ref !== undefined && !observation.refs.has(ref)) {
      throw new SnapshotError(
        "element_missing",
        `快照里没有 ${ref} 这个元素，请重新取快照后使用返回的 ref`,
      );
    }
    return observation;
  }

  /** ref 的期望指纹；`validate()` 之后调用，ref 一定存在。 */
  fingerprint(observation: Observation, ref: string): string | undefined {
    return observation.fingerprints.get(ref);
  }

  /** 导航发生时整页作废：不清的话，旧 ref 会继续在内存里“看起来有效”。 */
  invalidateTab(tabId: string): void {
    for (const [id, observation] of this.byId) {
      if (observation.tabId === tabId) this.byId.delete(id);
    }
  }

  clear(): void {
    this.byId.clear();
  }
}
