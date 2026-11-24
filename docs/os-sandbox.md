# OS 级执行隔离（第一阶段）

本文记录 ETHOS Desktop 目前**实际落地**的执行隔离：装了什么、拒绝什么、还缺什么。
与 spec 逐条对应之处会标注编号；没做到的一律写进「已知缺口」，不假设已防护。

## 1. 现状一句话

所有 Agent 发起的命令（Shell、后台进程、Git、需要执行本地程序的工具）都必须经过
`internal/isolation` 生成执行计划。**没有可用后端时命令不会启动**，直接以
`tool_failure(retryable=false)` 返回，错误信息里带安装指引。

本机（未安装 ASRT）当前状态可通过接口查询：

```
GET /internal/sandbox/health      # 走正常鉴权
{
  "requested": "auto", "backend": "", "available": false,
  "reason": "not_installed", "message": "没有可用的隔离后端…"
}
```

## 2. 链路

```
sandbox_run_command / git / start_process
        │
        ▼
LocalCommands.run()                     ← 保留：路径翻译 + asyncio 子进程
        │
        ▼
isolation.plan()                        ← 唯一入口，生成 argv + 裁剪后的 env
        ├─ resolve()  后端选择（缓存；不可用则抛 SandboxUnavailable）
        ├─ build_policy()  按工作区生成不可变策略
        ├─ audit.record_start()  审计（脱敏、只记键名）
        └─ backends.asrt → srt --settings <file> -- <shell> -c <cmd>
```

Agent 无法影响策略：策略全部来自本次执行的工作区快照、进程环境与模块常量，
不从工具参数读取。

**其它执行入口**：`gitops/push.py` 的 `git init/fetch/push/ls-remote` 也走同一个
`isolation.plan`（`argv=["git", ...]`，不经 shell）。凭据只经 `GIT_CONFIG_*`
环境变量注入，`plan` 只把键名写进审计；网络只放行远端仓库域名。后端不可用时
`push` 抛 `PushError("命令未执行：…")`，不会回落宿主。

**审计**：每次执行/被拒绝都落一条 `sandbox_audit`（SQLite），字段含
request_id / session / workspace / tool_call_id / backend + version / 策略版本与
指纹 / 工作区根 / 退出码 / 耗时 / outcome / 丢弃与注入的环境变量**键名**。
查询：`GET /internal/sandbox/audit?sessionId=…&requestId=…&limit=50`。

## 3. 后端选择（fail-closed）

| `ETHOS_SANDBOX_BACKEND` | 行为 |
| --- | --- |
| `auto`（默认） | ASRT → AppContainer → （仅测试）unsafe-host → 否则**拒绝** |
| `asrt` | 必须可用，否则拒绝 |
| `appcontainer` | 必须可用，否则拒绝（尚未实现，等于拒绝） |
| `host` | 必须同时设置 `ETHOS_SANDBOX_ALLOW_UNSAFE_HOST=1`，并打 warning 审计 |
| `off` | 一律拒绝执行（**不是**裸跑） |

`off` 的语义是「关掉执行能力」而不是「关掉沙箱」——没有任何配置组合能让命令在
无隔离的情况下运行，唯一例外是名字里带 `ALLOW_UNSAFE` 的那个测试开关。

## 4. 策略内容

- 可写：绑定工作区根；未绑定时为 `<data_root>/workspace-volumes/{当前 id}`（不是
  整个 volumes 目录，避免跨工作区互写）；沙箱树与 `.isolation-tmp`。
- 拒读：`~/.ssh`、`~/.aws`、`~/.azure`、`~/.gnupg`、`~/.config/gcloud`、`~/.kube`、
  `~/.docker`、`.netrc`、浏览器 User Data、`data_root()`（应用私有数据）。
- 拒写：`.env*`、`id_rsa`、`id_ed25519`、`*.pem`、`*.key`、`*.p12`、`*.pfx`。
- 网络：默认 `allowedDomains = []`（全禁）；白名单来自
  `ETHOS_SANDBOX_ALLOWED_DOMAINS`；本机网络默认不放行。
