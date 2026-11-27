# 内置浏览器与 Browser Agent：阶段 0 技术设计

**状态**：设计已出，待人工审查。**代码尚未开始实施。**
**分支**：`feat/desktop-browser-view`，HEAD `10a64cf`。
**本阶段交付物**：本文档。按方案 §6.3，阶段 0 结束后暂停，等人工验收指令。

本文只写「现状是什么、方案哪里需要改、接下来怎么落地」。所有未在真实环境验证过的
地方都显式标注为**假设**，并给出关闭方式——不把假设写成结论。

---

## 1. 现状核验

```
$ git branch --show-current   feat/desktop-browser-view
$ git rev-parse --short HEAD  10a64cf
$ git status --short
 M backend-py/src/edith_backend/internal/agentrun/plan_tools.py
 M backend-py/src/edith_backend/internal/cronjob/tools.py
 M backend-py/src/edith_backend/internal/systemtools/tools.py
 M backend-py/src/edith_backend/internal/tools.py
?? backend-py/inspect_db.py  inspect_llm.py  inspect_model.py
?? backend-py/inspect_success_model.py  inspect_trace_steps.py
?? backend-py/tests/test_tool_registry_forwarding.py
```

这 4 个 `M` 与 6 个 `??` 是**浏览器功能之前**的既有改动（`tool_call_id` 转发修复），
不属于本项目。约束：浏览器的每个提交都必须显式列文件，不得 `git add -A`，不得对
这些文件做格式化或顺带修改。

依赖实测：

| 项 | 实测值 | 影响 |
| --- | --- | --- |
| Electron | `33.4.11`（`package.json` 声明 `^33.2.0`） | `WebContentsView` / `BaseWindow.contentView` 可用（Electron 30+） |
| Electron 构建 | `tsc -p tsconfig.json`，`module: commonjs`，`strict: true` | 新模块用 CommonJS 相对导入，必须过 `npm run typecheck` |
| Next | `16.2.12`，`output: "standalone"` | 前端产物由 `loadURL(http://127.0.0.1:<port>)` 加载，**不是** `loadFile` |
| Python | 3.13 + FastAPI，venv 依赖完整 | 后端可直接扩展 |

---

## 2. 现状调用链（只写与本项目相关的部分）

### 2.1 启动与窗口

`desktop/src/main.ts`：

```
:241  requestSingleInstanceLock()  → 拿不到就直接 quit
:264  app.whenReady().then(bootstrap)
:228  bootstrap → startServices() → createWindow(webUrl) → createTray() → ready = true
:132  startServices:
        :139 registry.reapStale()                    ← Electron 子进程台账回收
        :144 backendPort = findFreePort()  /  webPort = findFreePort()
        :161 startProcess(python, ["-m","uvicorn","edith_backend.main:app",…],
                          { env: ETHOS_DESKTOP, ETHOS_DATA_DIR, EDITH_RUNTIME_TOKEN, PYTHONPATH })
        :184 waitForHttp(backend /health)
        :194 startProcess(execPath, [standalone/server.js], { env: ELECTRON_RUN_AS_NODE, PORT, EDITH_RUNTIME_URL, EDITH_RUNTIME_TOKEN })
        :215 waitForHttp(web)
:36   createWindow:
        :45 webPreferences: contextIsolation true / nodeIntegration false / preload preload.js
        :58 close → 有托盘则 hide()，没有托盘则放行退出
        :65 setWindowOpenHandler → 一律 shell.openExternal(url) + { action: "deny" }
        :70 loadURL(http://127.0.0.1:<webPort>)
:219  shutdown(): web.stop() → backend.stop()
:257  before-quit → shutdown().finally(app.exit)
```

**结论 1**：目前只有 **一个** `BrowserWindow`，页面是远程 URL。要挂原生浏览器视图，
只能在 `win.contentView` 上加子视图——这是 Electron 33 的推荐路径，`BrowserView`
已废弃。

**结论 2**：现有 `setWindowOpenHandler` 会**拦掉所有新窗口**。浏览器标签页必须在这里
改成分支：目标属于浏览器分区则新建 Tab，其余保持 `openExternal`。这条改动属于
阶段 2，不在阶段 1 偷跑。

### 2.2 Renderer ↔ Main：**当前没有 IPC**

`desktop/src/preload.ts` 全文 4 行，只有 `export {}`。全仓搜索 `ipcMain` / `ipcRenderer`
/ `contextBridge` / `window.electron`，除 `main.ts` 的 `webPreferences` 外 0 命中。

**结论 3**：方案里 `BrowserIPC` 不是"接线"，是**从零建一套 preload 桥 + ipcMain
处理器**。这是原方案未列出的工作量，必须在阶段 2 之前完成，并且要处理
"渲染进程页面本身来自 `http://127.0.0.1:<port>`"这一事实（preload 依然生效，
但桥接面必须白名单化，不能暴露通用 `invoke`）。

### 2.3 Renderer ↔ Python：同源 BFF

`web-v1/app/api/**/route.ts`（39 个）把 `/api/...` 转发到 `EDITH_RUNTIME_URL`，
令牌来自 `web-v1/lib/server/runtime-token.ts`，后端在 `main.py:296-347` 的 HTTP
中间件里用 `hmac.compare_digest` 校验 `X-ETHOS-RUNTIME-TOKEN`。

**结论 4**：浏览器 UI **不需要**经过 Python。UI 与浏览器视图都在同一个 Electron
进程边界内，走 IPC 即可；Python 只负责 Agent 工具那一侧，走独立 RPC。方案里同时列
`/internal/browser/tabs/list` 等路由，会造成两套入口，本文明确取消（见 §4.2）。

### 2.4 聊天页布局与右侧面板槽位

`web-v1/components/chat/chat-page.tsx`（1173 行）：

```
:1053 <main class="flex h-screen overflow-hidden">
         <AppSidebar/>                                  ← 左
         <section class="flex min-w-0 flex-1 flex-col">  ← 中：header / TimelineView / ChatComposer
:1162    <SandboxPanel ... open={sandboxOpen}/>          ← 右：flex 兄弟节点，可拖拽宽度
:1169    <SettingsDialog .../>                            ← fixed 覆盖层
```

`web-v1/components/chat/sandbox-panel.tsx` 是唯一可复用的分栏实现：`:42` 宽度 state
320、`:140-153` 指针拖拽（280~640 钳制）、`:186-191` `aside` + 左边缘拖拽条。

**结论 5**：浏览器面板放在 `:1162` 这一层（与 `SandboxPanel` 互斥或并列需产品定），
宽度/拖拽直接复用 `sandbox-panel.tsx` 的模式；`web-v1` 没有全局状态库，状态只能进
`ChatPage` 或新开 Context。

### 2.5 Agent 工具链（后端）

