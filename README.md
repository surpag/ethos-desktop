# ETHOS Desktop

ETHOS 的桌面端。Electron 壳 + 本地 Python sidecar + Next.js 界面，**不依赖云端**。

与上游 ETHOS（多租户云服务）是两个独立仓库：本仓库是 2026-11-12 的快照，
基线见 [UPSTREAM.md](./UPSTREAM.md)。上游任何改动都不会自动流到这里，反之亦然。

## 结构

```
ethos-desktop/
├── desktop/        Electron 主进程（新写）
│   ├── src/main.ts       启动编排：单实例锁 → sidecar → Next → 窗口
│   ├── src/process.ts    子进程管理（随机端口 / 日志前缀 / 整树结束）
│   └── src/paths.ts      开发态与打包态的资源定位
├── backend-py/     Python 后端（快照，本地化改造进行中）
├── web-v1/         Next.js 界面（快照，认证已单点化）
└── docs/           设计档案（archive/ 全为已废弃设计稿，勿据此理解代码）
```

## 启动链路

```
electron (desktop/dist/main.js)
  ├─ 单实例锁 ─── 第二个实例不启动服务，只把已有窗口叫回前台
  ├─ 回收        上次异常退出遗留的子进程（pid 存活 + 命令行匹配才 kill 进程树）
  ├─ sidecar    python -m uvicorn edith_backend.main:app --port <随机>
  │             ETHOS_DESKTOP=1, ETHOS_DATA_DIR=%APPDATA%\ETHOS
  ├─ web        electron.exe(ELECTRON_RUN_AS_NODE) .next/standalone/server.js
  │             PORT=<随机>, EDITH_RUNTIME_URL=http://127.0.0.1:<backendPort>
  └─ BrowserWindow.loadURL(http://127.0.0.1:<webPort>) + Tray
```

端口全部随机分配：不和用户自己跑的 `:8080` 打架。

## 关窗与退出

关闭按钮**不是**退出：事件被拦成 `hide()`，应用留在托盘继续跑。Agent 的长任务
不该因为误关窗口就被中断。

恢复入口有三个：托盘菜单「打开 ETHOS」、单击托盘图标、再启动一次（第二个实例
拿不到锁，但它会触发 first-instance 的 `second-instance`，把隐藏的窗口 `show()` 回来）。

真正的退出只有托盘菜单的「退出」，走 `before-quit` → 先停 web 再停 backend。

> 托盘图标读不到时，关窗**退回直接退出**。没有托盘就没有恢复入口，此时隐藏等于
> 把应用藏丢——这种降级宁可少一个功能，也不能让窗口消失在无法唤回的地方。

### 崩溃与残留

子进程台账 `%APPDATA%\ETHOS\runtime\children.json` 只在运行期存在。**文件还在
= 上次没走正常退出**（正常路径下两条记录都被 `stop()` 删掉，文件随之消失）。

启动时逐条校验后才动手：pid 存活 **且** 命令行含签名（`edith_backend.main:app` /
`server.js`）。只认 pid 会误杀——pid 会被系统复用，而用户自己也在跑 Python。
校验不过就跳过，宁可留一个孤儿报出 `database is locked`（那个错误至少指向真实
原因），也不盲杀。

运行中后端或 web 挂掉会弹框说明是哪一个停了，然后退出，不原地重启：重启会让
进行中的 SSE 断开、会话状态丢失，还可能再撞一次库锁。启动期的子进程退出会立刻
中断就绪等待，不用等满 60 秒超时。

## 数据位置

一切可变数据在 `%APPDATA%\ETHOS`：

```
%APPDATA%\ETHOS\
├── edith-v2.db          会话 / 消息 / 计划 / trace
├── skills/              Skill（用户可直接编辑的普通文件）
├── workspace-volumes/   工作区文件（未绑定本地目录时落这里）
├── runtime/             子进程台账（只在运行期存在；文件还在 = 上次异常退出）
└── sandbox/             每会话的沙箱目录（uploads / work / artifacts + 两个联接）
```

由 `backend-py/.../config.py` 的 `data_root()` 单点推导；Electron 也把
`userData` 设成同一个目录，两边不靠额外约定对齐。

