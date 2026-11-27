/**
 * 注入页面执行的**固定脚本**。
 *
 * 为什么是「常量字符串」而不是「模型给的 JS」：Agent 永远拿不到执行任意脚本的能力。
 * 这里每个脚本都是写死的，唯一的外部输入是 `ref`（形如 `el_007`）与少量标量参数，
 * 且都会先经过 `SnapshotStore` 的白名单校验。
 *
 * 为什么用字符串而不是把函数 `toString()` 过去：主进程的 tsconfig 不带 DOM 类型，
 * 函数体里写 `document` 会编译失败；用字符串还能保证「打包工具不会对页面代码做任何
 * 转换」（一旦被改写，`HTMLInputElement.prototype` 之类的原生访问可能失效）。
 *
 * 约定：脚本末尾统一是 `(__ARGS__)`，由 `withArgs()` 注入 JSON。
 *
 * 阶段 5 的两处扩展：
 * 1. `COLLECT` 支持 `offset`（分页）与更贴近真实输出体积的字符预算；
 * 2. 快照记录每个元素的 `fp`（指纹：tag + role + 稳定名称），点击/填写/按键前
 *    在页面侧重新计算并比对。`did-navigate-in-page`（SPA 换内容）不递增
 *    pageVersion，指纹是唯一能发现「同一个 ref 已经变成另一个东西」的手段。
 */

function withArgs(source: string, args: unknown): string {
  return source.replace("__ARGS__", JSON.stringify(args));
}

/**
 * 元素指纹用的「稳定名称」。
 *
 * 刻意**不读 input.value**：填写之后 value 会变，若把它算进指纹，同一个输入框
 * 第二次填写就会被判成「元素变了」。这里只取不随用户输入变化的属性。
 */
const FINGERPRINT_HELPERS = String.raw`
  function roleOf(el) {
    var explicit = el.getAttribute('role');
    if (explicit) return explicit;
    var tag = el.tagName.toLowerCase();
    if (tag === 'a') return 'link';
    if (tag === 'button') return 'button';
    if (tag === 'select') return 'combobox';
    if (tag === 'textarea') return 'textbox';
    if (tag === 'input') {
      var type = (el.getAttribute('type') || 'text').toLowerCase();
      if (type === 'submit' || type === 'button') return 'button';
      if (type === 'checkbox') return 'checkbox';
      if (type === 'radio') return 'radio';
      return 'textbox';
    }
    if (el.isContentEditable) return 'textbox';
    return 'generic';
  }

  function stableName(el) {
    var aria = el.getAttribute('aria-label');
    if (aria && aria.trim()) return aria.trim().slice(0, 80);
    var tag = el.tagName.toLowerCase();
    if (tag === 'input' || tag === 'textarea' || tag === 'select') {
      var field = el.getAttribute('name');
      if (field && field.trim()) return '#' + tag + ':' + field.trim().slice(0, 60);
      var type = (el.getAttribute('type') || 'text').toLowerCase();
      return '#' + tag + ':' + type;
    }
    var placeholder = el.getAttribute('placeholder');
    if (placeholder && placeholder.trim()) return placeholder.trim().slice(0, 80);
    var text = (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim();
    if (text) return text.slice(0, 80);
    var title = el.getAttribute('title');
    if (title && title.trim()) return title.trim().slice(0, 80);
    var alt = el.getAttribute('alt');
    if (alt && alt.trim()) return alt.trim().slice(0, 80);
    var name = el.getAttribute('name');
    if (name && name.trim()) return name.trim().slice(0, 80);
    return '';
  }

  function fingerprint(el) {
    return el.tagName.toLowerCase() + '|' + roleOf(el) + '|' + stableName(el);
  }
`;

