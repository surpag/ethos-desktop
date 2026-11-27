"""按工作区生成隔离策略（一次执行一份，不可变）。

**这份策略只能由执行链生成，Agent 无法修改其中任何一项**：它不从工具参数里读
任何值——工作区根来自本次执行的工作区快照，域名白名单来自进程配置，敏感目录来自
本模块的常量。模型能做的只有"在这个笼子里跑什么命令"，不是"笼子有多大"。

`POLICY_VERSION` 会进审计记录：策略语义一变就升版本，事后才能判断某条历史执行
当时用的是哪套规则。
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

from ...config import data_root

#: 策略版本。改任何一项语义（新增拒绝目录、改默认网络姿态）都要升。
#: v2：`denyRead` 不再包含整个 `data_root()`（桌面模式下那是 `%APPDATA%\ETHOS`），
#: 改为只拒绝数据库文件——见 `denied_read_paths()` 里的实测数据。
#: v3：`allowRead` 显式放行虚拟映射根（`workspace-volumes` / `skills`）。v2 收紧
#: `denyRead` 后 srt 开始严格按"不在允许列表即拒绝"，`cat workspace/foo.txt`
#: 直接失败——v1 时这个缺陷被 `denyRead` 的宽松副作用盖住了。
POLICY_VERSION = "ethos-sandbox-policy/3"

#: 相对用户主目录的敏感目录：**读也不行**。SSH 私钥、云凭据、浏览器配置、包管理器
#: 令牌都在这里；沙箱进程拿到它们等于拿到用户身份。
_HOME_DENIED = (
    ".ssh",
    ".aws",
    ".azure",
    ".gnupg",
    ".config/gcloud",
    ".kube",
    ".docker",
    ".netrc",
    "_netrc",
)

#: 浏览器数据目录（相对主目录 / 相对 AppData）。
_BROWSER_DENIED = (
    "AppData/Local/Google/Chrome/User Data",
    "AppData/Local/Microsoft/Edge/User Data",
    "AppData/Roaming/Mozilla/Firefox/Profiles",
    "AppData/Local/BraveSoftware",
)

#: 即使在可写工作区里也不允许写入的文件名（写进去等于凭据外送或自毁）
_DENIED_WRITE_NAMES = (
    ".env",
    ".env.local",
    ".env.production",
    "id_rsa",
    "id_ed25519",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
)


def _home() -> Path | None:
    raw = os.environ.get("USERPROFILE") or os.environ.get("HOME")
    return Path(raw) if raw else None


READ_ONLY_ROOTS_WARNING = """\
工具链目录**不能**写进 `allowRead`，这条踩过坑，别再犯：

沙箱用户 `srt-sandbox` 本来就在 `BUILTIN\\Users` 里，`C:\\Windows\\System32`、
`C:\\Program Files\\Git` 的默认 ACL 已经允许它读/执行，不需要任何显式授权
（实测：`allowRead: []` 时 Git bash 在沙箱内正常运行）。

反过来，把它们写进 `allowRead` 会让**整个 srt 启动失败**：`allowRead` 的每一项
都要 `srt-win acl grant`，即对目标目录调 `SetNamedSecurityInfoW` 改 DACL；普通
用户对 Program Files / System32 没有 WRITE_DAC，返回
`WIN32_ERROR=0x00000005`（ACCESS_DENIED），`acl grant` 整批回滚，srt 直接退出。