```
execution.py:1644  async with tracer.step(TOOL_CALL, name, input=..., metadata={"source": …})
execution.py:1651  evaluate_call(permission_level, name, arguments, is_mcp=…, registered=…)
execution.py:1670  _apply_approval(...)             ← 审批档在此挂起/恢复
execution.py:1693  readonly = _is_readonly_tool(...) → run 级结果缓存 read_cache
execution.py:1721  tool_executor.execute(name, arguments, user_id, session_id,
                                         workspace_id, context=…, tool_call_id=…)
tools.py:145-189   ToolRegistry.execute 按关键字转发给各 tool set
execution.py:1749  tool_step.output = result
execution.py:1750  materialize_tool_result(result)   ← 12000 字符预算，超了裁成「头+骨架」
```

`main.py:209-211` 是唯一的注册点，当前 4 个 tool set：
`SystemToolSet` / `SandboxToolSet` / `CronToolSet` / `PlanToolSet`。

`execution.py:1183-1187` 里可得的身份：`user_id`、`session_id`、`workspace_id`、
`request_id`（仅用于日志与 tracer）。**没有 `run_id`**——全目录 grep `run_id` 0 命中。

**结论 6**：方案里 `run_id` 的现实对应物是 `request_id`（tracer 的主键、SSE 的
`X-Request-ID`、`agent-runs/{requestId}` 路由都用它）。控制权绑定必须照这个来。

---

## 3. 对原方案的 7 处修正

| # | 原方案 | 现实 | 修正 |
| --- | --- | --- | --- |
| A | 控制权绑定 `run_id` | 不存在 `run_id` | 用 `request_id`；且它**当前不传给 tool set**，要扩展转发契约（见 B） |
| B | 浏览器工具"直接接入现有工具链" | `ToolRegistry.execute` 的透传面是 `workspace_id/context/tool_call_id` 四个 | 需要把 `request_id` 加进透传面：**一次改 5 个 tool set 的签名 + 注册表转发 + 守卫测试**。这正是 2026-11-24 那次"所有工具一起挂"的同类改动，必须连带 `tests/test_tool_registry_forwarding.py` |
| C | `browser_get_page_snapshot` / `browser_get_url` 视为只读 | `is_readonly` = 能力集合 ⊆ `{READ}`（`catalog.py:150-156`）；只读工具会被 run 级缓存（`execution.py:1696-1708`，key = 工具名+参数） | 快照/URL **绝不能**声明为纯 READ：`{}` 参数会让第二次快照直接命中缓存，返回**已经过期**的 `observation_id`。声明为 `{READ, METADATA}`（不缓存、且只读/审批档仍放行） |
| D | 网页视图用 `z-index` 处理覆盖关系 | `WebContentsView` 是**原生子视图**，永远盖在 DOM 之上 | React 只渲染占位 `div`，用 `getBoundingClientRect()` 上报 bounds；任何模态（`SettingsDialog` 等）打开时必须 `view.setVisible(false)`，否则弹窗会被网页盖住 |
| E | RPC 同时提供 `/internal/browser/tabs/list` 等 HTTP 路由 | 会让 UI 有两条通路（IPC 与 BFF） | UI 只走 IPC；Python 只走 Electron RPC（单端点 + 方法白名单）。Python 侧**不新增** `/internal/browser/*` 路由 |
| F | 新窗口/下载"处理" | `main.ts:65-68` 现在 deny 一切 | 阶段 2 明确改造点：同分区的 `window.open` → 新建 Tab；`will-download` 默认拒绝并提示（阶段 7 再谈下载目录） |
| G | 假设浏览器能直接出网 | 本机装了 ASRT（`isolation.health()` = asrt 0.0.83 available），它包含**机器级 WFP 出站过滤器** | 见假设 A1：必须先在装了 ASRT 的机器上验证 Electron 的网页请求不被拦。若被拦，方案要加"ASRT 白名单/放行 Electron 进程"一节，这会改变阶段 1 的验收 |

---

## 4. 目标架构

### 4.1 进程与职责

```
┌──────────────────────── Electron Main ────────────────────────┐
│ BrowserManager ── BrowserTab(WebContentsView) × N             │
│      │   └─ BrowserController（导航/点击/输入/滚动）           │
│      │   └─ BrowserSnapshot（观测 + ref 映射 + 过期判定）       │
│      ├─ BrowserSession（partition、Cookie、per-tab 状态）       │
│      ├─ BrowserPolicy（协议/权限/下载/new-window）              │
│      ├─ BrowserControl（USER / AGENT 控制权，唯一裁决者）        │
│      ├─ BrowserIPC（ipcMain.handle → 白名单方法 → Renderer）    │
│      └─ BrowserRpcServer（127.0.0.1:<rand> + 独立 token）       │
└───────────────────────────────────────────────────────────────┘
        ▲  ipcRenderer.invoke（preload 桥，白名单）   │ HTTP RPC（仅 Python）
        │                                            ▼
┌─ Renderer（web-v1, /chat）─┐            ┌─ Python: internal/browser ─┐
│ BrowserPanel / Tabs /      │            │ BrowserClient（httpx 封装） │
│ Toolbar / AddressBar /     │            │ BrowserService（业务/校验） │
│ ControlStatus              │            │ BrowserToolSet（7 个工具）  │
└────────────────────────────┘            └──────────┬─────────────────┘
                                                     ▼
                                          ToolRegistry → execution.py → tracer
```

### 4.2 通信边界（两条，互不复用）

| 通道 | 端点 | 鉴权 | 调用方 | 允许能力 |
| --- | --- | --- | --- | --- |
| IPC | `ipcMain.handle("browser:*")` | Electron 进程边界 + 白名单方法 | Renderer | 仅 UI 操作：tab 管理、导航、bounds、控制权 |
| RPC | `POST http://127.0.0.1:<port>/rpc` | `Authorization: Bearer <browserToken>` | Python | 仅 Agent 操作：open/snapshot/click/fill/press/scroll/get_url |
| Runtime HTTP | `/internal/**` | `EDITH_RUNTIME_TOKEN`（既有） | BFF | 不新增浏览器路由 |

Token 与端口由 Electron 在 `startServices()` 里生成（沿用 `findFreePort()`），通过
backend 的 spawn env 注入（与 `EDITH_RUNTIME_TOKEN` 同一模式，**但值不同**）：
`ETHOS_BROWSER_RPC_URL`、`ETHOS_BROWSER_RPC_TOKEN`。不落盘、不写日志、不进 trace。

RPC 协议（单端点，方法白名单）：

```jsonc
// 请求
{ "method": "tabs.navigate", "params": { … }, "requestId": "<uuid 可选，用于日志>" }
// 成功
{ "ok": true, "result": { … } }
// 失败
{ "ok": false, "error": { "code": "ref_expired", "message": "…", "retryable": true } }
```

方法集合：`health` `tabs.list` `tabs.active` `tabs.navigate` `page.snapshot`
`page.click` `page.fill` `page.press` `page.scroll` `page.url` `control.claim`
`control.release`。

限额：body ≤ 64 KiB；单次调用超时 10s（导航 20s）；同一 token 每秒 ≤ 20 次；
未知方法 → `method_not_allowed`；缺/错 token → 401（不区分"没有"和"不对"）。

错误码 → 工具结果 `kind` 的映射：

