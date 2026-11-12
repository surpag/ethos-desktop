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
└── docs/           上游设计文档
```

## 启动链路

```
electron (desktop/dist/main.js)
  ├─ 单实例锁 ─── 第二个实例直接退出（SQLite 非 WAL，双进程必锁）
  ├─ sidecar    python -m uvicorn edith_backend.main:app --port <随机>
  │             ETHOS_DESKTOP=1, ETHOS_DATA_DIR=%APPDATA%\ETHOS
  ├─ web        electron.exe(ELECTRON_RUN_AS_NODE) .next/standalone/server.js
  │             PORT=<随机>, EDITH_RUNTIME_URL=http://127.0.0.1:<backendPort>
  └─ BrowserWindow.loadURL(http://127.0.0.1:<webPort>)
```

端口全部随机分配：不和用户自己跑的 `:8080` 打架。

## 数据位置

一切可变数据在 `%APPDATA%\ETHOS`：

```
%APPDATA%\ETHOS\
├── edith-v2.db          会话 / 消息 / 计划 / trace
├── workspace-volumes/   工作区文件权威副本
├── user-skills/         用户级 Skill
├── project-skills/      项目级 Skill
└── workspace-sync/      同步状态（本地沙箱落地后应消失）
```

由 `backend-py/.../config.py` 的 `data_root()` 单点推导；Electron 也把
`userData` 设成同一个目录，两边不靠额外约定对齐。

## 开发

```bash
# 后端依赖（一次性）
cd backend-py && python -m venv .venv && .venv/Scripts/python.exe -m pip install -e .

# 前端依赖 + 桌面版构建（NEXT_PUBLIC_ETHOS_DESKTOP 必须在 build 时存在，
# 它是编译期内联的，运行期再设无效）
cd web-v1 && npm install
NEXT_PUBLIC_ETHOS_DESKTOP=1 npm run build
cp -r .next/static .next/standalone/.next/static
cp -r public .next/standalone/public

# 壳
cd desktop && npm install && npm start
```

### 环境变量

桌面端**不需要 `.env`**。数据目录、沙箱后端、认证模式均由 `desktop/src/main.ts`
在 spawn 时注入；模型 API Key 走应用内「设置 → 模型」，写进数据库。
`backend-py/.env.example` 只对云端部署和脱离壳调试后端有意义。

单独调试后端时注入同样的上下文，但要**换一个数据目录**——桌面端用的是
非 WAL 的 SQLite（`busy_timeout=5000`），和壳同时指着同一个库必定
`database is locked`：

```bash
cd backend-py
ETHOS_DESKTOP=1 ETHOS_DATA_DIR="$APPDATA/ETHOS-dev" \
  .venv/Scripts/python.exe -m uvicorn edith_backend.main:app --port 8080
```

## 当前进度

| 阶段 | 内容 | 状态 |
|---|---|---|
| M0 | 壳能跑：standalone + sidecar + 数据目录 | 完成 |
| M1 | 本地沙箱：`sandbox/` 换成本机实现，11 个原语 | 完成 |
| M2 | 删同步：`workspace/sync.py` 在本地后端下 no-op | 未开始 |
| M3 | 桌面化：keychain、托盘、退出清理 | 未开始 |
| M4 | 分发：隔离、签名、自动更新 | 未开始 |

## 本地沙箱（M1）

`backend-py/src/edith_backend/internal/sandbox/local.py` 提供与 E2B `AsyncSandbox`
**形状兼容**的 `LocalSandbox`（`.files` / `.commands` / `.sandbox_id`）。所以
`tools.py` 那一千行工具代码一行都没改——后端分歧只存在于 `Service.workspace()`
的一个 `if` 上。

虚拟路径映射（`LocalSandbox.resolve`）：

| 模型看到的路径 | 实际落点 |
|---|---|
| `/home/user/work/a.txt` | `%APPDATA%\ETHOS\sandbox\{user}\{session}\work\a.txt` |
| `/home/user/workspace/{id}/…` | `%APPDATA%\ETHOS\workspace-volumes\{id}\…`（**零拷贝，不同步**） |

第二行是本地模式最重要的简化：E2B 后端里沙箱与权威副本是两份，才需要
`workspace/sync.py` 那 842 行的推送/回拉/护栏/熔断；本地模式下它们本来就是
同一个目录，同步问题从根上不存在。

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

**认证**：`web-v1/lib/auth/current-user.ts` 是唯一入口。桌面模式固定返回
`local-user`，云端仍走 Clerk。38 个 BFF route 不再直接接触 Clerk。

**数据目录**：`config.data_root()` 按 `ETHOS_DESKTOP` 分流。非桌面模式下
`data_root() == project_root()`，与上游行为完全一致。
