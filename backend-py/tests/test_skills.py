"""Skill 单层实现的验收测试。

这一层替代了原来的 10 个测试文件（作用域裁决、存储事务、迁移、跨会话、
项目级存储……）。那些机制已经不存在，留下它们的测试只会测一堆死代码。

这里钉住的是**新的不变量**：
- 目录扫描是唯一真相源，没有索引文件；
- 单个坏 Skill 不影响其余（扫描不会因为一个文件而整体失败）；
- 注入文本跨 run 逐字节一致（否则 system prompt 前缀缓存全废）；
- 预算按条目截断，且截断必须写出来（不许静默丢）；
- `skills/` 在沙箱里是权威目录的联接，读写走同一个位置。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from edith_backend.internal.sandbox.local import LocalSandbox
from edith_backend.internal.skills.budget import (
    SkillLoadBudget,
    index_budget_chars,
    plan_index,
)
from edith_backend.internal.skills.catalog import (
    builtin_skill_dirs,
    load_skill,
    scan_skills,
    seed_builtin,
    split_frontmatter,
)
from edith_backend.internal.skills.instruction import render_skill_instruction
from edith_backend.internal.sandbox.tools import skill_body_key
from edith_backend.internal.skills.types import SKILLS_PATH, Skill

VALID = "---\nname: demo\ndescription: 演示用 Skill\n---\n\n# Demo\n\n正文。\n"


def _write_skill(root: Path, dir_name: str, content: str = VALID) -> Path:
    target = root / dir_name
    target.mkdir(parents=True, exist_ok=True)
    (target / "SKILL.md").write_text(content, encoding="utf-8")
    return target


# --------------------------------------------------------------------------
# frontmatter
# --------------------------------------------------------------------------


def test_split_frontmatter_strips_bom() -> None:
    front, body = split_frontmatter("\uFEFF" + VALID)
    assert "name: demo" in front
    assert body.strip() == "# Demo\n\n正文。"


@pytest.mark.parametrize(
    "content",
    [
        "no frontmatter at all",
        "---\nname: x\n",  # 未闭合
    ],
)
def test_split_frontmatter_rejects_malformed(content: str) -> None:
    with pytest.raises(ValueError):
        split_frontmatter(content)


def test_load_skill_requires_name_and_description(tmp_path: Path) -> None:
    path = _write_skill(tmp_path, "broken", "---\nname: only-name\n---\n正文\n")
    with pytest.raises(ValueError, match="name and description"):
        load_skill(path)


def test_load_skill_keeps_dir_name_separate_from_name(tmp_path: Path) -> None:
    """目录名与 frontmatter 的 name 可以不同——路径必须用目录名。"""
    path = _write_skill(
        tmp_path, "dir-name", "---\nname: display-name\ndescription: x\n---\n正文\n"
    )
    skill = load_skill(path)
    assert skill.id == "dir-name"
    assert skill.name == "display-name"


# --------------------------------------------------------------------------
# 扫描
# --------------------------------------------------------------------------


def test_scan_missing_dir_is_not_an_error(tmp_path: Path) -> None:
    result = scan_skills(tmp_path / "nope")
    assert result.exists is False
    assert result.skills == []
    assert result.skipped == []


def test_scan_sorts_by_name_for_deterministic_prompt(tmp_path: Path) -> None:
    for dir_name, name in (("b", "zeta"), ("a", "alpha"), ("c", "mid")):
        _write_skill(
            tmp_path, dir_name, f"---\nname: {name}\ndescription: d\n---\n正文\n"
        )
    result = scan_skills(tmp_path)
    assert [s.name for s in result.skills] == ["alpha", "mid", "zeta"]


def test_scan_skips_broken_skill_but_keeps_others(tmp_path: Path) -> None:
    """一个坏 SKILL.md 只让它自己消失，其余照常可用——但必须留下记录。"""
    _write_skill(tmp_path, "good")
    _write_skill(tmp_path, "bad", "没有 frontmatter")
    result = scan_skills(tmp_path)
    assert [s.id for s in result.skills] == ["good"]
    assert [name for name, _ in result.skipped] == ["bad"]


def test_scan_ignores_files_and_dirs_without_skill_md(tmp_path: Path) -> None:
    _write_skill(tmp_path, "real")
    (tmp_path / "stray.txt").write_text("x", encoding="utf-8")
    (tmp_path / "empty-dir").mkdir()
    assert [s.id for s in scan_skills(tmp_path).skills] == ["real"]


# --------------------------------------------------------------------------
# seed
# --------------------------------------------------------------------------


def test_seed_copies_builtin_skills(tmp_path: Path) -> None:
    created = seed_builtin(tmp_path)
    expected = {d.name for d in builtin_skill_dirs()}
    assert set(created) == expected
    assert {p.name for p in tmp_path.iterdir()} == expected


def test_seed_never_overwrites_existing(tmp_path: Path) -> None:
    """用户改过的内置 Skill 不能被升级悄悄覆盖回去。

    代价是内置 Skill 的修复不会送达已有副本——这个取舍是刻意的：
    「用户改的内容被无声抹掉」比「内置 Skill 旧了一点」严重得多。
    """
    target = _write_skill(tmp_path, "current-time")
    user_edited = "---\nname: current-time\ndescription: 我改过的\n---\n我的正文\n"
    (target / "SKILL.md").write_text(user_edited, encoding="utf-8")

    created = seed_builtin(tmp_path)
    assert "current-time" not in created
    assert (target / "SKILL.md").read_text(encoding="utf-8") == user_edited


# --------------------------------------------------------------------------
# 注入渲染
# --------------------------------------------------------------------------


def test_render_empty_returns_empty_text() -> None:
    """没有 Skill 时不往 prompt 里塞一个空标题。"""
    rendered = render_skill_instruction([])
    assert rendered.text == ""
    assert rendered.injected == []


def test_render_includes_name_and_dir_path() -> None:
    skills = [
        Skill(id="dir-a", name="alpha", description="做甲事"),
        Skill(id="dir-b", name="beta", description="做乙事"),
    ]
    text = render_skill_instruction(skills).text
    assert "alpha" in text and "做甲事" in text
    assert f"{SKILLS_PATH}/dir-a" in text
    assert f"{SKILLS_PATH}/dir-b" in text


def test_render_is_byte_stable_across_calls() -> None:
    """同一份数据两次渲染必须逐字符相同，否则 system prompt 前缀缓存全废。"""
    skills = [Skill(id=f"d{i}", name=f"s{i}", description=f"描述{i}") for i in range(5)]
    assert render_skill_instruction(skills).text == render_skill_instruction(skills).text
    # 输入顺序不同也要得到同一份文本（内部按 name 排序）
    assert (
        render_skill_instruction(list(reversed(skills))).text
        == render_skill_instruction(skills).text
    )


def test_render_truncation_is_announced(monkeypatch) -> None:
    """预算截断必须在文本里写明——模型不知道被截过，会得出「那个 Skill 不存在」。"""
    monkeypatch.setenv("SKILL_INDEX_BUDGET_CHARS", "120")
    skills = [Skill(id=f"d{i}", name=f"s{i}", description="x" * 40) for i in range(6)]
    rendered = render_skill_instruction(skills)
    assert rendered.omitted, "预算这么小应当有条目被截断"
    assert "还有" in rendered.text and "未列出" in rendered.text


def test_render_marks_injected_paths() -> None:
    skills = [Skill(id="dir-a", name="alpha", description="d")]
    rendered = render_skill_instruction(skills)
    assert rendered.injected == [f"{SKILLS_PATH}/dir-a"]
    assert rendered.budget_chars == index_budget_chars()


# --------------------------------------------------------------------------
# L1 预算
# --------------------------------------------------------------------------


def test_plan_index_keeps_whole_items_only(monkeypatch) -> None:
    """截断以条目为单位：半个摘要比没有更糟（模型会以为描述就到那儿为止）。"""
    monkeypatch.setenv("SKILL_INDEX_BUDGET_CHARS", "100")
    plan = plan_index([60, 60, 20])
    assert plan.kept == [0, 2]
    assert plan.omitted == [1]


# --------------------------------------------------------------------------
# L2 加载预算
# --------------------------------------------------------------------------


def test_budget_counts_distinct_skills_not_reads() -> None:
    """分页读同一个 Skill 不能算成多个——否则读一个 100KB 文件会被误杀。"""
    budget = SkillLoadBudget(max_skills=1, max_bytes=1000)
    assert budget.charge("skills/a", 10).allowed
    assert budget.charge("skills/a", 10).allowed
    denied = budget.charge("skills/b", 10)
    assert denied.allowed is False
    assert denied.reason == "skill_count"


def test_budget_counts_bytes_cumulatively() -> None:
    """反复读同一段也必须累加——否则片段读取可以绕过上下文预算。"""
    budget = SkillLoadBudget(max_skills=9, max_bytes=100)
    assert budget.charge("skills/a", 60).allowed
    denied = budget.charge("skills/a", 60)
    assert denied.allowed is False
    assert denied.reason == "skill_bytes"


def test_denied_read_does_not_change_ledger() -> None:
    """被拒的读取没有返回任何正文，记账会让模型在重试里把预算越撞越小。"""
    budget = SkillLoadBudget(max_skills=9, max_bytes=100)
    budget.charge("skills/a", 90)
    before = budget.loaded_bytes
    budget.charge("skills/a", 50)
    assert budget.loaded_bytes == before
    assert budget.denied == 1


# --------------------------------------------------------------------------
# 正文路径识别
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "skills/demo/SKILL.md",
        "/home/user/skills/demo/SKILL.md",
        "skills\\demo\\SKILL.md",
    ],
)
def test_skill_body_key_accepts_single_layer(path: str) -> None:
    assert skill_body_key(path) == "skills/demo"


@pytest.mark.parametrize(
    "path",
    [
        "skills/demo/references/guide.md",  # 按需片段不计入
        "skills/a/b/SKILL.md",  # 只剩一层，两层不是合法形状
        "skills/SKILL.md",  # 必须有自己的目录
        "",
        None,
    ],
)
def test_skill_body_key_rejects_others(path) -> None:
    assert skill_body_key(path) == ""


# --------------------------------------------------------------------------
# 沙箱：skills 是联接，不是副本
# --------------------------------------------------------------------------


def test_sandbox_skills_path_points_at_data_root(tmp_path: Path, monkeypatch) -> None:
    """`skills/x` 必须解析到 `<data_root>/skills/x`——同一份文件，不是副本。

    这是「删掉同步」的前提：如果它落在沙箱目录里，就必须有东西把文件搬过来。
    """
    monkeypatch.setenv("ETHOS_DATA_DIR", str(tmp_path))
    sandbox = LocalSandbox.open("u-skill", "s-skill")

    resolved = sandbox.resolve(f"{SKILLS_PATH}/demo/SKILL.md")
    assert resolved == (tmp_path / SKILLS_PATH / "demo" / "SKILL.md").resolve()


def test_sandbox_skills_roundtrip_through_virtual(tmp_path: Path, monkeypatch) -> None:
    """真实路径必须能还原成 `skills/…`，否则模型拿到一个自己无法再次传入的路径。"""
    monkeypatch.setenv("ETHOS_DATA_DIR", str(tmp_path))
    sandbox = LocalSandbox.open("u-skill2", "s-skill2")

    real = tmp_path / SKILLS_PATH / "demo" / "SKILL.md"
    real.parent.mkdir(parents=True, exist_ok=True)
    real.write_text(VALID, encoding="utf-8")

    virtual = sandbox.virtual(real)
    assert virtual == "/home/user/skills/demo/SKILL.md"
    # 还原出的虚拟路径必须能再解析回同一个文件
    assert sandbox.resolve(virtual) == real.resolve()


def test_sandbox_skills_cannot_escape_root(tmp_path: Path, monkeypatch) -> None:
    """`skills/../x` 归一化成沙箱内的普通路径，合法；真正出根才拒绝。"""
    monkeypatch.setenv("ETHOS_DATA_DIR", str(tmp_path))
    sandbox = LocalSandbox.open("u-skill3", "s-skill3")
    from edith_backend.internal.sandbox.local import SandboxPathError

    # 归一化后落在沙箱根内，不算逃逸
    sandbox.resolve(f"{SKILLS_PATH}/../work")

    with pytest.raises(SandboxPathError):
        sandbox.resolve("../../etc/passwd")
