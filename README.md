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

## 当前进度

| 阶段 | 内容 | 状态 |
|---|---|---|
| M0 | 壳能跑：standalone + sidecar + 数据目录 | 进行中 |
| M1 | 本地沙箱：`sandbox/` 换成本机实现，13 个原语 | 未开始 |
| M2 | 删同步：`workspace/sync.py` 在本地后端下 no-op | 未开始 |
| M3 | 桌面化：keychain、托盘、退出清理 | 未开始 |
| M4 | 分发：隔离、签名、自动更新 | 未开始 |

## 与上游的关键差异

**认证**：`web-v1/lib/auth/current-user.ts` 是唯一入口。桌面模式固定返回
`local-user`，云端仍走 Clerk。38 个 BFF route 不再直接接触 Clerk。

**数据目录**：`config.data_root()` 按 `ETHOS_DESKTOP` 分流。非桌面模式下
`data_root() == project_root()`，与上游行为完全一致。
