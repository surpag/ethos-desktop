"""本地目录合并：把一个目录的文件「只新增不覆盖」地并进另一个目录。

**为什么单独存在**：绑定一个已有本地目录作为工作区时，`workspace-volumes/{id}`
里可能还留着绑定之前的产物（用户以前在应用里传过文件）。要不要搬过去由用户决定，
但无论搬不搬，规则都只有一条：**绝不覆盖目录里已有的文件**。

这套规则与沙箱无关，纯粹是两个本地目录之间的事——所以它不属于任何"同步"模块。
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path

logger = logging.getLogger(__name__)

# 内置忽略：构建产物与版本库元数据。搬过去只会拖慢速度并污染视图。
DEFAULT_IGNORES = [
    "__pycache__",
    ".git",
    ".venv",
    "venv",
    "node_modules",
    ".next",
    ".mypy_cache",
    ".pytest_cache",
    ".DS_Store",
    "*.pyc",
]


def _load_ignore(local_root: Path) -> list[str]:
    """读取 .edithignore（每行一个简单模式；支持目录名与后缀）。"""
    f = local_root / ".edithignore"
    if not f.is_file():
        return []
    try:
        return [
            line.strip()
            for line in f.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
    except OSError:
        return []


def is_ignored(rel: str, patterns: list[str]) -> bool:
    """是否忽略该相对路径（内置忽略 + .edithignore）。

    模式形态刻意只有三种，够用且可预测：`后缀*`（尾部通配，匹配任一目录段）、
    `*后缀`（扩展名）、普通名（精确段）。尾部通配是后加的：Skill 存储的
    `.staging-<ts>/` / `.old-<ts>/` 名字里带时间戳，没法写成固定名。
    """
    parts = rel.split("/")
    for pat in DEFAULT_IGNORES + patterns:
        p = pat.rstrip("/")
        if not p:
            continue
        if p.startswith("*"):
            if rel.endswith(p[1:]):
                return True
        elif p.endswith("*"):
            prefix = p[:-1]
            if rel.startswith(prefix) or any(part.startswith(prefix) for part in parts):
                return True
        elif p in parts or rel == p or rel.startswith(p + "/"):
            return True
    return False


def _iter_merge_candidates(src: Path, dst: Path, prefix: str) -> Iterator[tuple[str, Path]]:
    """产出「只新增不覆盖」合并会复制的文件：逐个 yield (相对路径, 源绝对路径)。

    规则（`plan_merge` 预览与实际合并共用，避免两处漂移）：
    - 跳过 `..` 穿越；
    - 跳过忽略项（内置 DEFAULT_IGNORES + dst 的 .edithignore）；
    - 跳过 dst 中已存在的路径（绝不覆盖用户文件）。
    """
    if not src.is_dir():
        return
    p = (prefix or "").strip().strip("/")
    base = (src / p) if p else src
    if not base.is_dir():
        return
    ignore = _load_ignore(dst)
    for path in base.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(base).as_posix()
        if ".." in rel.split("/"):
            continue
        if is_ignored(rel, ignore):
            continue
        if (dst / rel).exists():
            continue
        yield rel, path


def plan_merge(src: Path, dst: Path, *, prefix: str = "") -> list[str]:
    """列出「只新增不覆盖」合并将会复制的相对路径（只读，不落盘）。

    供 UI 在真正合并前预览「还有 N 个文件未合并（在旧副本里）」。
    """
    return [rel for rel, _ in _iter_merge_candidates(src, dst, prefix)]


def merge_dir_add_only(src: Path, dst: Path, *, prefix: str = "") -> int:
    """把旧副本 src 中本地尚不存在的文件合并到 dst（绑定目录）。

    只新增、绝不覆盖用户已有文件（绑定已有工作区时的安全初始合并）。
    """
    copied = 0
    for rel, source in _iter_merge_candidates(src, dst, prefix):
        target = dst / rel
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read_bytes())
            copied += 1
        except OSError as exc:
            logger.warning("merge %s failed: %s", rel, exc)
    if copied:
        logger.info("merged %d files from legacy copy into %s", copied, dst)
    return copied
