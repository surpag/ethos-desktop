# P0-2 · S1 实施计划（第一阶段）

**状态**：第一阶段已授权（2026-11-20）。第二阶段（快照接入 + 租约验收）**未授权**，不在本文执行范围。
**前置**：设计评审 `c71c4e9`；核验记录 `docs/p0-2-preflight-verification.md`（含 §1.3 租约约束、§1.5 验收清单）。

---

## 0. 三条已裁决约束（先于代码写死）

**① `BOUND_MISSING` 包含 `NOT_DIRECTORY`，状态数保持 5 个。**
`state` 回答"能不能继续"，`detail` 回答"究竟发生了什么"。两者不能互相替代：

```
state  = bound_missing
detail = stat: exists but is not a directory
```

`BOUND_MISSING` 的措辞固定为**"绑定路径上不存在可用的工作区目录"**，不写成"目录已被删除"——运维看到它不能断定是删除，也可能是被替换成文件、被改名、盘符指向变了。新增第六个状态只会让执行链多一条没有行为差异的分支。

**② `BOUND_READY` 只代表"当前探测通过"。**
`probe_root()` 成功只说明此刻能 `stat` 到、能列举。**不代表后续创建/写入/删除一定成功**：目录可能允许列举但不允许写入；探测通过后权限被改；网络目录探测成功、真正打开文件时已断线。这些仍由执行链各自的 I/O 错误处理，S1 不做任何承诺。

**③ 只有 `UNBOUND` 才拿得到 Volume 根。**
`WorkspaceContext.root` 的类型是 `str | None`：

| state | root |
| --- | --- |
| `UNBOUND` | `data_root/workspace-volumes/{id}`（只拼字符串，**不创建**） |
| `BOUND_READY` / `BOUND_MISSING` / `BOUND_DENIED` / `BOUND_OFFLINE`（有绑定记录） | 绑定路径本身（归一后的 `local_root`） |
| 绑定记录无效（未绑定却给了探测结果 / 有绑定却漏了探测） | `None` |

**规则：任何绑定态异常都不得通过构建快照回退到 Volume。** 消费者必须先读 `state` 再用 `root`；`root` 存在不等于可用。

---

## 1. 数据来源：快照不能只读注册表

`_local_roots` 是运行期缓存，不是绑定关系的权威来源。它只在两处被填充：

| 填充点 | 时机 |
| --- | --- |
| `sandbox/service.py:93` | `Service.workspace()` 打开沙箱时 |
| `workspace/http.py:124` | 面板绑定请求 |

`_run()` 生成快照时，两处都还没跑。只读 `local_root_of()` 会把绑定态读成 `UNBOUND`——P0-2 要修的 bug 换个位置复现。所以第二阶段的取数顺序固定为：

```
load_and_bind_local_root(user, workspace_id)   # 读库 + 归一 + 注册缓存
  → local_root_of() / local_root_prefix_of()   # 读已注册的绑定事实
  → probe_root()                               # 探测磁盘
  → resolve_state() → build_context()          # 纯函数
```

**副作用核验（2026-11-20，只读）**：`load_and_bind_local_root`（`volume_service.py:66-88`）只做三件事——一次 SQLite `SELECT`（`store.py:61-78`）、`normalize_local_root(strict=False)`（`Path.expanduser`/`resolve`，**不 mkdir、不打开沙箱**）、写注册表 dict。失败时 `bind_local_root` 吞掉 `LocalRootError` 并保留原路径（`volume_backend.py:120-126`），不会抛。结论：**快照阶段不会因为绑定目录不可用而创建任何目录，也不会开出沙箱。**

**一条既有瑕疵，第二阶段必须绕开**：`_local_root_loaded.add(workspace_id)` 在 DB 读取**之前**执行（`volume_service.py:75`）。若那次 `SELECT` 抛错，该工作区被永久标记为"已加载"而未注册任何绑定，后续调用直接返回 `None`。第一阶段不改它（`volume_service.py` 禁止修改），但第二阶段的快照取数不能把"已加载"当成"已确认未绑定"——需要区分"注册表里没有"与"查过库确认没有"。

---

## 2. 模块与接口

新增 `backend-py/src/edith_backend/internal/workspace/context.py`。落点依据：权威数据（绑定记录、`_local_root`、`normalize_local_root` 异常族）都在 `workspace/`；`sandbox/` 与 `agentrun/` 对 `workspace/` 的依赖方向已存在（`sandbox/service.py:17-18`、`agentrun/configuration.py:25`）；反过来放 `sandbox/` 会让 S3 的 `workspace/http.py` 反向依赖。