| code | kind | retryable | 说明 |
| --- | --- | --- | --- |
| `rpc_unavailable` / `timeout` | `tool_failure` | false | 平台侧故障，计熔断 |
| `ref_expired` / `stale_observation` / `element_missing` | `command_error` | true | 模型重新取快照即可自救 |
| `control_revoked` / `not_owner` | `permission_denied` | false | 用户已接管，模型不得重试 |
| `invalid_arguments` / `bad_ref_format` | `invalid_arguments` | true | 参数问题 |
| `unsafe_url` / `policy_denied` | `command_error` | true | 换地址重试 |
| `tab_closed` | `command_error` | true | 重新开 tab |

### 4.3 数据结构

```ts
interface BrowserTabInfo { tabId: string; url: string; title: string; loading: boolean; canGoBack: boolean; canGoForward: boolean; crashed: boolean }

interface AgentControl {
  owner: "user" | "agent";
  sessionId: string;      // 占用控制权的会话
  requestId: string;      // run 标识（= Python 侧 request_id）
  tabId: string;
  token: string;          // 每次 claim 生成，release/takeover 后立即失效
  claimedAt: number;
}

interface Observation {
  observationId: string;  // obs_<8hex>
  tabId: string;
  requestId: string;      // 谁取的就归谁
  pageVersion: number;    // 每次主文档导航 +1
  createdAt: number;
  elements: { ref: string; role: string; name: string; tag: string; disabled?: boolean }[];
  truncated: boolean; omitted: number;
}
```

快照的 ref 不放在页面全局变量里（页面脚本可以改），而是：
`data-ethos-ref="el_007"` 属性 + 主进程持有的 `observationId → {ref, tabId, pageVersion}`
映射。执行前核对 `tabId` / `pageVersion` / `requestId` 三件事，任何一项不符即
`stale_observation`。

### 4.4 生命周期

| 时机 | 动作 |
| --- | --- |
| `app.whenReady` | 建 BrowserManager（此时**不**建任何 Tab，也不建 View） |
| 用户打开浏览器面板 | 建第一个 Tab（`WebContentsView` + partition）、绑到 `win.contentView`、注册 IPC/RPC 路由 |
| 面板收起 | `view.setVisible(false)`；Tab 与 Cookie 保留（不销毁，恢复要秒开） |
| 窗口 `hide()`（托盘） | 同上，`setVisible(false)`；**不销毁**，否则回来要重新加载 |
| 窗口 `show()` | 按保存的 bounds 恢复可见 |
| 关闭 Tab | `win.contentView.removeChildView(view)` → `webContents.close()` → 删快照/控制权 |
| `before-quit` | 顺序：停 RPC server → 销毁所有 Tab → 停 web → 停 backend（现有 shutdown 顺序不变，浏览器插在最前） |
| 渲染进程崩溃 | `render-process-gone` → 标记 tab.crashed、UI 显示"重新加载"，不退出应用 |
| RPC server 关闭 | 先置 `closing=true` 再 close，避免 Python 侧收到 ECONNREFUSED 以外的怪错 |

---

## 5. 安全边界与威胁模型

资产：用户 Cookie / 登录态、Agent 可控的网页内容、本机文件系统与命令能力。

| 威胁 | 处置 |
| --- | --- |
| 网页通过 `window.opener`/IPC 拿到主进程能力 | 网页 view 用**独立 partition** + `contextIsolation: true` + `nodeIntegration: false` + `sandbox: true` + **不注入任何 preload** |
| 模型要求执行任意 JS | 不提供该工具；唯一的 JS 是主进程里的**常量快照脚本**，参数只有 `offset/limit` |
| 模型提交任意 CSS selector | 工具只接受 `observation_id + ref`，ref 形如 `el_\d{3}` 且必须命中当前快照 |
| 模型借浏览器读本地文件 | 导航协议白名单只放 `http/https`（`file:`/`javascript:`/`data:` 一律拒）；不提供读本地路径的 RPC 方法 |
| RPC 被同机其他进程调用 | 随机端口 + 独立 token（`crypto.randomBytes(32)`）+ **方法白名单**；token 只进 backend 子进程 env |
| 旧 run 在用户接管后继续操作 | 控制权在 Main 内存里裁决，每次操作前比对 `(sessionId, requestId, tabId, token)`；接管即删除 token，旧请求一律 `control_revoked` |
| 快照泄漏敏感内容 | 快照只取 role/accessible name/文本摘要；密码框 `type=password` 不取 value、输入框只报 placeholder；不截屏、不落 Cookie |
| 浏览器成为绕过权限的通道 | 浏览器工具在 `TOOL_CAPABILITIES` 声明 `EXTERNAL`，审批档需人工确认；不提供 shell/git 能力 |

明确不做（写进文档，避免"以为已经防了"）：不做下载目录、不做文件上传、不绕过
ASRT 的沙箱策略、不代用户登录。

---

## 6. 文件清单

新增（Electron）：

```
desktop/src/main/browser/{types,browser-manager,browser-tab,browser-controller,
                          browser-snapshot,browser-session,browser-policy,
                          browser-ipc,browser-rpc-server}.ts
desktop/src/preload.ts                     ← 从空 stub 改成 contextBridge 白名单
```

修改（Electron）：`desktop/src/main.ts`（仅窗口内容区挂载 + 退出顺序 + new-window 分支）。

新增（前端）：

```
web-v1/components/browser/{BrowserPanel,BrowserTabs,BrowserToolbar,BrowserAddressBar,BrowserControlStatus}.tsx
web-v1/lib/browser/bridge.ts               ← window.ethosBrowser 的类型与调用封装
```

修改（前端）：`web-v1/components/chat/chat-page.tsx`（header 增加入口 + 面板槽位）。

新增（后端）：

```
backend-py/src/edith_backend/internal/browser/{__init__,types,client,service,snapshot,tools}.py
```

修改（后端）：`main.py`（注册 `BrowserToolSet`）、`permissions/catalog.py`（7 条能力声明）、
`agentrun/execution.py`（把 `request_id` 传进转发层）。

测试：`backend-py/tests/test_browser_tools.py`、`test_browser_rpc_client.py`、
`test_browser_snapshot_refs.py`；前端与 Electron 侧用 `desktop/scripts/` 下的
真实启动脚本 + 手工验收清单（方案要求不得全用 Mock）。

---

## 7. 分阶段计划（修正后）

沿用原方案的 9 个阶段与门禁，只调整两处工作量与顺序：

1. **阶段 1 前 30 分钟先做 3 个 spike**（A1~A3，见 §8）。任何一个不成立就停下来改设计，
   不进入编码。
2. **`request_id` 透传面**（修正 B）提前到阶段 3 一起做——它同时是 RPC 与控制权的
   身份来源，晚做会让阶段 4/6 返工。

其余阶段划分、验收标准、门禁保持方案原文。每个阶段结束按 §6.2 的 A~G 格式出报告，
状态只能是 PASS / BLOCKED / PARTIAL。

---

## 8. 待验证假设（阶段 0 无法关闭）

