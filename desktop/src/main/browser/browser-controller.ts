import type { BrowserTab } from "./browser-tab";
import {
  clickRefScript,
  collectElementsScript,
  fillRefScript,
  pressRefScript,
  scrollPageScript,
} from "./browser-page-scripts";
import { MAX_PAGES_PER_OBSERVATION, SnapshotError, SnapshotStore } from "./browser-snapshot";

/**
 * 页面上真正的操作：快照、点击、填写、按键、滚动。
 *
 * 四条不变式（任何一条破了，浏览器 Agent 就会开始「点错东西」）：
 * 1. 模型只能提交 `ref`，不能提交 selector、不能提交 JS；
 * 2. 每次操作前用 `SnapshotStore` 核对「同一标签页 + 同一页面版本 + ref 在快照里」；
 * 3. 操作时页面侧再比一次元素指纹——SPA 换内容不递增 pageVersion，只有指纹能发现；
 * 4. 页面侧脚本只返回结构化结果，不把 DOM 或整页 HTML 带回来。
 */

const REF_PATTERN = /^el_\d{3}$/;
const CURSOR_PATTERN = /^(obs_[0-9a-f]{8}):(\d{1,4})$/;

const DEFAULT_MAX_ELEMENTS = 120;
const MAX_ELEMENTS_LIMIT = 200;

/**
 * 单次快照的输出字符预算。
 *
 * 执行链在上层还有 12000 字符的二次裁剪，这里必须留余量：一旦被上层裁掉，
 * 被砍掉的是 JSON 尾部——模型看到的是一份语法都不完整的快照。
 */
const MAX_CHARS = 10_000;

/** 每条元素在 JSON 里的固定开销（引号、字段名、括号）加上名称长度。 */
const PER_ELEMENT_COST = 80;

/** ref 是三位序号，硬上限 999；超过的元素用滚动分段逼出来，不编号。 */
const REF_HARD_LIMIT = 999;

const MAX_FILL_LENGTH = 2000;
const MAX_SCROLL_AMOUNT = 5000;
const SCROLL_DIRECTIONS = new Set(["up", "down", "top", "bottom"]);
/** 只放行这些键：通用按键事件很容易被页面用来做快捷键（Ctrl+S 之类）。 */
const ALLOWED_KEYS = new Set([
  "Enter",
  "Tab",
  "Escape",
  "Backspace",
  "ArrowUp",
  "ArrowDown",
  "ArrowLeft",
  "ArrowRight",
  " ",
]);

export interface SnapshotElement {
  ref: string;
  role: string;
  name: string;
  tag: string;
  disabled: boolean;
}

export interface SnapshotPage {
  /** 本页从第几个可见元素开始（0 起）。 */
  offset: number;
  /** 本页返回了多少个。 */
  returned: number;
  /** 页面里可见可交互元素总数（上限 999）。 */
  total: number;
}

export interface SnapshotResult {
  observationId: string;
  tabId: string;
  pageVersion: number;
  url: string;
  title: string;
  elements: SnapshotElement[];
  /** 还有没返回的元素（用 next_cursor 继续翻）。 */
  truncated: boolean;
  omitted: number;
  nextCursor: string | null;
  page: SnapshotPage;
  budget: { maxElements: number; maxChars: number; usedChars: number };
}

interface PageElement {
  ref?: unknown;
  role?: unknown;
  name?: unknown;
  tag?: unknown;
  disabled?: unknown;
  fp?: unknown;
}

function clampInt(raw: unknown, fallback: number, min: number, max: number): number {
  const value = typeof raw === "number" && Number.isFinite(raw) ? Math.trunc(raw) : fallback;
  return Math.min(max, Math.max(min, value));
}

function requireRef(raw: unknown): string {
  const ref = typeof raw === "string" ? raw.trim() : "";
  if (!REF_PATTERN.test(ref)) {
    throw new SnapshotError(
      "invalid_arguments",
      "ref 形如 el_001，只能是 browser_get_page_snapshot 返回的值",
    );
  }
  return ref;
}

/**
 * cursor 是「同一份观察的下一段」的句柄，形如 `obs_1a2b3c4d:120`。
 *
 * 不用裸 offset 是刻意的：offset 看起来像可以随便填，模型会自己试 `offset=0`
 * 或 `offset=9999`。带 observationId 之后，它只能从工具返回的 next_cursor 里拿。
 */