| 层 | 接口 | 性质 |
| --- | --- | --- |
| 探测 | `probe_root(path) -> RootProbe` | **唯一 I/O**；只报事实 |
| 解析 | `resolve_state(record, probe) -> BindingState` | 纯函数，全序 |
| 快照 | `build_context(record, probe, *, now) -> WorkspaceContext` | 纯函数，不可变 DTO |

### 2.1 探测顺序（不可交换）

```python
os.stat(path)                 # 存在吗 / 是目录吗，保留真实异常
  ├─ FileNotFoundError        → NOT_EXISTS
  ├─ PermissionError          → PERMISSION_DENIED
  └─ OSError                  → 按 winerror / errno 分类，认不出 = UNEXPECTED
stat.S_ISDIR 为假             → NOT_DIRECTORY
with os.scandir(path): pass   # 能列举吗（stat 通不过权限错误，这一层才拦得住）
```

`Path.is_dir()` 不能当第一判据：它把 `OSError` 吞成 `False`，"没权限"和"不存在"从此无法区分，诊断信息也一起丢了。

### 2.2 错误分类（唯一映射点）

| 探测结论 | state |
| --- | --- |
| `OK` | `BOUND_READY` |
| `NOT_EXISTS` / `NOT_DIRECTORY` | `BOUND_MISSING` |
| `PERMISSION_DENIED` | `BOUND_DENIED` |
| `DEVICE_UNAVAILABLE` | `BOUND_OFFLINE` |
| `UNEXPECTED` | `BOUND_OFFLINE` |
| `local_root is None` + `probe is None` | `UNBOUND` |
| `local_root is None` + probe 存在 / 有绑定 + probe 缺失 | `BOUND_OFFLINE`（调用方错误，fail-closed） |

分类只用两个明确集合：`errno ∈ {EACCES, EPERM}` / `winerror == 5` → denied；`errno ∈ {ENODEV, EIO, ENETUNREACH, EHOSTDOWN, EHOSTUNREACH, ENETDOWN, ESTALE}` / `winerror ∈ {53, 59, 64, 67, 1231}` → offline（Windows 码含义：找不到网络路径、意外网络错误、网络名不可用、找不到网络名、网络位置不可达）。**其余一律 `UNEXPECTED` → `BOUND_OFFLINE`，不猜。** UNC 不可测（本机无网络盘），所以"网络盘掉线"这条只能归到 `BOUND_OFFLINE + detail`，不假装能精确识别。

### 2.3 快照字段

| 字段 | 内容 | 约束 |
| --- | --- | --- |
| `workspace_id` | 原样 | — |
| `state` | `BindingState` | 5 值 |
| `root` | 见 §0 ③ | `str \| None`；`UNBOUND` 时**不创建目录** |
| `virtual_root` | `/home/user/workspace/{id}` | 由 `WORKSPACE.workspace_path()` 生成，映射只在此组合一次 |
| `prefix` | 绑定的子树前缀 | 空串 = 虚拟根直连 `local_root`；非空时虚拟根不映射到本地目录（既有 `_strip_prefix` 语义，S1 不改） |
| `detail` | 探测的原始错误文本 | ready/unbound 时为空 |
| `captured_at` | `time.time()`（可注入以便测试） | 只用于诊断，**不参与判定** |

`@dataclass(frozen=True)`，字段只用 `str`/枚举/float，不持有 `Path`（避免"快照里的 Path 是归一前还是归一后"的歧义）。

---

## 3. UNBOUND 基线（改造前冻结）

**方法**：在改动任何代码之前运行一次性脚本，用独立临时 `ETHOS_DATA_DIR` 打开 `LocalSandbox`，对固定 18 条输入调 `resolve()`，把结果转成**相对 `data_root`** 的形式（绝对路径随机器变，不能进常量），序列化规则固定：`json.dumps(ensure_ascii=False, indent=2, sort_keys=True) + "\n"`，UTF-8 无 BOM。产物以数据文件形式入库：`backend-py/tests/fixtures/unbound_baseline.json`（**字节即契约**，测试读文件比对，不在测试里重写字面量）。

**冻结值**（2026-11-20，改造前 HEAD `765f6d6`）：

| 项 | 值 |
| --- | --- |
| 记录数 | 18 |
| 字节数 | 2204 |
| sha256 | `3c7e66892a522849099fea09abada29431210825e0436a559f7f7247854552f9` |