const COLLECT = String.raw`(function (args) {
  var MAX = args.maxElements;
  var MAX_CHARS = args.maxChars;
  var OFFSET = args.offset;
  var HARD_LIMIT = args.hardLimit;
  var PER_ELEMENT = args.perElementCost;
  var PRESERVE = args.preserve;
  var SELECTOR = 'a[href],button,input,textarea,select,[role="button"],[role="link"],[role="textbox"],[contenteditable=""],[contenteditable="true"]';

  function visible(el) {
    var style = window.getComputedStyle(el);
    if (!style) return false;
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    var rect = el.getBoundingClientRect();
    return rect.width > 1 && rect.height > 1;
  }

  function nameOf(el) {
    var aria = el.getAttribute('aria-label');
    if (aria && aria.trim()) return aria.trim().slice(0, 80);
    var tag = el.tagName.toLowerCase();
    if (tag === 'input') {
      var type = (el.getAttribute('type') || 'text').toLowerCase();
      // 密码框永远不取 value —— 快照会进模型上下文与日志。
      if (type === 'password') return '密码输入框';
      var value = el.value;
      if (value && type !== 'hidden') return String(value).slice(0, 60);
    }
    var placeholder = el.getAttribute('placeholder');
    if (placeholder && placeholder.trim()) return placeholder.trim().slice(0, 80);
    var text = (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim();
    if (text) return text.slice(0, 80);
    var title = el.getAttribute('title');
    if (title && title.trim()) return title.trim().slice(0, 80);
    var alt = el.getAttribute('alt');
    if (alt && alt.trim()) return alt.trim().slice(0, 80);
    var name = el.getAttribute('name');
    if (name && name.trim()) return name.trim().slice(0, 80);
    return '';
  }

  ${FINGERPRINT_HELPERS}

  // 分页时不能清：前几页的 ref 属性一旦被抹掉，模型用第一页的 ref 点击就会
  // 变成 element_missing。新建快照才整页重置命名空间。
  if (!PRESERVE) {
    var old = document.querySelectorAll('[data-ethos-ref]');
    for (var i = 0; i < old.length; i++) old[i].removeAttribute('data-ethos-ref');
  }

  var out = [];
  var chars = 0;
  var visibleIndex = 0;
  var stopped = false;
  var budgetStopped = false;
  var nodes = document.querySelectorAll(SELECTOR);
  for (var j = 0; j < nodes.length; j++) {
    var el = nodes[j];
    if (!visible(el)) continue;
    visibleIndex++;
    if (visibleIndex > HARD_LIMIT) { visibleIndex = HARD_LIMIT + 1; break; }
    // 已经不在本页范围或本页预算已满：继续数 total，不再收集。
    if (stopped || visibleIndex <= OFFSET) continue;

    var name = nameOf(el);
    var cost = PER_ELEMENT + name.length;
    if (out.length >= MAX) { stopped = true; continue; }
    if (chars + cost > MAX_CHARS) { stopped = true; budgetStopped = true; continue; }

    chars += cost;
    // ref 编号用「页面里的全局可见序号」，不是本页内的相对序号：
    // 否则第二页又会从 el_001 开始，把第一页的 ref 语义覆盖掉。
    var ref = 'el_' + ('000' + visibleIndex).slice(-3);
    el.setAttribute('data-ethos-ref', ref);
    out.push({
      ref: ref,
      role: roleOf(el),
      name: name,
      tag: el.tagName.toLowerCase(),
      disabled: !!el.disabled,
      fp: fingerprint(el)
    });
  }
  return {
    elements: out,
    total: Math.min(visibleIndex > HARD_LIMIT ? HARD_LIMIT : visibleIndex, HARD_LIMIT),
    budgetStopped: budgetStopped,
    usedChars: chars,
    scrollY: window.scrollY,
    url: location.href,
    title: document.title
  };
})(__ARGS__)`;

/** 三个写操作共用的前置检查：存在 → 指纹一致 → 可见 → 未禁用。 */
const REF_CHECKS = String.raw`
  function guard(el, args) {
    if (!el) return { ok: false, code: 'element_missing' };
    if (args.expected && fingerprint(el) !== args.expected) {
      return { ok: false, code: 'element_changed' };
    }
    var style = window.getComputedStyle(el);
    var rect = el.getBoundingClientRect();
    if (!style || style.display === 'none' || style.visibility === 'hidden' || rect.width < 1 || rect.height < 1) {
      return { ok: false, code: 'element_not_visible' };
    }
    return null;
  }
`;

