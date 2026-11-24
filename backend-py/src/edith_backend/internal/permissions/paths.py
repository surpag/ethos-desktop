"""敏感路径匹配（安全约束，与档位正交）。

这是**不可配置**的一层：只读 / 审批 / 完全三档都生效，`full` 档也生效。
把它做成档位的一个子集，等于宣布「完全档没有任何底线」。

## 为什么不能只做字符串包含

三个真实的绕过方式，逐个处理：

1. **分隔符与大小写**：`C:\\Users\\me\\.SSH\\id_rsa`、`/home/u/.ssh/id_rsa`、
   `work\\.env` 必须同判。统一 `\\`→`/` 后按**段**比较，而不是子串包含——
   `docs/env.md` 不该因为含 `env` 被拦，`.env.local` 才该拦。
2. **`..` 穿越**：`work/../.env` 字面上不含敏感段序列，但落点就是 `.env`。
   含 `..` 一律拒绝，**不依赖上游是否已经拦过**（纵深防御：上游改了、或某个新入口
   忘了校验，这一层仍然挡得住）。
3. **符号链接**：逻辑路径可以完全干净，落点却是 `~/.ssh/id_rsa`。所以调用方必须
   对**宿主真实路径**再调一次本函数（第二遍）。
4. **容器操作**：`rm -rf work/archive`、`git add work/archive` 的目标路径一个敏感段
   都没有，真正被删/被暂存的是**目录里的每一条**。这类入口不能只看目标路径，
   必须按条目展开——见 `first_sensitive_entry` 与调用方 `gitops.plan_git_add`。

无法可靠解析的路径（含 NUL 字符）默认拒绝：说不清它指向哪，就只能假设它指向最坏
的地方。真实路径解析本身失败（`OSError`）时同理——那是调用方的责任。
"""

from __future__ import annotations

import os
from pathlib import Path

#: 段级精确匹配（小写比较）
_SEGMENT_EXACT = frozenset(
    {
        # 版本库元数据：删了 git 能力就废，里面还有 hooks 与 config
        ".git",
        ".hg",
        ".svn",
        # SSH
        ".ssh",
        "id_rsa",
        "id_ed25519",
        "id_ecdsa",
        "id_dsa",
        # 云与凭据
        ".aws",
        ".azure",
        ".gnupg",
        ".netrc",
        "netrc",
        "credentials",
        "credentials.json",
        "auth.json",
    }
)

#: 段级前缀匹配：`.env` / `.env.local` / `.env.production`
_SEGMENT_PREFIX = (".env",)

#: 段级后缀匹配：私钥与证书容器
_SEGMENT_SUFFIX = (".pem", ".key", ".p12", ".pfx")

#: 连续两段匹配：`.config/gcloud`
_SEGMENT_PAIRS = ((".config", "gcloud"),)


def _segments(text: str) -> list[str]:
    return [
        part
        for part in text.replace("\\", "/").split("/")
        if part and part != "."
    ]


def _match_segments(parts: list[str]) -> str:
    """返回命中的敏感描述（可读）；未命中返回空串。"""
    lowered = [part.lower() for part in parts]
    for index, part in enumerate(lowered):
        if part in _SEGMENT_EXACT:
            return part
        if part.startswith(_SEGMENT_PREFIX):
            return part
        if part.endswith(_SEGMENT_SUFFIX):
            return part
        if index + 1 < len(lowered):
            for first, second in _SEGMENT_PAIRS:
                if part == first and lowered[index + 1] == second:
                    return f"{first}/{second}"
    return ""


def check_path(raw: object) -> str:
    """检查一个路径字符串。

    返回命中的敏感描述，或无法判定落点的原因；空串表示通过。
    工作区相对路径与宿主绝对路径都适用（同一个函数，两次调用）。
    """
    if not isinstance(raw, str) or not raw.strip():
        return ""
    if "\x00" in raw:
        return "路径含无法解析的字符"
    parts = _segments(raw.strip())
    if ".." in parts:
        return "路径含 '..'，无法可靠判定落点"
    return _match_segments(parts)