- 策略文件写在 `<data_root>/isolation/policies/<digest>.json`，**不在工作区内**，
  避免被沙箱进程改写自己的规则。
- 环境变量按白名单传递（`PATH` / `SystemRoot` / `TEMP` 等），任何名字含
  `TOKEN|SECRET|PASSWORD|API_KEY|PROXY` 或 `AWS_*`/`OPENAI_*`/`EDITH_*` 的一律丢弃；
  `TEMP`/`TMP` 重定向到工作区内的私有目录。

## 5. 安装（Windows，ASRT）

```powershell
npm i -g @anthropic-ai/sandbox-runtime      # 需要 Node >= 22.12
npx @anthropic-ai/sandbox-runtime windows-install   # UAC 一次
```

`windows-install` 会创建本地账户 `srt-sandbox`、写 `HKLM\SOFTWARE\sandbox-runtime`、
安装机器级 WFP 出站过滤器，并用 DPAPI 存该账户密码。**升级后必须重跑 install**，
降级前必须先 `windows-uninstall`，否则旧过滤器会放行越权写入。

装完重启应用（后端启动时缓存探测结果；`GET /internal/sandbox/health` 可确认）。

卸载：

```powershell
npx @anthropic-ai/sandbox-runtime windows-uninstall
```

## 6. 已知缺口（未完成，不要假设已防护）

1. **AppContainer 后端未实现**：本机 ASRT 不可用时，Windows 上没有任何可用后端，
   命令一律被拒绝。这是刻意的 fail-closed，不是 bug。
2. **ASRT Windows 为 Alpha**：schannel 类工具（PowerShell `Invoke-WebRequest`、
   `curl.exe`、.NET `HttpClient`、`dotnet restore`、`winget`、schannel 后端的 git）
   在沙箱内不可用。
3. **域名白名单依赖代理层**：非 HTTP(S)/SOCKS 感知的程序可能被阻断或绕过；
   IP 直连与内网地址的判定来自 ASRT，不在本仓库。
4. **文件读写工具仍在宿主进程执行**（应用层路径校验 + P0-2 的真实路径核验），
   尚未被 OS 权限约束——`sandbox/file` 系列工具不在沙箱内运行。
5. **验收测试覆盖 8 条，本机 8/8 通过（约 38 秒）**：沙箱内写读、越界写、
   应用私有数据、路径穿越、未授权域名、localhost、后台进程回收、`git --version`。
   仍缺一条：**工作区（`workspace-volumes/`）内的写读**——没有 workspace id 时
   `/home/user/workspace` 翻译到 volumes 根，那不是任何会话的可写根（策略只放行
   `workspace-volumes/{id}`），所以该场景目前没有断言。
6. **家目录敏感路径的拒绝不走 ASRT**：`denyRead` 只保留 `data_root()`；`.ssh` /
   `.aws` / 浏览器 User Data 等改由 `isolation/win_acl.py` 用 `icacls` 直接加
   DENY。原因是 ASRT 那条路在本机会空转，见第 8 节。

## 7. 故障排查

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| 所有命令返回「没有可用的隔离后端」 | 未装 ASRT / Node < 22.12 / `srt` 不在 PATH | 按第 5 节安装；`npm prefix -g` 下的 `srt.cmd` 也会被探测 |
| 提示「未找到可用的 shell」 | Windows 缺 Git for Windows 的 bash | 安装 Git for Windows |
| srt 退出码非零且 stderr 提到 initialize | 未执行 `windows-install`，或升级后未重跑 | 重跑 install |
| 命令里 `--debug` 之类参数「消失」 | argv 缺少 `--` 分隔符 | 已修复，见 `backends/asrt.py::build_argv` |
| 所有命令稳定卡 25~90 秒，卡在初始化 | srt-win 的 ACL 状态坏了：持久授权记录指向**已删除**的目录，或进程被强杀留下 dead holder，recovery 阶段 CPU 空转（退不出来） | 管理员执行 `<node 全局目录>\@anthropic-ai\sandbox-runtime\vendor\srt-win\x64\srt-win.exe acl forget --all --force`。单个 root 用 `acl forget <PATH>`，不需要提权（0.1s） |
| 沙箱内能读到 `~/.ssh` 私钥 | 家目录敏感路径的拒绝被交给 ASRT，而 `srt-win acl stamp` 对「parent 是用户主目录」的目标会空转 | 已改为 `win_acl.ensure_deny()` 用 `icacls` 只给目标目录加 DENY；加固失败会拒绝执行（fail-closed） |
| 想临时排查命令本身的语义 | — | **不要**打开 unsafe-host；用 `ETHOS_SANDBOX_BACKEND=off` 验证拒绝路径 |

