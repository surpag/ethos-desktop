---
name: skill-creator
description: 创建或更新 Skill，把可复用的工作方法沉淀成 SKILL.md。当用户要求新增 Skill、修改已有 Skill、或为某类重复任务固化流程时使用。
---

# Skill 创建指南

Skill 描述 Agent **如何完成一类任务**（规则、判断流程、边界）；Tool 是具体执行能力。

## 存放位置

只有一层：

```text
skills/<skill-name>/SKILL.md
```

`skills/` 与宿主上的数据目录是**同一个目录**（联接，不是副本）。写进去立刻落地，
下一轮对话就会出现在指令的 Skill 清单里——没有索引文件要维护，没有同步动作，
也不存在「用户级 / 项目级」的区分。

`skills/<name>/` 是平台目录，与用户项目里的 `workspace/<id>/skills/` 是两个命名空间。
写进后者不会报错，但永远不会被加载。

## 目录结构

最小 Skill 只有一个文件：

```text
<skill-name>/
├─ SKILL.md          必须：摘要 + 完整规则
├─ references/       可选：按需读取的详细资料
├─ scripts/          可选：需要稳定执行的脚本
└─ assets/           可选：模板、图片等资源
```

`SKILL.md` 必须以 YAML 头部开始，只保留两个字段：

```markdown
---
name: daily-summary
description: 帮助用户创建每日总结任务；当用户提到日报、每日总结或定时提醒时使用。
---

# 每日总结

这里写执行这类任务时必须遵守的规则和步骤。
```

- `name`：小写字母、数字、连字符；建议与目录名一致（不一致时模型按目录名找文件）。
- `description`：一句话说清**能力 + 触发场景**。模型只凭它决定要不要读正文，
  写「一个有用的技能」等于让它永远不被选中。

## 编写原则

1. **只写模型不知道的**：删掉常识和空泛背景，保留规则、边界、判断条件。
2. **先写稳定流程**：必须遵守的步骤写清楚；可变化的部分留出选择空间。
3. **保持短小**：主流程放 `SKILL.md`，详细资料放 `references/`，脚本放 `scripts/`。
4. **渐进加载**：正文只在任务需要时才被读取，不要指望模型预先通读。
5. **不要堆无关文件**：不要创建 README、变更日志、安装说明。

## 创建流程

```text
确定任务边界和触发场景
      ↓
确定 name、description、目录名
      ↓
检查 skills/<name>/ 是否已存在
      ↓
建目录 → 写 SKILL.md → 按需加 references/scripts
      ↓
读回文件确认 YAML 头部与正文完整
      ↓
向用户报告（下一轮对话自动生效）
```

用文件工具完成，不要用命令拼接内容：

1. `sandbox_make_directory` 建 `skills/<name>`
2. `sandbox_write_file` 写 `skills/<name>/SKILL.md`
3. `sandbox_read_file` 读回来检查
4. `sandbox_list_files` 确认目录里只有需要的文件

## 可复用脚本

```text
skills/skill-creator/scripts/init_skill.py      建骨架
skills/skill-creator/scripts/quick_validate.py  自查格式
```

`init_skill.py --path` 用**真实绝对路径**（沙箱里是 `/home/user/skills`），
默认值已经是这个目录，可以省略。它只检查目标目录是否存在（已存在直接失败，
不会静默合并），不校验内容——结构对不对仍要自己看。

`quick_validate.py` 校验 frontmatter 与目录结构。两个脚本都只用标准库。

## 更新已有 Skill

- 先读现有 `SKILL.md`，理解原有触发条件和规则。
- 只改用户明确要求的那一条；保留稳定的 `name`，除非用户要求改名。
- 修改后读回完整文件，确认摘要仍然准确、正文没有互相矛盾的规则。
- 内置 Skill（如 `current-time`、`skill-creator` 自身）同样可以直接改——
  它们只是被铺进 `skills/` 的普通文件，没有只读层。

## 运行时边界

```text
清单（name + description）──► 每轮注入 System Prompt 末尾
完整正文 ────────────────► 模型按需用 sandbox_read_file 读
```

同一轮对话最多读 3 个 Skill 的正文、累计 60KB；超出会被拒绝。所以正文写长没有收益，
该拆到 `references/` 里。