def first_sensitive_entry(root: str) -> str:
    """遍历目录，返回第一个命中敏感规则的**相对路径**；没有命中返回空串。

    这是「按条目判定」的那一半：目标路径干净不代表里面干净。用在容器操作上
    （递归删除、目录级 `git add`），因为那些操作展开的是目录里的每一条，
    而 `check_path` 只看得到信封。

    不跟随符号链接与目录联接：`rmtree` 删的是链接本身而不是它指向的东西，
    跟着走会把外部目录当成待删内容判（既不准，也可能走不完）。
    目录不存在或本来就是文件时返回空串——那种情况由 `check_path` 判过。

    遍历失败（`OSError`）向上抛，由调用方按「无法核对 = 拒绝」处理：
    这一层不能把「读不到目录」当成「里面没问题」。
    """
    if not os.path.isdir(root):
        return ""
    stack: list[tuple[str, str]] = [("", root)]
    while stack:
        prefix, current = stack.pop()
        with os.scandir(current) as entries:
            for entry in entries:
                shown = f"{prefix}{entry.name}"
                if check_path(shown):
                    return shown
                # follow_symlinks=False：链接与目录联接不进栈（见 docstring）
                if entry.is_dir(follow_symlinks=False):
                    stack.append((f"{shown}/", entry.path))
    return ""


class PathRejected(PermissionError):
    """真实路径不满足访问约束（命中敏感段，或解析后越出给定的根）。

    继承 `PermissionError`：HTTP 层原本就把 `PermissionError` 映射成 403，
    新增这一层不需要再引入一套错误家族。
    """


def guard_real_path(raw: object, *, root: object = None) -> str:
    """对**已解析的真实路径**做敏感段与越界检查，返回归一化后的真实路径。

    这是与文件工具同一套规则（`check_path`）的**第二遍**。为什么面板也需要它：
    面板直读本地磁盘时不经过沙箱 `resolve()`，而目录联接 / 符号链接能让一个
    逻辑上完全干净的路径（`workspace/{id}/k/config`）落到 `~/.ssh/id_rsa`
    或绑定根之外。第一遍看的是"用户说要看哪个文件"，这一遍看的是"最后读的是哪个
    文件"——只有两遍都干净才算干净。

    `root` 为空时只判敏感段：沙箱分支的越界由 `LocalSandbox.resolve()` 保证，
    这里重复判会因根的定义不同而产生假阳性。

    无法判定（无法规范化、根为空串）一律拒绝：「说不清它指向哪」不能当成
    「没问题」。`root` 本身命中敏感段时同样拒绝——把工作区绑到 `~/.ssh` 上
    不应该换来一条绕过规则的通道。
    """
    if not isinstance(raw, (str, os.PathLike)):
        return ""
    text = os.fspath(raw)
    if not text.strip():
        return ""
    # 第一遍：用户写下的路径本身
    hit = check_path(text)
    if hit:
        raise PathRejected(f"敏感路径不可访问：{text}（命中 {hit}）")
    try:
        real = os.path.realpath(text)
    except OSError as exc:
        raise PathRejected(f"路径无法解析为真实位置：{text}（{exc}）") from exc

    if root is not None and os.fspath(root):
        try:
            base = os.path.realpath(os.fspath(root))
        except OSError as exc:
            raise PathRejected(f"根目录无法解析为真实位置：{root}（{exc}）") from exc
        if not Path(real).is_relative_to(Path(base)):
            raise PathRejected(
                f"路径解析后越出根目录（疑似链接指向外部）：{text} → {real}"
            )

    # 第二遍：链接指向的真实落点
    hit = check_path(real)
    if hit:
        raise PathRejected(f"敏感路径不可访问：{text} → {real}（命中 {hit}）")
    return real

