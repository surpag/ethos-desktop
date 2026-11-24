# P0-2 S5 / S6：Git 回归与组合验收

**状态**：已完成。前置：S4（shell 的 cwd 与路径翻译同源）。

---

## 0. 一句话结论

S5 不需要改代码——Git 的 cwd 走的是 `resolve()`，S2 已经改对了。
S6 需要改**一处错误语义**：绑定目录不可用时，敏感路径检查抢在前面把它报成
「路径无法解析为真实位置」，用户收到的建议是「改操作其他文件」——方向完全错。

---

## 1. S5：Git 为什么不用改

调用链只有一条，中间没有任何一处自己拼路径：

```
_op_git(args, workspace_id)
  → WORKSPACE.workspace_path(id)            # 虚拟路径 /home/user/workspace/{id}
  → run_git(cwd=那个虚拟路径)
  → sandbox.commands.run(cwd=…)
  → LocalSandbox.resolve(cwd)               # ← 唯一的落点判定
```

S2 改的是最后那个 `resolve()`，所以 Git 自动跟着落对。S4 改的是 shell 的
cwd 默认值与路径翻译，**不参与** `_op_git`（它显式传 cwd）。

`tests/test_git_bound_workspace.py`（3 项）把这条钉住，防止将来某次优化
（比如"git 自己拼绝对路径更快"）把它拆开：

| 用例 | 断言 |
| --- | --- |
| `test_git_status_runs_in_bound_root` | `git init` 落在绑定目录，`git status` exit=0，输出无宿主路径 |
| `test_git_sees_file_written_by_file_tool` | 文件工具写的文件，`git status --porcelain` 当轮可见 |
| `test_git_and_file_tools_never_touch_volume_copy` | 全程不创建 `workspace-volumes/{id}` |

没有可执行 git 时 `pytest.skip`：这层测的是落点，不是 git 本身。

---

## 2. S6：一处真实缺陷——错误语义被抢占

### 2.1 现象

绑定目录在 run 中期被删，三个入口的报错：

| 入口 | 期望 | 实际（修复前） |
| --- | --- | --- |
| 文件工具 | 「绑定目录不可用，可能被移动/删除」 | **「敏感路径不可访问：路径无法解析为真实位置」** + 建议「改操作其他文件」 |
| shell | 同上 | 同上（cwd 解析同一条 `resolve()`） |
| git | 同上 | 同上 |

用户会以为文件被安全策略拦了，去换路径；真正的动作是去工作区设置检查目录。

### 2.2 根因

`tools.py` 的敏感路径检查分两遍，第二遍要把逻辑路径解析成宿主真实路径
（堵符号链接绕过），失败时按「不可判定 = 拒绝」处理：

```python
except Exception:                      # 无法解析真实落点 = 无法判定
    return "路径无法解析为真实位置"
```

`WorkspaceUnavailableError` 恰好落进这个兜底分支。它**不是**"路径可疑"，而是
"这次执行没有可用的权威根"——两件事的处置完全不同（前者换路径，后者修绑定）。

### 2.3 修法

在两处真实路径解析里显式透传，不进兜底：

```python
except WorkspaceUnavailableError:
    raise          # 交给执行层的 fail-closed 报准确原因
```

为什么是 `raise` 而不是在这里返回一句文案：这个函数返回 `str` 表示"命中的敏感段"，
调用方据此构造 `permission_denied`。让绑定故障借用这个形状，等于把两种故障
编码进同一个返回类型——下一次还会有人踩。抛出之后由 `execute` 的既有
`except Exception` 转成 `tool_failure`（`retryable=false`，显式告诉模型别重试）。

**注意 `retryable=false` 是刻意的**：绑定目录被删不是模型能自救的错误，
继续重试只会把熔断打满。

---

## 3. 组合验收：一轮里四个入口互相看见

`tests/test_p0_2_integration.py`（3 项）不引入新实现，只钉住组合：

### 3.1 绑定态 · 全链路成功

```
文件工具写 from-tool.txt
  → shell cat（相对路径，cwd 命中）
  → shell printf > from-shell.txt
  → 文件工具读回
  → shell 写 /home/user/workspace/{id}/from-abs.txt（绝对虚拟路径）
  → 落在同一个根
  → git status 看到两个文件
  → workspace-volumes/{id} 不存在
```

### 3.2 绑定态 · 运行中期目录消失

三入口**全部** `retryable=false`、文案含「不可用」、不在原路径重建目录、
不回退旧副本。这一条是 P0-2 的核心承诺：**宁可停下来，也不给一份看起来正常的错数据**。

### 3.3 未绑定态 · 行为与改造前一致

文件工具与 shell 落在 `workspace-volumes/{id}`；沙箱级 `work/` 仍要用
`/home/user/work` 绝对路径才能到达，不会因为 cwd 变了被写进工作区。

面板入口由 `test_panel_workspace_source.py`（S3，4 项，真实 `TestClient` 走 HTTP）覆盖，
这里不重复。

---

## 4. 改动面

| 文件 | 改动 |
| --- | --- |
| `sandbox/tools.py` | `_sensitive_real_violation` / `_sensitive_subtree_violation`：`WorkspaceUnavailableError` 透传，不落"不可判定"兜底 |
| `tests/test_git_bound_workspace.py` | 新增 3 项 |
| `tests/test_p0_2_integration.py` | 新增 3 项 |

**未改**：`gitops`、敏感路径规则本身、面板。

---

## 5. 验收

| 项 | 结果 |
| --- | --- |
| `tests/test_git_bound_workspace.py` | 3 passed（无 git 时 skip） |
| `tests/test_p0_2_integration.py` | 3 passed |
| 全量 `pytest tests` | **672 passed, 1 skipped** |
| 未绑定 18 条基线 | 逐字节通过 |

---

## 6. P0-2 收尾状态

| 阶段 | 内容 | 状态 |
| --- | --- | --- |
| S1 | 状态机 + 快照接入 + UNBOUND 基线 | 完成 |
| S2 | 文件工具与 Git 落 `local_root` | 完成 |
| S3 | 面板与执行链同源 | 完成 |
| S4 | shell 的 cwd 与路径翻译同源 | 完成 |
| S5 | Git 回归（零代码改动） | 完成 |
| S6 | 组合验收 + 错误语义修正 | 完成 |

**剩余（独立立项）**：

- ~~`P0-SEC-1`：面板直读路径绕过敏感检查与越界核验（只读先行）~~ **已完成**：
  列表 / 预览 / 下载 / 上传统一走 `permissions.paths.check_path`（第一遍：请求路径）
  与 `guard_real_path`（第二遍：解析后的真实路径 + 越界），用例见
  `tests/test_panel_path_guard.py`
- 原生程序不认 MSYS 路径形式（`python /home/user/x.py`），见 S4 文档 §6
- `_run` 启动前取消导致租约残留
- `_background` 跨请求生命周期