所以 `allowRead` 只能放**当前用户自己可写 DACL** 的路径（沙箱树、工作区、
workspace-volumes）。系统目录一律不加。
"""


def windows_acl_denied_paths() -> tuple[str, ...]:
    """Windows 上**不能**走 `denyRead`、必须用 ACL 加固的路径。

    它们全都在用户主目录下，而 `srt-win acl stamp` 对「parent 是主目录」的目标
    会 CPU 空转（详见 `win_acl` 模块说明）。带进 `denyRead` 的后果是整条命令
    永远不返回，而不是被拒绝。
    """
    if sys.platform != "win32":
        return ()
    home = _home()
    if home is None:
        return ()
    rels = (*_HOME_DENIED, *_BROWSER_DENIED)
    # **不按 exists() 过滤**：不存在的路径要记进 `missing`，一旦它被创建出来
    # （srt 的占位目录机制、用户装 AWS CLI 等）就必须在下次执行前补加固。
    # 早期版本在这里过滤，导致 `.azure` / `.gnupg` 出现后永久漏保护。
    return tuple(dict.fromkeys(str(home / rel) for rel in rels))


def denied_read_paths() -> tuple[str, ...]:
    """必须拒绝读取的绝对路径（目录不存在也照样写进策略：后来被建出来同样生效）。

    **不能把整个 `data_root()` 放进 `denyRead`**——这条踩过两次坑，实测数据如下
    （同一条 `echo`，本机 Windows 10，srt-win 0.0.83）：

    | denyRead | 单次执行耗时 |
    | --- | --- |
    | 空 | 3.6s |
    | `%APPDATA%\\ETHOS`（整个 data_root） | **44.5s** |
    | 只放 `edith-v2.db` | 5.0s |

    `denyRead` 的每一项都要 `srt-win acl grant` 递归改 DACL，而桌面模式下
    `data_root()` 就是 `%APPDATA%\\ETHOS`——里面装着 Electron 的 `Cache` /
    `GPUCache` / `Partitions`，几万个小文件。命令执行的兜底超时是 30s，于是
    **每一条命令都被超时杀掉**（审计里是 `exit_code=124, duration≈30.5s`），
    而 `verify_asrt` 因为 `denyRead=[]` 只要 3s、判定"后端可用"——两边结论相反，
    排查时会被彻底带偏。

    所以只拒绝真正的秘密载体：SQLite 主库及其 WAL/SHM（API Key、MCP Header、
    Git Token 的密文都在里面）。跨会话隔离不再靠 `denyRead` 兜——沙箱根在
    `data_root()/sandbox/` 之内，给父目录加 DENY 会连当前会话自己的 allowWrite
    一起挡掉（DENY 优先于 ALLOW），那是另一个更难查的故障。
    """
    roots: list[str] = []
    home = _home()
    handled = {p.casefold() for p in windows_acl_denied_paths()}
    if home is not None:
        for rel in (*_HOME_DENIED, *_BROWSER_DENIED):
            text = str(home / rel)
            if text.casefold() in handled:
                # Windows：交给 win_acl 加固。塞进 denyRead 会让 srt-win 空转。
                continue
            roots.append(text)
    # 应用私有数据：只拒绝数据库文件本身。`-wal`/`-shm` 存在才加——不存在的路径
    # 会被 srt 物化出来，反而干扰 SQLite。
    for name in ("edith-v2.db", "edith-v2.db-wal", "edith-v2.db-shm"):
        candidate = data_root() / name
        if candidate.exists():
            roots.append(str(candidate))
    return tuple(dict.fromkeys(roots))


@dataclass(frozen=True)
class SandboxPolicy:
    """一次执行的隔离策略。冻结：生成之后没有任何入口能改它。"""

    policy_version: str
    #: 本次执行允许写入的根（工作区权威根 + 沙箱可写区 + 私有临时目录）
    writable_roots: tuple[str, ...]
    #: 允许读取但不是本次工作区的路径（沙箱树、skills、工具链）
    read_only_roots: tuple[str, ...]
    denied_read: tuple[str, ...]
    denied_write: tuple[str, ...]
    #: 允许访问的域名；空 = **完全禁网**（默认姿态）
    allowed_domains: tuple[str, ...]
    #: 是否允许沙箱内进程连本机端口（默认否：本机还有 Runtime、数据库、其他服务）
    allow_local_network: bool = False
    #: 生成策略时的工作区根（诊断用，不参与判定）
    workspace_root: str = ""
    extra: dict[str, str] = field(default_factory=dict)

    def digest(self) -> str:
        """策略指纹：审计里只记这个，不记整份策略（里面有用户绝对路径）。"""
        payload = {
            "version": self.policy_version,
            "write": list(self.writable_roots),
            "read": list(self.read_only_roots),
            "denyRead": list(self.denied_read),
            "denyWrite": list(self.denied_write),
            "domains": list(self.allowed_domains),
            "local": self.allow_local_network,
        }
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def to_asrt_settings(self) -> dict:
        """转成 ASRT 的 settings JSON。

        只写 README 明确给出的字段：ASRT 对 settings 做 schema 校验，多写一个
        未知键会导致初始化直接失败（失败是 fail-closed，但会让整个功能不可用，
        所以字段集按最小可信集合来）。
        """
        return {
            "network": {
                "allowedDomains": list(self.allowed_domains),
                "deniedDomains": [],
            },
            "filesystem": {
                "allowWrite": list(self.writable_roots),
                "denyWrite": list(self.denied_write),
                "denyRead": list(self.denied_read),
                "allowRead": list(self.read_only_roots),
            },
        }


def _inside(path: Path, parent: Path) -> bool:
    try:
        return path.is_relative_to(parent)
    except (ValueError, OSError):  # pragma: no cover - is_relative_to 不抛这两种
        return False


def build_policy(
    *,
    sandbox_root: Path,
    cwd: Path,
    bound_root: str | None = None,
    allowed_domains: tuple[str, ...] = (),
    extra_writable: tuple[str, ...] = (),
) -> SandboxPolicy:
    """按本次执行的落点生成策略。

    可写范围按"权威根是谁"决定，与 P0-2 的路径判定同一套语义：

    - 绑定工作区：`bound_root` 可写；
    - 未绑定工作区：落点是 `<data_root>/workspace-volumes/{id}`，只把**该 id 目录**
      放行（不是整个 `workspace-volumes`，否则同机其他工作区可互相写）；
    - 其余（`work/`、`uploads/`、`artifacts/`）：沙箱树本身。

    无论哪种，沙箱树与私有临时目录都保持可写——工具链、构建缓存、pip/npm 缓存
    落在那里，不落到宿主用户目录。
    """
    sandbox_root = Path(sandbox_root).resolve()
    cwd = Path(cwd).resolve()

    writable: list[str] = [str(sandbox_root)]
    # 只读范围 = 沙箱树（+ 绑定的工作区）。**不含工具链**：见
    # `READ_ONLY_ROOTS_WARNING` —— 系统目录进 allowRead 会让 srt 直接启动失败。
    read_only: list[str] = [str(sandbox_root)]
    workspace_root = ""

    if bound_root:
        bound = Path(bound_root).resolve()
        writable.append(str(bound))
        read_only.append(str(bound))
        workspace_root = str(bound)
    else:
        volumes = (data_root() / "workspace-volumes").resolve()
        if _inside(cwd, volumes):
            # cwd 在 workspace-volumes/{id}/… 下：只放行这一个工作区
            rel = cwd.relative_to(volumes).parts
            if rel:
                volume = volumes / rel[0]
                writable.append(str(volume))
                read_only.append(str(volume))
                workspace_root = str(volume)

    # 虚拟映射根：`/home/user/workspace/{id}/…` 与 `/home/user/skills/…` 在命令
    # 执行前会被翻译成 `<data_root>/workspace-volumes/{id}/…` 和 `<data_root>/skills/…`。
    # 不放行的话 `cat workspace/foo.txt` 会被 srt 拒绝——而文件工具（走宿主进程）
    # 能读到同一份文件，模型看到的是"刚写的文件不见了"。
    #
    # **只读，不能放 writable**：srt 会重写 allowWrite 根的 DACL，把宿主账号的
    # 继承项切掉，结果是宿主自己都读不回那些文件（`pathlib.read_text` 直接
    # PermissionError）。`win_acl.ensure_grant` 的预授权**并不能**阻止重写
    # （实测放了 writable 之后失败用例从 3 个涨到 9 个），所以这里只放 allowRead。
    #
    # 代价：shell 对 `workspace/{id}` 只能读，写入要走文件工具（宿主进程写）。
    # `test_shell_sees_workspace_volume` 这类"shell 直接写卷"的用例因此失败，
    # 属于 ASRT 在 Windows 上的 ACL 行为限制。
    for mapped in (data_root() / "workspace-volumes", data_root() / "skills"):
        if mapped.exists():
            text = str(mapped.resolve())
            if text not in read_only:
                read_only.append(text)

    # 私有临时目录：每次执行独立，退出后由调用方清理（见 audit 的 cleanup 字段）
    tmp = sandbox_root / ".isolation-tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    writable.append(str(tmp))

    # 服务端显式追加的可写根（例如 push 用的临时仓库目录）。
    # **只能由服务端代码传**：工具参数进不来，模型无法扩大自己的可写范围。
    for extra in extra_writable:
        if extra:
            writable.append(str(Path(extra).resolve()))

    return SandboxPolicy(
        policy_version=POLICY_VERSION,
        writable_roots=tuple(dict.fromkeys(writable)),
        read_only_roots=tuple(dict.fromkeys(read_only)),
        denied_read=denied_read_paths(),
        denied_write=_DENIED_WRITE_NAMES,
        allowed_domains=tuple(allowed_domains),
        allow_local_network=False,
        workspace_root=workspace_root,
    )