## 8. 真机验收记录（Windows 10 19045，2026-11-23）

安装：`npm i -g @anthropic-ai/sandbox-runtime` → `srt --version` = 0.0.83，
Node 22.20.0，`srt-sandbox` 账户已存在（setup_version 3，seclogon ok）。当前会话
**非管理员**。

结论：**可用**。`isolation.health()` = `available=true`（4~8 秒），
`tests/test_sandbox_acceptance.py` **8/8 通过（38 秒）**；沙箱内 `bash` / `git` /
`ls` / `cat` / 重定向写文件 / 后台进程回收均正常。

本文档此前记录过「`available=true` 是假阳性，实际不可用」，那几条结论**都是
误判**。真因如下（均已修复）：

| 曾经的误判 | 真因 | 修法 |
| --- | --- | --- |
| 沙箱内 `bash.exe` 起不来，报 `The filename, directory name, or volume label syntax is incorrect`，被当成权限问题 | ASRT CLI 在 Windows 上仍用 POSIX `quote()` 拼命令串（`dist/utils/shell-quote.js`），再交给 `cmd /d /s /c`；含空格和反斜杠的参数被单引号包住，cmd 不认单引号 | Windows 改走 `srt -c <cmdline>`，见 `backends/asrt.py::_windows_argv` |
| 给工具链加 `allowRead` 后 `srt-win acl grant` 报 `WIN32_ERROR=0x5`，整批回滚 | `allowRead` 每一项都要改目标目录 DACL，普通用户对 `Program Files` / `System32` 没有 WRITE_DAC | 工具链不进 `allowRead`：沙箱用户本就在 `BUILTIN\Users`，默认可读可执行 |
| 命令挂 90 秒，日志停在 `acl grant` / `stamp` | `filesystem.denyRead` 里有落在用户主目录下的路径，`srt-win acl stamp` 给「parent 是用户主目录」的目标加 `FILE_DELETE_CHILD` DENY 时会 CPU 空转 | 这些路径改由 `win_acl.ensure_deny()` 用 `icacls` 只给目标目录加 DENY，`denyRead` 只留 `data_root()` |

`stamp` 空转的实测边界（同一台机器、同一条命令）：

| 目标 | parent | 耗时 |
| --- | --- | --- |
| `~/.ssh` | 用户主目录 | 超过 20s 超时 |
| `~/.srtprobe`（主目录下新建空目录） | 用户主目录 | 超过 12s 超时 |
| `~/.srtprobe/sub` | `~/.srtprobe` | 0.1s |
| `data_root()`（D 盘普通目录） | 普通目录 | 0.3s |

另外这台机器的 `C:\Users\<user>` 上有 `Everyone:(OI)(CI)(F)`，所以 `.ssh` 对
沙箱账户是可读的（实测能读出私钥头）——家目录敏感路径的 DENY 是**必需**的，
不能因为「默认读不到」而省掉。

**运维提醒**：srt 的持久写授权（`acl grant --persistent`）在进程被强杀后不会
回收；记录一旦指向被删除的目录，之后**每次** `acl grant` 的 recovery 都会空转。
所以不要直接删除被授权过的目录；验收测试的 teardown 现在会 `acl forget` 掉自己
创建的三处 root（单 root 不需要提权）。
