# skills（单层目录、清单注入与加载预算）

## 第一部分：模块名称与简介

**模块名**：`skills`

**一句话定位**：扫描 `<data_root>/skills/<目录>/SKILL.md`，把「有哪些 Skill、在
哪、什么时候该用」渲染成 system prompt 末尾的一段清单；模型按需去沙箱里读正文。
同时提供只读 HTTP 接口给浏览器展示列表。

**设计只有一句话：一个目录，扫它。** 云端那套 platform / user / project 三层
作用域、优先级裁决、冲突快照、跨副本迁移在这里全部不存在——它们存在的前提是
「同一份东西有多份副本、且谁覆盖谁必须可解释」。桌面端只有一个用户、一份目录，
裁决链永远只有参与者一人。

## 文件清单

| 文件 | 职责 |
| --- | --- |
| `catalog.py` | 目录扫描（`scan_skills`）、单个 Skill 解析（`load_skill`）、内置 Skill 铺设（`seed_builtin`）、`Catalog` 门面 |
| `instruction.py` | `render_skill_instruction`：把清单渲染成注入 system prompt 的一段 |
| `budget.py` | 两级预算：清单字符预算（`plan_index`）+ 单 run 加载预算（`SkillLoadBudget`，ContextVar 账本） |
| `http.py` | 只读列表接口 `/internal/skills` |
| `types.py` | `Skill` / `SkillListItem` / `SkillsResponse` 与 `SKILLS_PATH` / `SKILL_FILE` |
| `system/` | 随包发布的内置 Skill 源目录（只读） |

## 第二部分：架构设计

### ① 三层加载（渐进式披露）

| 层 | 时机 | 载体 | 预算 |
| --- | --- | --- | --- |
| L1 清单 | 进程启动 + 每轮 run | `frontmatter` 的 `name` / `description` | `SKILL_INDEX_BUDGET_CHARS`（默认 4000 字符，按条目截断） |
| L2 注入 | 每轮 run | `render_skill_instruction` 追加到 system prompt **末尾** | 与 L1 同一份清单 |
| L3 正文 | 模型主动调用 | `sandbox_read_file` 读 `skills/<目录>/SKILL.md`（可分页） | `SkillLoadBudget`：3 个 / 60KB（48KB 预警） |

**正文不进 prompt**：`Skill.body` 会被读入对象但只在 L3 需要时用；清单里只有
name / description / 路径。这样 Skill 数量增长不会线性吃掉上下文。

**为什么注入在末尾**：system prompt 开头改一个字就毁掉前缀缓存（实测 0% vs
98%）。清单每轮都可能变（用户新增 Skill），只能放在末尾。

### ② 预算的两套口径

- **清单预算按「条目」为单位截断**，确定性、跨 run 稳定；溢出时在指令里写
  「已省略 N 个」，让模型知道清单不完整，而不是以为「只有这些」。
- **加载预算数量按「不同 Skill 数」、体积按「累计返回字节」**：
  - 只数调用次数 → 分页读一个 100KB 的 SKILL.md 会被当成 5 个 Skill 而误杀；
  - 只数文件大小 → 反复读同一段就能绕过预算；
  - 计量口径是**实际返回给模型的字节**，不是文件大小。
- 账本挂在 `ContextVar`（作用域 = run 的 task，随 task 消失，不需回收）。
  **被拒的读取不记账**：那次没有返回正文，记进去会让模型在「被拒 → 重试 → 被拒」
  里把预算越撞越小。

### ③ 内置 Skill 的铺设

`seed_builtin(dest)` 只**铺不覆盖**：目标目录已存在就跳过。用户改过内置 Skill 的
内容，不该被一次升级抹掉。

## 第三部分：与其他模块的联系

| 依赖方 | 用途 |
| --- | --- |
| `agentrun` | 每轮 run 调 `render_skill_instruction` 注入清单；工具侧用 `charge_skill_load` 记账 |
| `sandbox` | `skills/` junction 到 `<data_root>/skills`，模型读正文走 `sandbox_read_file` |
| 组合根 | `seed_builtin(data_root()/"skills")` + `Catalog(skills_root)` + `SkillsHTTP(skills)`（其它模块 import 时把它别名为 `SkillsCatalog`） |

## 第四部分：已知约束

- **内置 Skill 正文是随包资源**：改了 `system/*/SKILL.md` 需要重新发版；已存在的
  用户目录不会被覆盖（刻意的）。
- **没有热重载**：清单在每轮 run 重新扫描（按需），但 `Catalog` 实例本身不监听
  文件变化；扫描成本是一次 `iterdir`，足够便宜。
- **不校验正文安全性**：Skill 正文就是 prompt 内容，能写进什么取决于来源。内置
  的是随包发布的；用户自己放进目录的，等同于用户自己写的指令。