## 开发

```bash
# 后端依赖（一次性）
cd backend-py && python -m venv .venv && .venv/Scripts/python.exe -m pip install -e .

# 前端依赖 + 桌面版构建（standalone 产物 + 补齐 static/public，一条命令）
cd web-v1 && npm install
cd ../desktop && npm install && npm run build:web

# 壳（build:web 已生成 web-v1/.next/standalone）
npm start
```

### 环境变量

桌面端**不需要 `.env`**。数据目录、沙箱、认证模式均由 `desktop/src/main.ts`
在 spawn 时注入；模型 API Key 走应用内「设置 → 模型」，写进数据库。
`backend-py/.env.example` 只对「脱离壳单独调试后端」有意义。

单独调试后端时注入同样的上下文，但要**换一个数据目录**——桌面端用的是
非 WAL 的 SQLite（`busy_timeout=5000`），和壳同时指着同一个库必定
`database is locked`：

```bash
cd backend-py
ETHOS_DESKTOP=1 ETHOS_DATA_DIR="$APPDATA/ETHOS-dev" \
  .venv/Scripts/python.exe -m uvicorn edith_backend.main:app --port 8080
```

## Skill（M2）

云端版按 platform / user / project 三层存放，再用解析器裁决「谁覆盖谁」——那是
多租户的必需品。桌面端只有一个用户、一份目录，三层退化成三个子文件夹，裁决链
永远只有参与者一人。所以这里砍到只剩一句话：**一个目录，扫它**。

```text
%APPDATA%\ETHOS\skills\<名字>\SKILL.md     ⇄  /home/user/skills/<名字>/SKILL.md
```

- **加载**：每轮对话扫一遍目录，取 frontmatter 的 `name` + `description` 渲染成清单，
  追加到 System Prompt **末尾**（放前面会让前缀缓存每次失效）。正文不进 prompt。
- **读取**：模型用 `sandbox_read_file` 按需读。同一轮最多 3 个 Skill、累计 60KB，
  超了返回结构化错误而不是抛异常（模型能据此改用片段读）。
- **预算**：清单 4000 字符封顶，按条目截断，被截掉的在文本里写明「还有 N 个未列出」——
  不说的话模型会认为目录里只有这些，得出「那个 Skill 不存在」这种虚构结论。
- **内置**：`current-time`、`skill-creator` 在启动时铺进数据目录，**目标已存在的跳过**。
  用户改过的内置 Skill 不会被升级覆盖回去。

删掉的东西：`resolver.py`（407 行裁决）、`storage/`（约 1800 行：作用域存储、冲突快照、
LWW、事务、校验）、`migrate.py`（527 行迁移工具）、`overview.md` 索引、B2/B3 Skill 同步、
`edith.yaml` 展示元数据。

## 当前进度

| 阶段 | 内容 | 状态 |
|---|---|---|
| M0 | 壳能跑：standalone + sidecar + 数据目录 | 完成 |
| M1 | 本地沙箱：`sandbox/` 换成本机实现，11 个原语 | 完成 |
| M2 | Skill 单层化 | 完成 |
| M2.5 | 删 E2B 后端与工作区同步：`sync.py`、`volume/`、出口白名单 | 完成 |
| M3 | 桌面化：托盘常驻、关窗不退出、崩溃残留回收 | 完成 |
| M3.5 | 凭据加密：`api_key` 目前只是混淆，未接 DPAPI | 未开始 |
| M4 | 分发：隔离、签名、自动更新 | 未开始 |

## 本地沙箱（M1）

`backend-py/src/edith_backend/internal/sandbox/local.py` 是工具层文件 / 命令原语的
**唯一**实现（`.files` / `.commands` / `.sandbox_id`）。没有"后端"这个概念：
远端沙箱连同它的工作区同步、Volume 生命周期、出口白名单一起删掉了——留在代码里
就是一条看起来能走、实际走不通的路。

虚拟路径映射（`LocalSandbox.resolve`）：

| 模型看到的路径 | 实际落点 |
|---|---|
| `/home/user/work/a.txt` | `%APPDATA%\ETHOS\sandbox\{user}\{session}\work\a.txt` |
| `/home/user/workspace/{id}/…` | `%APPDATA%\ETHOS\workspace-volumes\{id}\…`（**零拷贝**） |
| `/home/user/skills/…` | `%APPDATA%\ETHOS\skills\…`（**零拷贝**） |