| 编号 | 假设 | 关闭方式 | 状态 |
| --- | --- | --- | --- |
| A1 | 装了 ASRT（机器级 WFP 出站过滤器）后，Electron 自己的网页请求仍能正常出网 | 真实 Electron 里加载 `https://example.com` | **已关闭 · 成立**（见 §10） |
| A2 | `win.contentView.addChildView()` + `view.setBounds()` 在 33.4.11 上按窗口坐标生效，页面在独立渲染进程里真的跑起来 | 同上，读回 bounds + DOM 探针 | **机械部分已关闭 · 成立**；合成/观感仍需人工看 |
| A3 | 原生视图之上的模态处理（`setVisible(false)`）足够，不需要拆窗口 | 打开 `SettingsDialog` 时隐藏/显示视图，确认无闪烁、无焦点抢占 | **未关闭**（需可见窗口观测） |

A3 是实现级假设，只能靠可见窗口逐项目视，这也是阶段 1 判定为 PARTIAL 的原因。

---

## 10. 阶段 1 验证记录（2026-11-26）

两个脚本留在 `desktop/scripts/spikes/`，可重复运行：

```bash
cd desktop
npx electron scripts/spikes/webcontentsview.cjs     # A1/A2 机制
npm run build && npx electron scripts/spikes/manager-smoke.cjs   # BrowserManager 集成
```

**spike 1（`webcontentsview.cjs`）结论**：

- `addChildView` + `setBounds({x:120,y:60,width:600,height:400})` → `getBounds()` 逐字段一致；
  二次 `setBounds({width:320,height:240})` 同样生效（说明是活的操作，不是一次性初始化）。
- `https://example.com` `did-finish-load`，DOM 探针 `title="Example Domain"`、正文 917 字符。
  **ASRT 的 WFP 没有拦掉 Electron 的网页请求** —— A1 成立。
- `getLastWebPreferences()` 回读 `sandbox:true / contextIsolation:true / nodeIntegration:false`
  三项都生效。

**spike 2（`manager-smoke.cjs`，10 项断言全过）**：

| 断言 | 结果 |
| --- | --- |
| 建 tab / manager 可见 | ✓ |
| 加载 example.com（标题、URL 落到状态） | ✓ |
| 视图已挂到 contentView | ✓ |
| 初始 bounds 与 `getContentSize()` 推导值一致 | ✓ |
| 1000×700 → 1300×800 后 bounds 重算为 `{x:780,y:56,w:520,h:744}` | ✓ |
| `hide()` 后 manager 不可见 / 视图不可见 | ✓ |
| `show()` 恢复后 URL、标题仍在（未重建视图） | ✓ |
| `dispose()` → `win.destroy()` 无异常 | ✓ |

**一条实测发现（已写进代码注释）**：`BrowserWindow({width:1000,height:700})` 是**窗口**尺寸，
内容区实测只有 `985×637`（边框 + DPI 缩放）。布局必须用 `getContentSize()` 推导，
用窗口尺寸会算偏 6px，在右侧贴边时表现为一条空隙。第一次跑时正是这条断言先红，
说明这个断言是有效的（反向验证成立）。

**仍需人工目视**（阶段 1 门禁的一部分）：真实应用里 `Ctrl+Shift+B` 的观感、模态遮挡、
托盘隐藏→恢复的视觉连续性、退出后任务管理器无残留 `electron.exe`。

---

## 11. 阶段 2 实施记录（2026-11-26）

阶段 1 的人工验收通过后进入阶段 2：多标签页 + 完整工具栏 + preload 桥。

**架构上落地的两条硬约束**（阶段 0 §3 的修正 D、E）：

1. 视口矩形由渲染进程决定。`BrowserPanel` 用占位 `div` 的 `getBoundingClientRect()`
   经 `view.set` 上报，主进程只负责套到当前标签页并把其余标签页压成 0 尺寸。
   主进程不再计算任何布局比例——否则侧栏折叠、面板拖拽、弹窗都会错位。
2. UI 只走 IPC，Python 只走 RPC。本轮把 IPC 的暴露面收成一个
   `command(method, params)` 入口 + 方法白名单，不在 preload 暴露通用 `invoke`。

**IPC 契约**（`desktop/src/preload.ts` ↔ `browser-ipc.ts`）：

| 方法 | 参数 | 说明 |
| --- | --- | --- |
| `state` | — | 拉整份状态 |
| `tabs.open` | `{url?}` | 新建前台标签页 |
| `tabs.close` / `tabs.activate` | `{tabId}` | 关闭 / 切换 |
| `nav.navigate` | `{url}` | 地址栏跳转（协议白名单在 `normalizeUrl`） |
| `nav.back` / `nav.forward` / `nav.reload` / `nav.stop` | — | 导航控制 |
| `view.set` | `{bounds: {x,y,width,height} \| null}` | 视口；`null` = 面板收起（隐藏但不销毁） |

推送方向：`browser:state`（整份状态，30ms 合并）、`browser:toggle-request`（Ctrl+Shift+B，
仅通知，显隐由 React 决定）。

**验证**：`scripts/spikes/manager-smoke.cjs` 20 项断言全过（多标签、后台打开不抢焦点、
非当前页压 0、切换换位、导航/后退/前进可用性、收起保留标签页、关闭不误伤、dispose 干净）。
`web-v1` 通过 `tsc --noEmit` 与 `next build`。

**已知未覆盖**：除 `SettingsDialog` 外的模态（会话重命名、模型选择弹窗）尚未接入
`suspended`，打开时仍会被原生视图遮挡——阶段 6/7 统一处理；下载仍走 Electron 默认。

---

## 12. 阶段 3 实施记录（2026-11-26）

按阶段 0 的**修正 E** 落地：UI 只走 IPC，Python 只走 RPC，**没有**新增
`/internal/browser/*` 这类 Python 侧路由（方案原文里那组接口会造成两条通路）。

**协议**：`POST http://127.0.0.1:<系统分配>/rpc`，`Authorization: Bearer <token>`，
body `{"method","params"}`，响应 `{"ok":true,"result":...}` /
`{"ok":false,"error":{"code","message"}}`。方法白名单（阶段 3）：
`health` `tabs.list` `tabs.active` `page.navigate` `page.url`。

**启动顺序**（这是接线里最容易做错的一处）：RPC 服务必须先于 backend 启动，
端口与令牌才能写进后端子进程的 env；此时窗口还没建，管理器不存在，服务先挂空挡
（`browser_not_ready` 503），`attachBrowser()` 之后再 `attach()`。令牌与
`EDITH_RUNTIME_TOKEN` **刻意不同**：前者只能操作浏览器。

**限额**：body 64 KiB（超过仍读完再返 413，而不是直接断连接——否则客户端只能看到
`ECONNRESET`，读不到错误码）、硬上限 1 MiB（超过直接断）、20 次/秒、单方法处理 15 秒超时。

**Python 侧**：`internal/browser/{types,client,service}.py`。
`BrowserRpcError` 带 `code` 与 `retryable`（按错误码分类，而不是按 HTTP 状态猜）；
未注入地址/令牌时 `enabled=False`，调用抛 `browser_disabled`——不降级成假浏览器。
客户端**不做重试**：重试策略归工具层，因为「导航重试」会重复提交表单。