function parseCursor(raw: unknown): { observationId: string; offset: number } | null {
  if (raw === undefined || raw === null || raw === "") return null;
  const text = typeof raw === "string" ? raw.trim() : "";
  const matched = CURSOR_PATTERN.exec(text);
  if (!matched) {
    throw new SnapshotError(
      "invalid_arguments",
      "cursor 不合法：只能用上一次 browser_get_page_snapshot 返回的 next_cursor",
    );
  }
  return { observationId: matched[1], offset: Number(matched[2]) };
}

function pageFailure(code: string | undefined): SnapshotError {
  switch (code) {
    case "element_not_visible":
      return new SnapshotError(
        "element_not_visible",
        "元素当前不可见（可能被折叠或需要滚动），请重新取快照确认页面状态",
      );
    case "element_disabled":
      return new SnapshotError("element_disabled", "元素当前处于禁用状态，无法操作");
    case "element_changed":
      return new SnapshotError(
        "element_changed",
        "页面上这个位置的内容已经变了（可能发生了局部刷新），请重新取快照再操作",
      );
    case "element_missing":
    default:
      return new SnapshotError(
        "element_missing",
        "页面上找不到这个元素（可能已被移除或页面已更新），请重新取快照",
      );
  }
}

interface RawSnapshot {
  elements?: PageElement[];
  total?: number;
  budgetStopped?: boolean;
  usedChars?: number;
  url?: string;
  title?: string;
}

export class BrowserController {
  constructor(private readonly snapshots: SnapshotStore) {}

  /** 主导航发生时调用：旧快照整页作废。 */
  invalidateTab(tabId: string): void {
    this.snapshots.invalidateTab(tabId);
  }

  clear(): void {
    this.snapshots.clear();
  }

  async snapshot(
    tab: BrowserTab,
    options: { maxElements?: unknown; cursor?: unknown },
  ): Promise<SnapshotResult> {
    let maxElements = clampInt(options.maxElements, DEFAULT_MAX_ELEMENTS, 1, MAX_ELEMENTS_LIMIT);
    const cursor = parseCursor(options.cursor);

    let observationId = "";
    let offset = 0;
    if (cursor) {
      // 先校验归属与页面版本，再允许继续翻页：导航之后旧 cursor 必须直接作废。
      const existing = this.snapshots.validate(
        cursor.observationId,
        tab.tabId,
        tab.pageVersion,
      );
      if (cursor.offset !== existing.nextOffset) {
        throw new SnapshotError(
          "invalid_arguments",
          "cursor 已过期：只能使用最近一次快照返回的 next_cursor，或重新取一次快照",
        );
      }
      observationId = existing.observationId;
      offset = cursor.offset;
      // 没显式指定页大小时沿用第一页：否则第一页 5 条、第二页 120 条，
      // 模型会拿到一段它没预期的输出。
      if (options.maxElements === undefined) maxElements = existing.pageSize;
    }

    const raw = await tab.runScript<RawSnapshot>(
      collectElementsScript({
        maxElements,
        maxChars: MAX_CHARS,
        offset,
        hardLimit: REF_HARD_LIMIT,
        perElementCost: PER_ELEMENT_COST,
        preserve: cursor !== null,
      }),
    );

    const pageElements = Array.isArray(raw?.elements) ? raw.elements : [];
    const usable = pageElements.filter((item) => REF_PATTERN.test(String(item.ref ?? "")));
    const elements: SnapshotElement[] = usable.map((item) => ({
      ref: String(item.ref),
      role: String(item.role ?? "generic").slice(0, 40),
      name: String(item.name ?? "").slice(0, 80),
      tag: String(item.tag ?? "").slice(0, 20),
      disabled: Boolean(item.disabled),
    }));
    const total = Math.min(
      REF_HARD_LIMIT,
      Math.max(0, Number.isFinite(Number(raw?.total)) ? Math.trunc(Number(raw?.total)) : 0),
    );
    const inputs = usable.map((item) => ({
      ref: String(item.ref),
      fp: String(item.fp ?? ""),
    }));

    const observation = cursor
      ? this.snapshots.extend(observationId, {
          tabId: tab.tabId,
          pageVersion: tab.pageVersion,
          elements: inputs,
        })
      : this.snapshots.create({
          tabId: tab.tabId,
          pageVersion: tab.pageVersion,
          elements: inputs,
          total,
          pageSize: maxElements,
          url: String(raw?.url ?? tab.snapshot.url),
          title: String(raw?.title ?? tab.snapshot.title),
        });

    const nextOffset = offset + elements.length;
    const hasMore =
      nextOffset < total &&
      observation.pages < MAX_PAGES_PER_OBSERVATION &&
      elements.length > 0;
    const omitted = Math.max(0, total - nextOffset);

    return {
      observationId: observation.observationId,
      tabId: tab.tabId,
      pageVersion: observation.pageVersion,
      url: observation.url,
      title: observation.title,
      elements,
      truncated: omitted > 0,
      omitted,
      nextCursor: hasMore ? `${observation.observationId}:${nextOffset}` : null,
      page: { offset, returned: elements.length, total },
      budget: {
        maxElements,
        maxChars: MAX_CHARS,
        usedChars: Number.isFinite(Number(raw?.usedChars))
          ? Math.trunc(Number(raw?.usedChars))
          : 0,
      },
    };
  }

