# docs

这个目录**不放「当前实现说明」**。

当前实现的权威文档是各模块的 `README.md`：

- 组合根与装配顺序：`backend-py/src/edith_backend/README.md`
- 各业务模块：`backend-py/src/edith_backend/internal/<模块>/README.md`
- 怎么跑起来：`backend-py/README.md`、`web-v1/README.md`、`desktop/`
- 与上游的关系：`UPSTREAM.md`

放在这里的只有两类东西：

| 类别 | 位置 | 判据 |
| --- | --- | --- |
| **仍在生效的方案与裁决记录** | 本目录根下 | 它回答的是「为什么这样定」，代码本身回答不了；结论仍然适用 |
| **已废弃的设计稿** | `archive/` | 前提已经不成立，只能当历史读 |

| 文档 | 主题 |
| --- | --- |
| `permissions-p1.md` | 权限系统 P1：能力声明、档位裁决、敏感路径、副作用入口覆盖矩阵；§15 含 P2 的按条目判定 |
| `permissions-p2-approval.md` | 权限系统 P2-2/P2-3：审批挂起/恢复的终止路径枚举、为什么是原地等待、授权范围与风险；§14 含前端审批面板与档位开关 |
| `permissions-p3-scopes.md` | 权限系统 P3：会话级/工作区级档位**上限**（只能收紧）、四来源解析链、问题空间枚举与存储/HTTP/前端接线 |

`archive/` 存的是 2026-11-12 之前的设计稿。它们大多描述的是「云端多租户 Agent
平台」这条路线，而桌面版走的是相反方向：单用户、本机执行、无远端沙箱、无对象
存储。每一篇顶部都写了**它为什么失效**以及**现在该看哪**，读之前先读那一段。

保留它们的理由只有一个：知道「当时为什么那样设计」能避免把踩过的坑再踩一遍
——比如双副本同步的熔断设计、Skill 三层裁决的取舍，都是在这个项目里真实付过
代价的结论。但它们**不能**用来理解当前代码。

## 归档索引

| 文档 | 主题 | 为什么归档 |
| --- | --- | --- |
| `cloud-multitenant-migration.md` | 多租户改造方案 | 方向已放弃（改为单用户桌面） |
| `e2b-volume-beta-request.md` | E2B Volumes 申请 | 集成已删除，E2B 依赖已移除 |
| `local-sandbox-sync-design.md` | 本地 ↔ 沙箱双向同步 | 同步已删除（改目录联接） |
| `sandbox-sync-optimization.md` | 同步按需化 / 降频 | 同上，无同步可优化 |
| `sandbox-runtime-abstraction.md` | 多沙箱后端抽象 | 只剩本机实现，不留抽象 |
| `skill-scope-design.md` | Skill 三层作用域与裁决 | 已单层化 |
| `skills系统设计.md` | Skill 系统总设计 | 同上 |
| `p1-workspace-volume-design.md` | 工作区远端持久卷 | 改为本机目录 + 联接 |
| `multi-app-deployment.md` | 单机多应用部署 | 桌面版不对外服务 |
| `mobile-adaptation-plan.md` | 移动端适配 | 前提不成立 |
| `系统结构原理图.md` | 架构图（Clerk/E2B/COS） | 依赖已全部移除 |
| `project-walkthrough.md` | 全景导读 | 按旧架构组织 |
| `git-workspace-design.md` | Git 工作区设计 | 凭据结论保留，同步部分失效 |
| `improvement-backlog-v2.md` | 优化 backlog V2 | 条目针对云端形态 |
| `known-issues-and-improvements.md` | 已知问题 V1 | 同上 |
| `project-retrospective.md` | 阶段复盘 | 复盘的是另一条路线 |
| `session-summary.md` | 摘要压缩设计稿 | 已实现，口径不同 |
| `p0-project-memory-design.md` | 项目级记忆设计稿 | 已落地 |
| `p0-phase-a-design.md` | 指令文档化设计稿 | 已落地 |
| `degradation-plan.md` | 降级与容错 | 远端组件已删 |
| `context-engineering.md` | 上下文工程规划 | 现状基线已过期 |
| `observability.md` | 可观测性设计稿 | 部分已落地，方案不同 |
| `trace-eval-design.md` | 追踪与评测设计稿 | 已落地，字段不同 |
| `tool-error-handling.md` | 工具错误语义与熔断 | 起因（E2B 签名漂移）已不存在 |
| `tool-call-pipeline.md` | 工具调用全链路 | 行号与模块名已过期 |
| `memory-comparison.md` | 与 wuu 的记忆对比 | 对比结论仍可用，ETHOS 现状过期 |
| `定时任务如何设计.md` | 定时任务设计 | 主结论成立，IM 分支已删 |