**验证**：

- `tests/test_browser_rpc_client.py` 10 例，起真实 `http.server`（不 mock 传输层），
  覆盖正常路径、401、未知方法、超时、非 JSON、连接不可达、服务未启用。
- `desktop/scripts/spikes/rpc-smoke.cjs` 13 项断言全过：真实 Electron 起 RPC + 真网页，
  `child_process` 拉起 `backend-py/scripts/browser_rpc_probe.py` 用真实客户端跨进程调用；
  断言「Python 导航后 Electron 侧 URL 也变了」（同一个视图）、401/404/413、
  以及 RPC 关闭后 Python 立刻拿到 `unavailable` 而不是挂住。
- 后端全量：`826 passed, 1 skipped`。

---

## 13. 阶段 4 实施记录（2026-11-26）

**工具**（7 个，全部走既有 Tool Registry）：`browser_open_url`、`browser_get_page_snapshot`、
`browser_click`、`browser_fill`、`browser_press`、`browser_scroll`、`browser_get_url`。

**能力声明**（`permissions/catalog.py`，被一条测试锁住）：

| 工具 | 能力 | 理由 |
| --- | --- | --- |
| `browser_open_url` / `click` / `fill` / `press` | `{EXTERNAL}` | 改变外部站点状态（提交表单、下单）；审批档必须拦 |
| `browser_get_page_snapshot` / `browser_get_url` / `browser_scroll` | `{READ, METADATA}` | **不能是纯 READ**：纯 READ 会进 run 级结果缓存，快照被缓存 = 下一次点击用过期 ref |

**快照与 ref**（`browser-snapshot.ts` + `browser-page-scripts.ts` + `browser-controller.ts`）：

- 快照脚本是**常量字符串**，模型只能影响 `maxElements`；点击/填写/按键只接受 `ref`。
- 绑定 `(tabId, pageVersion, ref)`：`did-navigate` 让 `pageVersion + 1` 并清空该标签页
  所有旧快照。校验失败统一 `stale_observation` / `element_missing`（409）。
- 填写走 `HTMLInputElement.prototype` 的原生 setter 再补发 `input/change`，
  否则 React 受控组件收不到变化。
- 密钥类输入（`type=password`）在快照里只显示「密码输入框」，不取 value。

**错误分类**（`browser/tools.py`）：能自救的（快照过期、元素不存在/不可见/禁用、
没开标签页、参数非法）→ `command_error` + 建议重新取快照；浏览器不可用/令牌错/RPC 挂
→ `tool_failure` 且 `retryable=False`，避免模型对着墙试到熔断。

**验证**：

- `tests/test_browser_tools.py` 16 例（真实本地 HTTP 桩，全链路）：定义完整性、参数必填、
  7 个工具的正常/失败路径、`browser_disabled`、能力声明护栏。
- `desktop/scripts/spikes/tools-smoke.cjs` 9 项断言全过：真实 Electron + 真实 Python 工具链
  + 本地固定测试页（表单 → 填写 `hello` → 点击提交 → 回读 URL 含 `q=hello`），
  并用**旧快照**再点一次，必须返回 `stale_observation`。
- 后端全量：`841 passed, 1 skipped`。

**未做**：`request_id` 的透传仍留到阶段 6（控制权要绑 run 时才需要，提前加只是一条
无用参数）；方案要求的「5 组真实模型任务」需要用户自己的模型，未代跑。

---

## 14. 阶段 5 实施记录（2026-11-26）

阶段 4 让快照「能校验」，阶段 5 解决它的三个现实缺口：**会撑爆上下文**（无预算）、
**大页面只看得到前 120 个**（无分页）、**局部刷新后 ref 会指错东西**（无元素复核）。

### 14.1 预算

| 项 | 值 | 依据 |
| --- | --- | --- |
| 单页元素数 | 默认 120，上限 200 | 超过 200 条模型也不会逐条读，只会浪费上下文 |
| 输出字符预算 | 10000 | 执行链还有 12000 字符二次裁剪；被上层砍掉时 JSON 尾部会残缺 |
| 单条元素成本估算 | 80 + 名称长度 | 约等于序列化后的 `ref/role/name/tag/disabled` 固定开销 |
| ref 编号上限 | 999（三位序号） | `el_1000` 会破坏 `el_\d{3}` 的解析约定；超出部分靠滚动分段 |

字符预算按**估算的序列化体积**算，不再只算名称长度：阶段 4 的实现按 `name + 40`
计，120 条元素实际输出约 15KB，会被上层静默截断。现在超预算的元素计入 `omitted`，
返回 `truncated=true` 与 `nextCursor`，模型知道「还有，但没给你」。

### 14.2 分页

`browser_get_page_snapshot` 新增可选 `cursor`；返回新增 `nextCursor`、`page`、`budget`。

- **同一份观察分页**：所有页共享一个 `observationId`，后一页的 ref 并入同一份白名单。
  否则「翻到第二页后想点第一页的搜索框」会报快照过期，模型只能在两页之间反复重取。
- **ref 用页面全局可见序号**，不是页内相对序号。第二页是 `el_006..el_010`，不是
  重新从 `el_001` 开始——重复编号会把第一页的属性覆盖掉。
- **cursor 是 opaque 句柄**（`obs_xxxxxxxx:120`），必须等于该观察的 `nextOffset`；
  模型自己拼 `offset` 会被 `invalid_arguments` 拒掉。导航后旧 cursor 直接
  `stale_observation`。
- **页大小沿用第一页**：第一页 `max_elements=5`、第二页不填时仍是 5，不会突然变成 120。
- 一份观察最多翻 5 页（`MAX_PAGES_PER_OBSERVATION`），防止对着巨型列表一直翻。

### 14.3 元素级复核

快照时为每个元素记录指纹 `fp = tag|role|稳定名称`，点击/填写/按键前在**页面侧**
重新计算并比对：

- 指纹里的名称**不读 `input.value`**，只取 aria-label / name 属性 / placeholder /
  文本。否则填完一次再填第二次，同一个输入框会被判成「元素变了」。
- 不一致返回 `element_changed`（409），工具层映射为 `command_error` +
  「重新取快照」。`element_changed` 与 `element_missing` 分开，是因为「元素还在但
  内容变了」和「元素没了」对模型是两种不同的恢复动作。
- 为什么必须有它：`did-navigate-in-page`（`history.pushState` / hash 变化）不递增
  `pageVersion`，SPA 可以把整块内容换掉；只靠阶段 4 的三条校验会漏。

### 14.4 两条实测发现

1. **新快照必须作废旧快照**。`data-ethos-ref` 是页面上的全局命名空间，重新取快照时
   ref 会重新编号。若旧观察还留在 store 里，模型拿旧 ref 点击时快照校验会通过，
   但属性已经指到别的元素上——正好是「点错东西」。现在 `create()` 先清空该标签页的
   旧观察（原来保留 8 份的设计取消：浏览器回退本身会触发导航，旧快照本来也会失效）。
