"""skills 目录：扫描单层 Skill 目录，并把内置 Skill 铺到数据目录。

唯一的数据源是 `<data_root>/skills/<目录名>/SKILL.md`。没有索引文件、
没有 overview.md、没有「存储门面」——目录扫描就是全部真相。

索引曾经存在是因为云端要跨进程、跨卷读取（E2B Volume 里只有一份 markdown）。
桌面端目录就在本机，扫描成本是一次 `iterdir`，再维护一份派生索引只会带来
「索引与目录不一致」这一类问题。
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .types import SKILL_FILE, Skill

logger = logging.getLogger(__name__)

#: 随包发布的内置 Skill 源目录（只读）。
_BUILTIN_DIR = Path(__file__).parent / "system"


def split_frontmatter(content: str) -> tuple[str, str]:
    """分离 SKILL.md 的 YAML 头部和 Markdown 正文。"""
    content = content.lstrip("\uFEFF")
    lines = content.split("\n")
    if not lines or lines[0].strip() != "---":
        raise ValueError("SKILL.md must start with YAML frontmatter")
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            return "\n".join(lines[1:index]), "\n".join(lines[index + 1 :])
    raise ValueError("SKILL.md frontmatter is not closed")


def load_skill(skill_dir: Path) -> Skill:
    """读一个 Skill 目录。

    frontmatter 缺 `name` / `description` 直接抛错而不是填默认值：这两个字段是
    模型决定「要不要读它」的唯一依据，缺失时填一个空字符串等于让它永远不被选中，
    却看不出任何异常。
    """
    skill_path = skill_dir / SKILL_FILE
    frontmatter, body = split_frontmatter(skill_path.read_text(encoding="utf-8"))
    metadata = yaml.safe_load(frontmatter) or {}
    if not isinstance(metadata, dict):
        raise ValueError(f"{skill_path}: frontmatter is not a mapping")
    name = str(metadata.get("name") or "").strip()
    description = str(metadata.get("description") or "").strip()
    if not name or not description:
        raise ValueError(f"{skill_path}: frontmatter requires name and description")
    return Skill(
        id=skill_dir.name,
        name=name,
        description=description,
        body=body.strip(),
    )


@dataclass
class ScanResult:
    """一次扫描的产出。

    `skipped` 不是错误日志，是要给用户看的事实：一个坏掉的 SKILL.md 只会让它
    自己消失，其余照常可用——所以必须有地方说明「少了谁、为什么少」。
    """

    skills: list[Skill] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    #: 目录是否存在。不存在 ≠ 出错：首次启动时它本来就不该存在。
    exists: bool = True


def scan_skills(root: Path) -> ScanResult:
    """扫描 `root` 下的所有 Skill 目录，按 name 稳定排序。

    排序是必须的：注入文本要跨 run 逐字节一致，否则 system prompt 前缀每次都变，
    跨 run 的缓存复用直接归零（实测 94.8% → 0%）。
    """
    result = ScanResult()
    if not root.is_dir():
        result.exists = False
        return result
    for entry in sorted(root.iterdir(), key=lambda p: p.name):
        if not entry.is_dir():
            continue
        if not (entry / SKILL_FILE).exists():
            continue
        try:
            result.skills.append(load_skill(entry))
        except Exception as exc:  # noqa: BLE001 - 单个坏 Skill 不该拖垮整次扫描
            result.skipped.append((entry.name, str(exc)))
            logger.warning("skill %s skipped: %s", entry.name, exc)
    result.skills.sort(key=lambda s: s.name)
    return result


def builtin_skill_dirs() -> list[Path]:
    """随包内置的 Skill 目录列表（只读源）。"""
    if not _BUILTIN_DIR.is_dir():
        return []
    return [
        entry
        for entry in sorted(_BUILTIN_DIR.iterdir(), key=lambda p: p.name)
        if entry.is_dir() and (entry / SKILL_FILE).exists()
    ]


def seed_builtin(dest: Path) -> list[str]:
    """把内置 Skill 铺进 `dest`，返回本次新铺的目录名。

    **只在目标不存在时复制**：用户改过的内置 Skill 不能被下次升级悄悄覆盖回去。
    代价是内置 Skill 的修复不会自动送达已存在的副本——这是刻意的取舍，
    「用户改的内容被无声抹掉」比「内置 Skill 旧了一点」严重得多。
    """
    created: list[str] = []
    dest.mkdir(parents=True, exist_ok=True)
    for source in builtin_skill_dirs():
        target = dest / source.name
        if target.exists():
            continue
        try:
            shutil.copytree(source, target)
            created.append(source.name)
        except Exception as exc:  # noqa: BLE001 - 铺不进去不影响其它 Skill
            logger.warning("seed builtin skill %s failed: %s", source.name, exc)
    if created:
        logger.info("seeded builtin skills into %s: %s", dest, ", ".join(created))
    return created


class Catalog:
    """按需扫描数据目录，回答「现在有哪些 Skill」。

    刻意**不缓存**：缓存会引入失效问题（用户在外部编辑器里删了一个 Skill，
    缓存版本要等到什么时候才更新？）。目录只有几十个条目，每次 run 扫一遍
    的成本远低于任何缓存失效策略的复杂度。
    """

    def __init__(self, root: Path) -> None:
        self._root = root

    @property
    def root(self) -> Path:
        return self._root

    def scan(self) -> ScanResult:
        return scan_skills(self._root)

    def summaries(self) -> list[Skill]:
        return self.scan().skills

    def find(self, name: str) -> Skill | None:
        for skill in self.scan().skills:
            if skill.id == name or skill.name == name:
                return skill
        return None
