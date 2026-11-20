"""S1 第一阶段：未绑定态（UNBOUND）的逐字节基线。

这份清单在**改造任何代码之前**生成（2026-11-20，HEAD `765f6d6`），冻结的是
`LocalSandbox.resolve()` 在未绑定工作区下的实际输出：18 条输入 → 相对
`data_root` 的路径，或异常类型。

它存在的意义不是"描述期望"，而是**证明 S1/S2/S4 没有顺手改坏未绑定执行链**：

- S1 只加了一个无人调用的模块，这份清单必须原样通过；
- S2 接文件工具、S4 接 Shell 之后，`workspace/{id}` 必须仍然落在
  `workspace-volumes/{id}` 上，且按需创建保留；
- 若将来**有意**改变未绑定行为，必须替换 `fixtures/unbound_baseline.json`
  并在提交信息里写明理由与新 sha256——不允许"改测试让它变绿"。

序列化规则固定为 `json.dumps(ensure_ascii=False, indent=2, sort_keys=True) + "\\n"`
（UTF-8 无 BOM），所以比对是**字节级**的，格式化漂移也会被发现。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from edith_backend.config import data_root
from edith_backend.internal.sandbox.local import LocalSandbox

BASELINE = Path(__file__).parent / "fixtures" / "unbound_baseline.json"

#: 冻结文件本身的 sha256（改造前生成，2026-11-20）
BASELINE_SHA256 = "3c7e66892a522849099fea09abada29431210825e0436a559f7f7247854552f9"


def _dump(records: list[dict]) -> str:
    return json.dumps(records, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _render(value: Path) -> dict:
    """真实路径 → 相对 data_root 的表示；绝对路径不能进常量（随机器变）。"""
    try:
        rel = value.resolve().relative_to(data_root().resolve())
        return {"type": "path", "value": rel.as_posix()}
    except ValueError:
        return {"type": "path", "value": f"<outside>/{value.name}"}


def test_baseline_file_is_intact() -> None:
    """先证明输入的基线没被改动，再拿它当判据。"""
    raw = BASELINE.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == BASELINE_SHA256
    assert len(raw) == 2204


def test_unbound_resolve_matches_frozen_baseline(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ETHOS_DATA_DIR", str(tmp_path))
    expected = BASELINE.read_text(encoding="utf-8")
    inputs = [item["input"] for item in json.loads(expected)]

    sandbox = LocalSandbox.open("u1", "s1")
    actual: list[dict] = []
    for item in inputs:
        try:
            actual.append({"input": item, "result": _render(sandbox.resolve(item))})
        except Exception as exc:  # noqa: BLE001 - 冻结的就是"会抛什么"
            actual.append(
                {
                    "input": item,
                    "result": {"type": "exception", "class": type(exc).__name__},
                }
            )

    assert _dump(actual) == expected


def test_volume_dir_is_created_on_demand(tmp_path: Path, monkeypatch) -> None:
    """未绑定态的按需创建是**工作区初始化**，S1 必须保留（不是"去掉 mkdir"）。"""
    monkeypatch.setenv("ETHOS_DATA_DIR", str(tmp_path))
    sandbox = LocalSandbox.open("u1", "s1")
    target = sandbox.resolve("workspace/ws-on-demand/a.txt")

    volumes = (tmp_path / "workspace-volumes").resolve()
    assert (volumes / "ws-on-demand").is_dir()
    assert target.resolve().is_relative_to(volumes)


def test_workspace_mount_comes_from_data_root(tmp_path: Path, monkeypatch) -> None:
    """`workspace/{id}` 必须落在 data_root 下的 Volume，而不是别的目录。

    这一条防的是"基线碰巧通过了，但其实两个视图都指错了"。
    """
    monkeypatch.setenv("ETHOS_DATA_DIR", str(tmp_path))
    sandbox = LocalSandbox.open("u1", "s1")
    mount = sandbox._ensure_workspace_mount()  # noqa: SLF001 - 基线要断言挂载真实落点

    assert mount == (tmp_path / "workspace-volumes").resolve()