后两行是这份代码最重要的前提：**磁盘上只有一份文件**。文件原语做路径翻译，
`run_command` 靠目录联接看到同一个位置，两边读写的是同一片字节。

于是「把副本同步过去」这件事没有存在前提——原实现那 842 行的推送 / 回拉 / 三道护栏 /
熔断 / 删除传播，处理的是"沙箱在远端、权威副本在本地"的两份副本问题。这里没有第二份。

### 四件"看起来能用、实际不能用"的事

文件原语的路径映射只解决了工具层；`run_command` 是**真实进程**，它只认文件系统。
以下四条都必须补，少一条模型就开始反复试错（首轮实测：3 个任务用了 21 次工具调用）：

| 缺口 | 症状 | 做法 |
|---|---|---|
| `workspace/{id}` 在 shell 里是空目录 | 刚写完 `ls` 不到，模型怀疑自己，开始用 `cmd.exe` / `python -c` 变着法子找文件 | 把 `{root}/workspace` 做成指向 `workspace-volumes/` 的**目录联接**，`workspace/{id}` 因此是普通目录（`find`/`grep -r` 也会进去） |
| `/home/user` 在 Windows 上不存在 | `ls /home/user/...` → No such file | 命令文本里把 `/home/user` 换成真实根（`_translate`），**唯一**一处翻译点 |
| 输出里是宿主真实路径 | 模型拿它再调文件工具，被 `resolve()` 判越界 | 输出侧反向归一化回 `/home/user`（`_normalize`），与 `_translate` 对称 |
| 中文 Windows 输出 GBK | `ϵͳ�Ҳ���ָ����·����`，模型当成"语法错了"换语法重试 | 先严格 UTF-8、再按本地代码页解码（`_decode_output`） |

另外两件事：命令统一走 **Git for Windows 的 bash**（模型的命令是按 POSIX 写的：
`mkdir -p`、管道、`sed`、`find`），PATH 由 `_shell_path()` 显式拼装——不能依赖
"应用从哪启动"，双击启动时 PATH 里没有 Git 的 `usr\bin`。

安全边界（诚实说明）：文件原语拒绝 `..` 与符号链接逃逸，且不允许穿越或删除
`workspace` 挂载点。但 `commands.run` 执行的是**本机真实进程**，没有 OS 级隔离——
本地后端的适用前提是「用户在自己的机器上跑自己的 Agent」。

### 验收

`scripts/e2e_check.py` 跑两轮同一个会话（真实模型 + 真实工具）：

```
第一轮  写文件 → shell 确认 → 读回     3 次工具调用 / 0 失败 / 57s / 证据 mutated
第二轮  不重建，只确认文件还在          1 次工具调用 / 0 失败 / 证据 read_only
```

第二轮是关键：它证明的是**跨 run 持久化**——新 run 新 request，文件仍在那份权威副本里。

## 与上游的关键差异

**认证**：`web-v1/lib/auth/current-user.ts` 是唯一入口，固定返回 `local-user`。
Clerk（含 provider、middleware、登录页、账号菜单）与 `ETHOS_DESKTOP` /
`NEXT_PUBLIC_ETHOS_DESKTOP` 两个构建期开关都已删除——桌面端没有第二条路径要兼容，
留着只会让「服务端认为在桌面、客户端认为在云端」这种白屏故障有生存空间。

**数据目录**：`config.data_root()` 按 `ETHOS_DESKTOP` 分流。非桌面模式下
`data_root() == project_root()`，脱离壳单独调试时行为与上游一致。

**图片**：从腾讯云 COS（预签名直传 + 签名 URL）改成本机磁盘
（`data_root()/images`）+ 喂模型时内联 data URL。桌面端没有 bucket，
远端模型也拉不到 `127.0.0.1` 的地址。

**渠道**：`gateway` 只放行 `web` / `cron`，未知渠道直接 `invalid_request`。
上游那套「外部渠道账号 → 用户」的绑定表已随多租户一起删除。