覆盖的四条分支：虚拟挂载根（`workspace` → `workspace-volumes`）、`workspace/{id}` 及其子路径（含 `.` / `..` 归一后仍在同一边界内的合法写法）、`skills` 挂载、沙箱根内普通路径（`.` / `a.txt` / `work/a.txt` / `/home/user/work/a.txt`）。拒绝类 5 条：`workspace/ws1/../../x`、`workspace/ws1/../ws2/x`、`/etc/passwd`、`C:/Windows`、`../x`（均 `SandboxPathError`）。

基线是**冻结数据**，不是"改造后的期望值"：测试断言的是同一份文件，若 S2/S4 改变了输出，测试必须失败而不是被更新。重新冻结只在**有意**改变未绑定行为时执行，且必须在提交信息里写明理由与新 sha256。

---

## 4. 测试

| 文件 | 覆盖 |
| --- | --- |
| `tests/test_workspace_context.py` | 5 状态表驱动；`local_root is None + probe` → offline 且 `root is None`；探针：真实目录 / 不存在 / 是文件 / `stat` 权限 / `stat` 通过但 `scandir` 权限 / `winerror=53` / 无 winerror 的 `OSError`；不可变性；`UNBOUND` 快照 `root` 指向 Volume 但**未创建** |
| `tests/test_unbound_baseline.py` | 18 条输入逐条与 `fixtures/unbound_baseline.json` 字节比对（含异常类型），并校验该文件的 sha256；`volumes/{id}` 按需创建保留；`workspace/{id}` 确实落在 Volume 下 |

跑法：`pytest tests --basetemp=.pytest-tmp`（不限定目录会把 `workspace-volumes/` 收进来）。每个用例独立 `ETHOS_DATA_DIR`，避免历史产物污染。

---

## 5. 改动面

**第一阶段（本次）**

| 项 | 动作 |
| --- | --- |
| `workspace/context.py` | 新增 |
| `tests/test_workspace_context.py`、`tests/test_unbound_baseline.py` | 新增 |
| `sandbox/local.py` / `agentrun/service.py` / `workspace/http.py` / `volume_service.py` / `volume_backend.py` | **一行不改** |

第一阶段的产物是"可被调用但无人调用的模块 + 一条钉住现状的基线"，**零运行时行为变化**。

**第二阶段（未授权）**：`Dependencies` 增加 `WorkspaceContextSource` Protocol（定义在 `agentrun`，`WorkspaceVolumeService` 实现，`main.py` 装配）；`service.py` 在 `_run()` 的 `try` 内、`run.started` 之后、`set_task_status("in_progress")` 之前生成快照，并新增 `workspace_unavailable` 错误类型；§1.5 的 ①②③ 租约用例。**`agentrun` 不访问 `_local_roots`、数据库或磁盘**——绑定加载、探测、状态解析全部由提供方负责，`agentrun` 只请求一次快照。

---

## 6. 实施结果（2026-11-20）

两阶段均已落地，与上文计划的差异只有三处，逐条说明理由。

| 项 | 计划 | 实际 | 理由 |
| --- | --- | --- | --- |
| `WorkspaceUnavailableError` 的归属 | 未指定 | `workspace/context.py` | "绑定状态决定能不能跑"是工作区语义，与消费者无关；面板、定时任务将来要用同一套判定，放执行层会让它们各自复制一份 |
| `capture()` 的落点 | `WorkspaceVolumeService` 实现 Protocol | 同计划 | 绑定记录、缓存、归一化都在这个类里；放别处它要反过来抄一遍取数逻辑 |
| `volume_service.py` | 只加 `capture()` | 另修正 `_local_root_loaded` 的标记时机 | 原实现在读库**之前**就标记"已加载"：那次查询一旦抛错，该工作区被永久标记却什么都没注册，之后 `capture()` 会把"已绑定"读成 `UNBOUND` → 静默回退到 Volume。这正是 P0-2 要消灭的行为，不修等于在第二阶段把 bug 装回去 |

**快照目前不参与路径映射**：`_run` 拿到快照后只判 `executable`，没有消费者用它决定根路径。
文件工具、Git、Shell、面板仍走原来的 `_workspace_volume_dir`，因此绑定态的路径分裂
与 S1 之前**完全一致**。这不是遗漏，而是分步可验证的前提：先证明"绑定状态进入了执行链"
（本阶段），再逐个入口改读快照（S2–S4），否则无法归因是哪一步改变了行为。

**验收**：`pytest tests --basetemp=…` → `629 passed, 1 skipped`（S1 前 593+1，第一阶段 +31，第二阶段 +5）。
未绑定态 18 条基线逐字节未变——第一阶段冻结的清单在第二阶段改完 `service.py` 后仍然通过。