  async click(
    tab: BrowserTab,
    observationId: string,
    ref: string,
  ): Promise<{ clicked: boolean; ref: string; tag: string; name: string }> {
    const normalized = requireRef(ref);
    const observation = this.snapshots.validate(
      observationId,
      tab.tabId,
      tab.pageVersion,
      normalized,
    );
    const raw = await tab.runScript<{ ok?: boolean; code?: string; tag?: string; name?: string }>(
      clickRefScript(normalized, this.snapshots.fingerprint(observation, normalized)),
    );
    if (!raw?.ok) throw pageFailure(raw?.code);
    return { clicked: true, ref: normalized, tag: String(raw.tag ?? ""), name: String(raw.name ?? "") };
  }

  async fill(
    tab: BrowserTab,
    observationId: string,
    ref: string,
    value: string,
  ): Promise<{ filled: boolean; ref: string; length: number }> {
    const normalized = requireRef(ref);
    if (value.length > MAX_FILL_LENGTH) {
      throw new SnapshotError("invalid_arguments", `填写内容超过 ${MAX_FILL_LENGTH} 字符`);
    }
    const observation = this.snapshots.validate(
      observationId,
      tab.tabId,
      tab.pageVersion,
      normalized,
    );
    const raw = await tab.runScript<{ ok?: boolean; code?: string; length?: number }>(
      fillRefScript(
        normalized,
        value,
        this.snapshots.fingerprint(observation, normalized),
      ),
    );
    if (!raw?.ok) throw pageFailure(raw?.code);
    return { filled: true, ref: normalized, length: Number(raw.length ?? 0) };
  }

  async press(
    tab: BrowserTab,
    observationId: string,
    ref: string,
    key: string,
  ): Promise<{ pressed: boolean; key: string; submitted: boolean }> {
    const normalized = requireRef(ref);
    if (!ALLOWED_KEYS.has(key)) {
      throw new SnapshotError(
        "invalid_arguments",
        `不支持的按键：${key}（允许 Enter / Tab / Escape / Backspace / 方向键 / 空格）`,
      );
    }
    const observation = this.snapshots.validate(
      observationId,
      tab.tabId,
      tab.pageVersion,
      normalized,
    );
    const raw = await tab.runScript<{ ok?: boolean; code?: string; submitted?: boolean }>(
      pressRefScript(
        normalized,
        key,
        this.snapshots.fingerprint(observation, normalized),
      ),
    );
    if (!raw?.ok) throw pageFailure(raw?.code);
    return { pressed: true, key, submitted: Boolean(raw.submitted) };
  }

  async scroll(
    tab: BrowserTab,
    direction: string,
    amount: number,
  ): Promise<{ direction: string; before: number; after: number; height: number }> {
    if (!SCROLL_DIRECTIONS.has(direction)) {
      throw new SnapshotError("invalid_arguments", `不支持的滚动方向：${direction}`);
    }
    const step = clampInt(amount, 500, 1, MAX_SCROLL_AMOUNT);
    const raw = await tab.runScript<{ ok?: boolean; before?: number; after?: number; height?: number }>(
      scrollPageScript(direction, step),
    );
    if (!raw?.ok) throw pageFailure(undefined);
    return {
      direction,
      before: Number(raw.before ?? 0),
      after: Number(raw.after ?? 0),
      height: Number(raw.height ?? 0),
    };
  }
}