2. **页面定时器被隐藏窗口节流**。验收里「点击后 600ms 删除元素」在 `show:false`
   的窗口里迟迟不执行，`element_missing` 用例因此假失败。给标签页加了
   `backgroundThrottling: false`：Agent 点击后常依赖页面自己的 `setTimeout` 完成
   跳转/渲染，被节流到 1Hz 会让「点完等结果」无故超时。

### 14.5 11 条验收用例

`backend-py/scripts/browser_snapshot_probe.py` 通过**真实工具链**
（`BrowserToolSet → BrowserService → BrowserRpcClient → HTTP → BrowserManager →
Chromium`）驱动，页面是 `desktop/scripts/spikes/snapshot-budget-smoke.cjs` 起的本地
固定测试页。

| 编号 | 用例 | 结果 |
| --- | --- | --- |
| C1 | 默认预算：单次 ≤120 个元素，页面总数不丢（300） | PASS（返回 119，total=300） |
| C2 | 超长名称下返回条数与 `usedChars` 都不超上限 | PASS（62 条，9920/10000） |
| C3 | 被预算截掉的元素计入 `omitted` 且 `truncated=true` | PASS（omitted=138） |
| C4 | `max_elements=5` 返回 5 条并给出 `nextCursor` | PASS |
| C5 | 第二页 ref 全局连续（el_006..el_010），不与第一页重叠 | PASS |
| C6 | 两页共享同一个 `observationId` | PASS |
| C7 | 用第一页的 ref 在第二页取回后仍可点击 | PASS（title 变为 `clicked:B001`） |
| C8 | 末页 `nextCursor=null` 且 `truncated=false` | PASS（返回 2/7 条） |
| C9 | 同 ref 文案变化 → `element_changed` | PASS |
| C10 | 元素被移除 → `element_missing`（不是内部错误） | PASS |
| C11 | 导航后旧 cursor → `stale_observation` | PASS |

**验证数据**：

- `desktop/scripts/spikes/snapshot-budget-smoke.cjs`：16 项断言全过（5 项环境/探针
  完整性 + 11 条用例）。
- `desktop/scripts/spikes/tools-smoke.cjs` 阶段 4 回归：9 项全过（新增字段没有破坏
  表单填写/提交/旧快照失效链路）。
- `tests/test_browser_tools.py` 增至 20 例：新增 cursor 透传、空 cursor 忽略、
  工具定义含 `cursor`、`element_changed` 分类。
- 后端全量：`847 passed, 1 skipped`。

**未做**：

- 真实模型任务验收仍需用户自己的模型（与阶段 4 同一批 5 组）。
- 指纹只能发现「tag/role/名称变化」。若页面用**同 tag、同文本**的新元素替换旧元素，
  指纹相同，无法区分；这种情况只能靠下一次导航/快照重取。
- 多个 Agent run 同时操作同一个标签页仍未做互斥（阶段 6 控制权绑定一起处理）。

---

## 15. 阶段 6 实施记录（2026-11-26）

阶段 6 做两件事：把 `request_id` 送进浏览器调用链，并用它做**控制权串行**。
没有它，两个 run 同时点同一个页面时，A 的 ref 会在 B 导航后指到别的元素上；
元素级复核能兜住一部分，但两个 run 会陷入「取快照 → 被踩 → 再取」的死循环。

### 15.1 `request_id` 透传链

```
agentrun/execution.py:chat()            request_id 是本次 run 的身份
  → ToolRegistry.execute(..., request_id=...)
  → 5 个 tool set 的 execute 签名（4 个只接不用）
  → BrowserToolSet 的 _op_* handler
  → BrowserService.call(..., request_id=...)
  → BrowserRpcClient.call(..., request_id=...)
  → POST /rpc  body: {method, params, request_id}
  → browser-rpc-server.ts dispatch(method, params, requestId)
```

- `request_id` 放在**请求体顶层**，不进 `params`：它是请求级身份，不是业务参数。
  放进 params 后每个方法都得转发一遍，漏一个就退化成匿名调用。
- 匿名（空 `request_id`）仍放行但不参与控制权：探针、`rpc-smoke.cjs` 这类
  本地脚本不应被迫伪造 run 身份。
- 转发面回归由 `tests/test_tool_registry_forwarding.py` 守住：`_FORWARDED_KEYWORDS`
  加了 `request_id`，并新增「假工具集记录 kwargs」的端到端用例。这条链 2026-11-20
  与 11-24 各断过一次，必须以测试而不是人工检查为准。

### 15.2 控制权状态机（`browser-control.ts`）

| 事件 | 结果 |
| --- | --- |
| 用户在面板上操作（`tabs.*` / `nav.*`，IPC） | 立即 `owner=user`，记录 `userActedAt` |
| Agent 首次写操作（不同 run 不持有） | 接管，`owner=agent` + `requestId` |
| 同一 run 再次操作 | 刷新租约（20s） |
| 另一个 run 在租约内操作 | `browser_busy`（409） |
| 用户刚操作过（<1.5s） | `user_took_control`（409） |
| Agent 空闲超过 20s | 租约过期，自动回到 `user` |
| `manager.dispose()` | `control.reset()` |

接管范围：`page.navigate` / `snapshot` / `click` / `fill` / `press` / `scroll`。
`page.url` 不接管——只读地址不改页面，任何 run 都可以看；`state` / `view.set`
也不算用户操作（那是面板在报告布局）。

**为什么会有 1.5s 的抑制窗口**：用户点一下地址栏后，Agent 若立刻把控制权抢回去，
界面上会看到「用户控制 ↔ Agent 控制」来回闪。冷却期让用户的操作真的生效，之后
Agent 再自动接管（不需要用户额外点「允许」）。

### 15.3 错误分类

| 错误码 | 分类 | 理由 |
| --- | --- | --- |
| `user_took_control` | `command_error` + `retryable=True` | 用户暂时接管，稍后重取快照即可继续 |
| `browser_busy` | `tool_failure` + `retryable=False` | 另一个 run 正在用；重试只会把两个任务都拖死，模型应停下告诉用户 |

### 15.4 UI

`BrowserState.control = { owner, requestId }` 随 30ms 合并的状态一起下发，
`BrowserToolbar` 把 `agentControlled` 传给 `BrowserControlStatus`：Agent 持有时
显示「Agent 控制」，用户动手后立刻变回「用户控制」。控件位置阶段 2 就留好了，
这次只接线。

### 15.5 验证数据

`backend-py/scripts/browser_control_probe.py`（真实 Python 工具链）+ 
`desktop/scripts/spikes/control-smoke.cjs`（真实 Electron）共 19 项断言全过：

