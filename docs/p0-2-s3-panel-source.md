# P0-2 S3：面板数据来源与执行链同源

**状态**：已实施并提交。前置：S2（文件工具 + Git 已落 `local_root`）。
**范围**：只改文件面板的两条直读路径。未动前端。

---

## 0. 一句话结论

S2 之后执行链已经落在用户目录，但面板还留着一条静默回落：

> 本地读不到 → 回落读沙箱 → 读到 `workspace-volumes/{id}` 的旧副本。

用户看到的是旧数据，界面上没有任何提示。S3 把这条回落按**权威根是谁**拆开：
未绑定的工作区照旧回落（两者本来就是同一份数据），绑定态不再回落。

---

## 1. 根因：`Path | None` 把三件相反的事压成一个值

`LocalVolumeBackend.resolve_workspace_path()` 返回 `Path | None`，四类情况共用 `None`：

| 实际情况 | `None` 的含义 | 应做 |
| --- | --- | --- |
| 不是 `workspace/{id}/…` | 不是工作区路径 | 回落沙箱 |
| 未绑定，副本目录还没建 | 本地没有 | 回落沙箱（按需初始化） |
| 有前缀，路径在前缀之外 | 映射规则决定不落本地 | 回落沙箱 |
| **绑定目录被移动/删除** | 本地没有 | **报错** |

前三行的处置是"去沙箱看看"，第四行必须是"停下来"。一个 `None` 表达不出这个区别，
于是调用方只能统一回落——这就是那条静默回落的来源。

---

## 2. 改动：分类而不是判空

`LocalLookup`（`workspace/volume_backend.py`）三值：

| kind | 含义 | 调用方行为 |
| --- | --- | --- |
| `SANDBOX` | 本地没有权威文件 | 走沙箱 |
| `READY` | 本地路径已解析（存在性由调用方判断） | 直读；不存在时看 `fallback_allowed` |
| `UNAVAILABLE` | 绑定目录不可用 | **409 `workspace_unavailable`** |

`fallback_allowed` 只在 `READY` 且本地没有时起作用，取值等于"是否未绑定"：

- 未绑定 → `True`：本地与沙箱是同一份数据，谁先建出来都行；
- 绑定态 → `False`：本地没有就是没有。

两个消费者（`sandbox/http.py`）：

| 接口 | 绑定目录不可用 | 绑定态文件不存在 | 未绑定 |
| --- | --- | --- | --- |
| `GET /internal/sandbox/files` | 409 | 空列表 | 回落沙箱（不变） |
| `GET /internal/sandbox/files/content` | 409 | 404 `file_not_found` | 回落沙箱（不变） |

`resolve_workspace_path()` 保留为 `lookup_workspace_path()` 的路径视图（`READY` 时给路径，
否则 `None`），避免两份逻辑分叉；新代码应当用 lookup。

---

## 3. 不需要改前端的原因

`sandbox-panel.tsx` 已经把 `body.message` 渲染成目录级错误并带「重试」按钮
（`state.error` 分支）。409 会自然落在那里，用户看到的是
「工作区绑定的本地目录不可用：…（可能被移动、改名，或所在磁盘掉线）。已停止读取，
避免显示另一份数据。」

这比静默显示旧副本好得多：错误是可见的，而且文案直接指向可执行动作。

---

## 4. 未受影响的三条

| 入口 | 为什么不受影响 |
| --- | --- |
| 上传（`upload_file`） | 目标是 `uploads/{name}`，不是工作区路径，走沙箱的暂存区 |
| 下载（`download_file`） | 只允许 `artifacts/`，不经 `lookup` |
| 非工作区路径（`skills/…`） | `lookup` 返回 `SANDBOX`，行为与改造前一致 |

**有前缀的绑定**（`prefix` 非空）在面板侧仍然是"前缀之外 → 沙箱"，这是既有映射规则，
不是本次引入的。执行链侧（S2）对这类绑定是显式拒绝——两边的处置不同是刻意的：
面板可以展示"虚拟视图"，Agent 不能半映射地写文件。

---

## 5. 改动面

| 文件 | 改动 |
| --- | --- |
| `workspace/volume_backend.py` | 新增 `LocalLookupKind` / `LocalLookup` / `lookup_workspace_path()`；`resolve_workspace_path()` 改为其路径视图 |
| `sandbox/service.py` | `local_workspace_path()` → `local_workspace_lookup()`（返回 `LocalLookup`） |
| `sandbox/http.py` | `list_files` / `read_content` 按 kind 分流；新增 409 `workspace_unavailable`、404 `file_not_found` |

**未改**：前端、`upload_file`、`download_file`、`workspace_path()` 的授权校验。

---

## 6. 验收

| 项 | 结果 |
| --- | --- |
| `tests/test_local_workspace_lookup.py`（分类契约，纯单元） | 13 passed |
| `tests/test_panel_workspace_source.py`（真实 `TestClient` 走 HTTP） | 4 passed |
| 全量 `pytest tests` | **659 passed, 1 skipped**（S2 后 642 + 17） |

HTTP 那组覆盖的是消费者而不是纯函数：列出绑定目录、绑定目录消失后 409、
文件不存在 404、正常读内容。

---

## 7. 下一步

**S4**：Shell 的 cwd 与虚拟绝对路径映射取同一根。这是绑定态最后一块视图分裂——
`_op_sandbox_run_command` 的 cwd 仍硬编码 `/home/user/work`，`_translate` 只换前缀。
**S5/S6** 在其后。P0-SEC-1（面板绕过敏感检查）仍独立。