const CLICK = String.raw`(function (args) {
  ${FINGERPRINT_HELPERS}
  ${REF_CHECKS}
  var el = document.querySelector('[data-ethos-ref="' + args.ref + '"]');
  var failed = guard(el, args);
  if (failed) return failed;
  if (el.disabled) return { ok: false, code: 'element_disabled' };
  if (el.scrollIntoView) el.scrollIntoView({ block: 'center', inline: 'center' });
  if (el.focus) el.focus();
  el.click();
  return { ok: true, tag: el.tagName.toLowerCase(), name: (el.getAttribute('aria-label') || el.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 80) };
})(__ARGS__)`;

const FILL = String.raw`(function (args) {
  ${FINGERPRINT_HELPERS}
  ${REF_CHECKS}
  var el = document.querySelector('[data-ethos-ref="' + args.ref + '"]');
  var failed = guard(el, args);
  if (failed) return failed;
  if (el.disabled || el.readOnly) return { ok: false, code: 'element_disabled' };
  if (el.scrollIntoView) el.scrollIntoView({ block: 'center' });
  if (el.focus) el.focus();
  if (el.isContentEditable) {
    el.textContent = args.value;
  } else {
    var proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    var setter = Object.getOwnPropertyDescriptor(proto, 'value');
    // React 受控组件把 value 的 setter 劫持了：直接 el.value = x 不会触发 onChange，
    // 必须走原型上的原生 setter，再补发 input/change 事件。
    if (setter && setter.set) setter.set.call(el, args.value);
    else el.value = args.value;
  }
  el.dispatchEvent(new Event('input', { bubbles: true }));
  el.dispatchEvent(new Event('change', { bubbles: true }));
  var actual = el.isContentEditable ? el.textContent : el.value;
  return { ok: true, length: String(actual || '').length };
})(__ARGS__)`;

const PRESS = String.raw`(function (args) {
  ${FINGERPRINT_HELPERS}
  ${REF_CHECKS}
  var el = document.querySelector('[data-ethos-ref="' + args.ref + '"]');
  if (el) {
    // 只有真的找到快照里的元素才做指纹复核；焦点回退到 activeElement 时
    // 已经没有「快照里那个元素」可比对，硬比只会误报。
    var failed = guard(el, args);
    if (failed) return failed;
  } else {
    el = document.activeElement;
  }
  if (!el) return { ok: false, code: 'element_missing' };
  if (el.focus) el.focus();
  var init = { key: args.key, code: args.key, bubbles: true, cancelable: true };
  el.dispatchEvent(new KeyboardEvent('keydown', init));
  el.dispatchEvent(new KeyboardEvent('keypress', init));
  el.dispatchEvent(new KeyboardEvent('keyup', init));
  var submitted = false;
  // 合成事件不会触发浏览器的默认行为（回车提交表单就是默认行为），
  // 所以这里显式补一次。代价是页面若自己也在 keydown 里提交，可能重复提交一次。
  if (args.key === 'Enter' && el.form && el.form.requestSubmit) {
    el.form.requestSubmit();
    submitted = true;
  }
  return { ok: true, submitted: submitted };
})(__ARGS__)`;

const SCROLL = String.raw`(function (args) {
  var before = window.scrollY;
  if (args.direction === 'top') window.scrollTo(0, 0);
  else if (args.direction === 'bottom') window.scrollTo(0, document.body.scrollHeight);
  else window.scrollBy(0, args.direction === 'up' ? -args.amount : args.amount);
  return {
    ok: true,
    before: before,
    after: window.scrollY,
    height: document.body ? document.body.scrollHeight : 0
  };
})(__ARGS__)`;

export interface CollectOptions {
  maxElements: number;
  maxChars: number;
  offset: number;
  hardLimit: number;
  perElementCost: number;
  /** 分页续取时为 true：保留前面页已经写上的 data-ethos-ref。 */
  preserve: boolean;
}

export const collectElementsScript = (options: CollectOptions): string =>
  withArgs(COLLECT, options);

export const clickRefScript = (ref: string, expected?: string): string =>
  withArgs(CLICK, { ref, expected });

export const fillRefScript = (ref: string, value: string, expected?: string): string =>
  withArgs(FILL, { ref, value, expected });

export const pressRefScript = (ref: string, key: string, expected?: string): string =>
  withArgs(PRESS, { ref, key, expected });

export const scrollPageScript = (direction: string, amount: number): string =>
  withArgs(SCROLL, { direction, amount });