| 编号 | 用例 | 结果 |
| --- | --- | --- |
| K1 | run A 的写操作成功并取得控制权 | PASS |
| K2 | run A 取得快照 | PASS |
| K3 | run B 在 A 持有控制权时被拒（`browser_busy`） | PASS |
| K4 | run A 重入仍成功（不会把自己锁死） | PASS |
| K5 | run B 的只读 `page.url` 不被拦截 | PASS |
| — | 探针结束后 `state.control = agent / req-probe-a` | PASS |
| — | `userTakeover()` 后 `state.control = user` | PASS |
| — | 用户夺回后 Agent 立刻写 → `user_took_control` | PASS |
| — | 1.8s 后同一调用 → 成功，`state.control` 回到 agent | PASS |
| — | 短租约（200ms）：第二个 run busy → 过期后接管 | PASS |

**回归**：`tools-smoke.cjs` 9 项、`snapshot-budget-smoke.cjs` 16 项全过；
`tests/test_browser_tools.py` 增至 24 例；后端全量 `852 passed, 1 skipped`；
`web-v1` `tsc --noEmit` 通过。

**未做**：

- run 结束没有主动 `control.release`（`execution.py` 没有可靠的收尾钩子），
  只能靠 20s 租约过期。表现为：任务刚结束、用户立刻操作前，控制权可能还挂在
  Agent 名下——但用户一动手就会夺回，不影响实际使用。
- 多窗口 / 多面板共享同一分区的场景没有额外协调（当前只有一个 BrowserWindow）。
- 真实模型任务验收仍需用户自己的模型（与阶段 4/5 同一批 5 组）。

---

## 16. 阶段 7 实施记录（2026-11-26）

阶段 7 处理「网页能把什么带回本机」这件事：下载、非 http(s) 导航、新窗口。
方案第 5 节把「下载目录」写进明确不做清单，所以这里的目标是**拒绝 + 可见反馈**，
不是「把文件存到某个目录」。

### 16.1 下载：一律拒绝

`BrowserManager` 对 `persist:ethos-browser` 分区 session 注册一次 `will-download`
（每个 tab 各注册会重复触发 N 次），`event.preventDefault()` 取消下载，并把提示
落到来源标签页的 `notice` 字段。

- 不实现下载目录：任意网页能写文件，等于把「网页内容」变成「本机文件投递通道」。
- 文件名经过 `describeDownload()` 清洗（去路径分隔符、控制字符、截断 80 字符），
  提示里不出现完整 URL。
- 提示必须可见，否则用户只看到「点了下载没反应」。`notice` 与 `lastError` 分开：
  复用 `lastError` 会被下一次 `did-start-loading` 立刻清掉。

### 16.2 导航协议白名单

- 用户输入：`normalizeUrl()` 只放行 http/https（阶段 2 已有）。
- 页面自发导航：`will-navigate` 拦非 http(s)，提示写到 `notice`。
- 新窗口：`setWindowOpenHandler` 只把 http(s) 目标转成同分区新标签页，其余拒绝。

**一条实测发现（改变了验收断言）**：http 页面点击 `file:///C:/Windows/win.ini`
链接时，Chromium 在**渲染进程**就拒绝了这次导航，Electron 侧连 `will-navigate` /
`did-fail-load` 都收不到（加了临时日志确认，零事件）。所以：

- 安全目标成立：URL 不变、没有新标签页、没有文件被读取；
- 但「点击时有 UI 提示」做不到，除非在页面里常驻注入点击守卫脚本。那会打破
  「快照脚本是唯一按需脚本」的边界（方案 §5 明确不注入 preload），收益不值得
  破坏这条约束。`will-navigate` 保留为纵深防线（覆盖 `about:blank` 等仍会触发
  事件的路径）。

### 16.3 UI

- `BrowserTabState` 新增 `notice`；`BrowserPanel` 用 warning 色横幅显示。
  成功导航（`did-navigate`）清空，避免「人都走了提示还挂着」。
- 修了一个阶段 2 遗留的互斥漏洞：`Ctrl+Shift+B` 只翻转 `browserOpen`，没有关
  Sandbox 面板，两个面板会同时占右侧槽位、网页视图盖住文件树。现在快捷键路径
  也 `setSandboxOpen(false)`（放在 updater 外面，保持 updater 纯函数）。
- 核对阶段 2 记录里「其它模态未接入 suspended」：当前全仓只有 `SettingsDialog`
  一个顶层模态，`custom-model-dialog` 嵌套在设置弹窗内（`settingsOpen` 已覆盖），
  无需额外接线。

### 16.4 验证数据

`desktop/scripts/spikes/policy-smoke.cjs`（真实 Electron + 本地固定测试页）11 项全过：

| 用例 | 结果 |
| --- | --- |
| 下载触发后临时下载目录为空 | PASS |
| 下载提示含「已拦截下载：blocked.txt」 | PASS |
| 点击 `file://` 链接后 URL 不变、页面标题不变 | PASS |
| `window.open(http)` 新增标签页并加载目标 | PASS |
| `window.open(file://)` 不新增标签页、不出现 file: URL | PASS |
| 页面脚本 `location.href = file://` 被拦，URL 不变 | PASS |
| 正常 http 导航不受误伤（标题变为目标页） | PASS |
| 全部用例结束后下载目录仍为空 | PASS |

**回归**：`tools-smoke` 9 项、`snapshot-budget-smoke` 16 项、`control-smoke` 19 项
全过；`web-v1` `tsc --noEmit` 通过。后端无改动。

**未做**：

- 下载目录 / 文件上传（方案 §5 明确不做）。
- 页面内非 http(s) 链接的**点击提示**（原因见 16.2，不做常驻注入）。
- 真实模型任务验收仍需用户自己的模型（与阶段 4/5/6 同一批 5 组）。

---

## 17. 阶段 8 实施记录（2026-11-26）：Trace、可观测性与 E2E

### 17.1 实施前核验

| 项 | 结果 |
| --- | --- |
| 分支 / HEAD | `feat/desktop-browser-view` / `10a64cf` |
| 工作区 | 阶段 1~8 改动均未提交（`git status` 与逐阶段记录一致） |
| 阶段 7 结果 | `policy-smoke` 11 项全过；`tools/snapshot/control` 回归通过 |
| 现有 Trace | `traces` + `trace_steps` 两表；`TOOL_CALL` 步骤已有 `duration_ms`/`status`/`metadata`(JSON) |
| 浏览器调用链 | `agentrun.execution` → `ToolRegistry` → `BrowserToolSet` → `BrowserService` → `BrowserRpcClient` → HTTP → `browser-rpc-server` → `BrowserManager` |

结论：**不新建追踪系统**。`trace_steps.metadata` 本来就是 JSON 旁路，浏览器专有字段
直接写进去即可；需要补的是「工具如何把字段交出来」而不是一张新表。

### 17.2 Trace 落地

字段来源（全部落在 `TOOL_CALL` 步骤）：

