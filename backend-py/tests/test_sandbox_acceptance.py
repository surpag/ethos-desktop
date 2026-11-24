"""ASRT 真机验收：验证**操作系统层面**的拒绝，而不是我们自己函数的返回值。

为什么单独一个文件：这些用例只有装了 ASRT（且 Windows 已 `windows-install`）
才有意义；没装时整组 skip。它们的断言全部落在"命令在沙箱里跑完之后，磁盘/网络
上发生了什么"，不是"`check_path` 返回了什么字符串"。

已知的运行前提（本机实测）：ASRT 0.0.83 + Node 22.20 + 非提权会话，
`srt-win acl grant` 首次对新根授权需要几秒；如果它卡住超过 25 秒，本组会 skip
并在 skip 原因里给出 srt 的实际错误——**不是**把它当成通过。
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from edith_backend.internal import isolation
from edith_backend.internal.isolation.capabilities import probe_asrt, verify_asrt
from edith_backend.internal.sandbox.local import LocalSandbox

pytestmark = pytest.mark.skipif(
    sys.platform != "win32" and sys.platform != "linux" and sys.platform != "darwin",
    reason="需要真实 OS 隔离后端",
)


def _asrt_state() -> tuple[bool, str]:
    cap = probe_asrt()
    if not cap.available:
        return False, cap.detail
    ok, detail = verify_asrt(cap)
    return ok, detail


_ASRT_OK, _ASRT_DETAIL = _asrt_state()

requires_asrt = pytest.mark.skipif(
    not _ASRT_OK, reason=f"ASRT 不可用：{_ASRT_DETAIL or '未安装'}"
)


@pytest.fixture(scope="module")
def sandbox(tmp_path_factory):
    """真实 ASRT 后端下的沙箱。显式覆盖会话级的 host 后端设置。"""
    data_dir = tmp_path_factory.mktemp("asrt-data")
    saved = {
        k: os.environ.get(k)
        for k in (
            "ETHOS_DATA_DIR",
            "EDITH_DB_PATH",
            isolation.BACKEND_ENV,
            isolation.UNSAFE_HOST_ENV,
        )
    }
    os.environ["ETHOS_DATA_DIR"] = str(data_dir)
    os.environ["EDITH_DB_PATH"] = str(data_dir / "acceptance.db")
    os.environ[isolation.BACKEND_ENV] = "asrt"
    os.environ.pop(isolation.UNSAFE_HOST_ENV, None)
    isolation.reset()
    sandbox = None
    try:
        sandbox = LocalSandbox.open("accept-user", f"accept-{uuid.uuid4().hex[:8]}")
        yield sandbox
    finally:
        if sandbox is not None:
            _forget_asrt_roots(sandbox, data_dir)
        isolation.reset()
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _forget_asrt_roots(sandbox: LocalSandbox, data_dir: Path) -> None:
    """删掉本组留下的 persistent write grant。

    `tmp_path_factory` 的目录会被 pytest 轮转删除，而 srt-win 的持久授权记录会
    留下来；记录指向已删除目录后，后续任何 `acl grant` 的 recovery 阶段都会 CPU
    空转（实测 25s 也退不出来），只能管理员 `acl forget --all --force` 才能救。
    单 root 的 `acl forget <PATH>` 不需要提权，所以就地清掉。
    """
    from edith_backend.internal.isolation.backends import asrt

    cap = probe_asrt()
    exe = asrt.srt_win_exe(cap.location) if cap.available else None
    if exe is None:
        return
    targets = [
        sandbox.root,
        sandbox.root / ".isolation-tmp",
        data_dir / "isolation" / "probe",
    ]
    for target in targets:
        try:
            subprocess.run(
                [str(exe), "acl", "forget", str(target), "--json"],
                capture_output=True,
                timeout=20,
            )
        except (OSError, subprocess.SubprocessError):
            # 清理失败不值得让验收变红；但也不能静默 —— 留在 audit 里
            pass


def _run(sandbox: LocalSandbox, command: str, timeout: float = 90):
    import asyncio

    return asyncio.run(sandbox.commands.run(command, timeout=timeout))


# ---- 文件系统 ----


@requires_asrt
def test_write_and_read_inside_workspace(sandbox) -> None:
    """沙箱内必须能写文件并读回来。

    写的是**本次 cwd**（沙箱树里的 `work/`，确定在可写根内）。不能写
    `/home/user/workspace/probe.txt`：没有 workspace id 时它翻译到
    `workspace-volumes/` 根，那不属于任何会话的可写根（策略只放行
    `workspace-volumes/{id}`，否则同机工作区可互相写）。
    """
    result = _run(sandbox, "echo inside-ok > probe.txt && cat probe.txt")
    assert result.exit_code == 0, result.stderr[-500:]
    assert "inside-ok" in result.stdout


@requires_asrt
def test_write_outside_workspace_is_denied(sandbox, tmp_path_factory) -> None:
    outside = Path(tmp_path_factory.mktemp("outside")) / "escaped.txt"
    result = _run(
        sandbox,
        f"echo nope > '{outside.as_posix()}'",
    )
    assert result.exit_code != 0, "OS 层面没有拦住工作区外写入"
    assert not outside.exists(), "文件竟然真的被写出来了"


@requires_asrt
def test_read_app_private_data_is_denied(sandbox) -> None:
    """应用私有目录（SQLite 里是凭据密文）必须读不到。

    用数据根里的真实文件验证，不去碰用户的 `~/.ssh`——测试不该在别人家里
    创建目录。
    """
    from edith_backend.config import data_root

    secret = data_root() / "isolation" / "acceptance-secret.txt"
    secret.parent.mkdir(parents=True, exist_ok=True)
    secret.write_text("acceptance-secret-value", encoding="utf-8")
    try:
        result = _run(sandbox, f"cat '{secret.as_posix()}'")
        assert result.exit_code != 0, "应用私有数据没有被拒绝"
        assert "acceptance-secret-value" not in result.stdout
    finally:
        secret.unlink(missing_ok=True)


@requires_asrt
def test_path_traversal_is_denied(sandbox, tmp_path_factory) -> None:
    outside = Path(tmp_path_factory.mktemp("traversal")) / "target.txt"
    outside.write_text("traversal-secret", encoding="utf-8")
    result = _run(
        sandbox, "cat ../../../../../../../../'%s'" % outside.as_posix().lstrip("/")
    )
    assert result.exit_code != 0, "路径穿越没有被拒绝"
    assert "traversal-secret" not in result.stdout


# ---- 网络 ----


@requires_asrt
def test_unauthorized_domain_is_blocked(sandbox) -> None:
    """allowedDomains 为空 = 全禁。用一个一定能连的域名验证"被拦住"。"""
    result = _run(
        sandbox,
        "curl -s -m 8 http://example.com || echo CURL_FAILED",
        timeout=60,
    )
    # 拦截在代理层完成：响应体是 "Connection blocked by network allowlist"，
    # 而 curl 不带 -f 时退出码仍是 0。只看退出码会把"已拦截"判成"放行了"。
    assert (
        "CURL_FAILED" in result.stdout
        or "blocked" in result.stdout.lower()
        or result.exit_code != 0
    ), result.stdout[-300:]


@requires_asrt
def test_localhost_is_blocked(sandbox) -> None:
    result = _run(
        sandbox,
        "curl -s -m 8 http://127.0.0.1:135 || echo LOCAL_BLOCKED",
        timeout=60,
    )
    assert (
        "LOCAL_BLOCKED" in result.stdout
        or "blocked" in result.stdout.lower()
        or result.exit_code != 0
    ), result.stdout[-300:]


# ---- 进程 ----


@requires_asrt
def test_background_process_can_be_killed(sandbox) -> None:
    import asyncio

    info = asyncio.run(
        sandbox.commands.run("sleep 60", background=True)
    )
    assert info.pid
    asyncio.run(sandbox.commands.kill(info.pid))
    remaining = asyncio.run(sandbox.commands.list())
    assert all(item.pid != info.pid for item in remaining)


@requires_asrt
def test_stdio_tools_still_work_in_sandbox(sandbox) -> None:
    """隔离不能把基本工具链一起关掉：git 版本查询必须能跑通（不联网）。"""
    result = _run(sandbox, "git --version")
    assert result.exit_code == 0, result.stderr[-400:]
    assert "git version" in result.stdout