| 字段 | 来源 | 说明 |
| --- | --- | --- |
| `run_id` | `chat()` 的 `request_id` | 与 `traces.request_id` 同值，落到 metadata 是为了只查 `trace_steps` 也能还原 |
| `session_id` | 同上 | 同上 |
| `tool_call_id` | `tc["id"]` | 关联模型侧的 tool_call |
| `tab_id` | Electron 返回值 `tabId` | `click/fill/press/scroll` 原本没有，由 RPC 层统一补齐 |
| `action` | `BrowserToolSet` 的映射 | `snapshot` / `click` / `fill` / `press` / `scroll` / `open_url` / `get_url` |
| `observation_id` | 结果 `observationId` 或入参 | 分页的多步共享同一个 observation |
| `duration_ms` | `TOOL_CALL` 步骤自带 | 工具总耗时（含权限裁决、参数解析） |
| `status` | 工具自报 | `ok` / `error` |
| `error_code` | 工具自报 | 与模型看到的 `code` 同源 |

耗时拆解：

- **模型决策**：`LLM_CALL` 步骤的 `duration_ms`（既有）；
- **工具执行总耗时**：`TOOL_CALL` 步骤的 `duration_ms`（既有）；
- **RPC 耗时**：`metadata.rpc_ms`（本次新增，在 `BrowserService` 的
  `call/navigate/current_url` 里计时）；`metadata.tool_ms` 是 handler 自身耗时。

传递方式：`tool_trace_scope()`（ContextVar）包住 `tool_executor.execute`，工具用
`record_tool_trace(**fields)` 写旁路字典，execution 退出后合并进 `tool_step.metadata`。
**不把元数据塞进工具结果字符串**——那会让模型看到平台内部字段，且每个消费方
（SSE、工具卡、历史）都要记得剥离。

脱敏（需求明确要求不记录 Token / Cookie / 密码 / 敏感表单内容）：

- 入参：`redact_tool_input()` 把 `browser_fill.value` 换成 `<redacted: N chars>`；
- 结果：`redact_tool_output()` 只保留白名单字段（tab/url/observation/page/status/
  error_code 等），**丢弃 `elements` 明细与所有 `name`**——快照里的输入框名称可能
  就是用户刚填进去的内容；
- Token / Cookie 从来不在工具参数或结果里（RPC 令牌只在 HTTP header）。

### 17.3 顺带修的两个真实缺口

1. **run 结束不释放控制权**：阶段 6 靠 20s 租约兜底，表现为「用户点了取消，Agent
   立刻重试却拿到 `browser_busy`」。现在 `agentrun/service.py` 的 `finally` 调用
   `release_browser_control(request_id)` → RPC `control.release`，只释放自己持有的
   租约（幂等）。
2. **页面崩溃后模型看到的是「空页面」**：错误页本身能执行脚本，快照会返回 0 个元素。
   现在 `BrowserTab` 用 `rendererGone` 标记（活到下一次主动导航），`runScript` 抛
   `page_crashed`；同时崩溃后错误页的 `data:` 地址不再覆盖 `state.url`，重试时
   还知道崩的是哪一页。

### 17.4 E2E 12 项

`desktop/scripts/spikes/e2e-smoke.cjs` + `backend-py/scripts/browser_e2e_probe.py`，
真实 Electron + 真实 Python 工具链 + 本地固定测试页，31 项断言全过：

| 编号 | 用例 | 实际结果 |
| --- | --- | --- |
| E1 | 打开网页 | PASS（tabId + `/app`） |
| E2 | 获取快照 | PASS（4 个可交互元素，含搜索框/提交） |
| E3 | 点击指定元素 | PASS（标题变为 `已切换-1`） |
| E4 | 填写表单 | PASS（写入 11 字符） |
| E5 | 按键提交 | PASS（`/results?q=hello+world`） |
| E6 | 页面滚动 | PASS（0 → 800） |
| E7 | 多标签页切换 | PASS（操作第二页，标题 `clicked-second`） |
| E8 | 导航后旧快照失效 | PASS（`stale_observation`） |
| E9 | 用户接管后 Agent 被拒 | PASS（`user_took_control`） |
| E10 | Agent 取消后控制权释放 | PASS（`control.release` 后另一个 run 立即可操作） |
| E11 | RPC 断开 | PASS（`unavailable`，`tool_failure`，不可重试） |
| E12 | 页面崩溃 | PASS（`page_crashed` → 重新打开恢复） |

### 17.5 Trace 链路验证

`tests/test_browser_trace_store.py`：真实 `Tracer` + `Store` + `BrowserToolSet`（RPC 桩），
按 execution 的写法打点后从 `trace_steps` 查回，断言：

- 三步顺序 = `snapshot → fill → click`，`seq` 递增；
- 每步 metadata 含 `run_id / session_id / tool_call_id / action / status`；
- 快照步含 `tab_id / observation_id / elements / rpc_ms / tool_ms`；
- 失败步 `error_code=element_missing`；
- 把整个 `trace_steps` 序列化后**搜不到**测试里填入的密码明文。

`tests/test_tool_trace.py` 另外锁定脱敏函数本身（6 条）。

### 17.6 五组真实模型任务

**未执行**。运行环境没有模型凭据（`OPENAI_API_KEY` / `DEEPSEEK_API_KEY` /
`ANTHROPIC_API_KEY` 均未设置），用户密钥在 `user_providers` 表里、只能由用户在
桌面端配置；擅自读取或调用用户额度不合适。

五组任务的目标、发送话术、期望轨迹、A/B/C 判定与记录表已写入
`docs/design/browser-model-tasks.md`，填完回填到本节。

### 17.7 验证数据汇总

| 项 | 结果 |
| --- | --- |
| 后端全量 | `861 passed, 1 skipped` |
| `e2e-smoke.cjs` | 31 项全过（12 条用例） |
| `tools-smoke.cjs` | 9 项全过 |
| `snapshot-budget-smoke.cjs` | 16 项全过 |
| `control-smoke.cjs` | 19 项全过 |
| `policy-smoke.cjs` | 11 项全过 |
| `web-v1` `tsc --noEmit` | 通过 |
| `desktop` `tsc` | 通过 |

### 17.8 未做

- **五组真实模型任务**（无凭据）→ 阶段结论 **PARTIAL**；这是唯一未达成的验收项。
- 真实模型 run 的 Trace 还原依赖上一项：目前只验证到「工具调用 → trace_steps」
  的存储形状，没有验证「一次真实对话里的完整顺序」。
- `tool_call_records`（历史工具卡数据）仍存 `result[:2000]` 原文，与 trace 的脱敏
  口径不同。这是既有行为，本阶段没有改动；如果要统一，应先确认前端工具卡是否
  需要元素明细。

---

## 9. 不改动清单（提交边界）

浏览器相关提交**不得**包含下列文件，也不得对它们做格式化：

```
backend-py/src/edith_backend/internal/{tools.py, systemtools/tools.py,
    cronjob/tools.py, agentrun/plan_tools.py}
backend-py/tests/test_tool_registry_forwarding.py
backend-py/inspect_*.py
```

例外：阶段 6 为了让 `request_id` 进入转发面，**必须**修改这 4 个源文件 + 扩守卫测试
（与 `context`、`tool_call_id` 两次断链是同类改动）。这部分要单独成一个 commit，
说明「为浏览器 RPC 透传身份」，与浏览器主体功能分开审查；`browser-control.ts`、
RPC 闸门、IPC 夺权属于控制权实现，与主体一起走。
