"""后台进程台账：跨实例可见、归属校验、会话级回收、pid 复用识别。

这些用例针对的是一组**已经发生**的缺陷，不是假想的边界：

1. `Service.workspace()` 每次工具调用都新建 `LocalSandbox`。台账挂在实例上时，
   `sandbox_start_process` 登记完就随实例消失——下一次 list 空表、kill 只能盲杀。
2. `LocalSandbox.kill()` 没有调用方，进程活到后端退出，一直占着工作区文件。
3. `kill(pid)` 对未登记 pid 仍会 `taskkill`，等于任何会话都能杀同机任意进程。

写法约束：一个用例只用一个事件循环。生产环境是一个常驻 loop，"启动—结束—观察"
都发生在同一个循环里；每次 `asyncio.run` 新建 loop 会让回收 task 随循环一起被取消，
于是 GC 落在循环关闭之后，对已失效的句柄报警（WinError 6）——那是测试写法制造的
噪声，不是被测代码的行为。
"""

from __future__ import annotations

import asyncio
import sys
import time

import pytest

from edith_backend.internal.sandbox import processes
from edith_backend.internal.sandbox.local import LocalSandbox
from process_helpers import cleanup_detached, spawn_detached

LONG_RUNNING = "ping -n 30 127.0.0.1 > nul" if sys.platform == "win32" else "sleep 30"
QUICK_EXIT = "ping -n 1 127.0.0.1 > nul" if sys.platform == "win32" else "true"


@pytest.fixture(autouse=True)
def clean_registry(tmp_path, monkeypatch):
    """每个用例都从空台账开始。用例自己负责在循环内回收。"""
    monkeypatch.setenv("ETHOS_DATA_DIR", str(tmp_path))
    processes.release_all()
    try:
        yield
    finally:
        processes.release_all()
        cleanup_detached()


def _alive(pid: int) -> bool:
    return processes._probe(pid)[0]


async def _start(sandbox: LocalSandbox) -> int:
    info = await sandbox.commands.run(LONG_RUNNING, background=True)
    assert info.pid > 0
    return info.pid


async def _wait_dead(pid: int, timeout: float = 10.0) -> bool:
    """进程退出是异步的（taskkill 返回 ≠ 进程立刻消失）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        await asyncio.sleep(0.1)
    return False


async def _settle() -> None:
    """回收本用例启动的进程，并让回收 task 观察退出、正常收尾 transport。"""
    processes.release_all()
    await asyncio.sleep(0.3)


def test_list_sees_process_started_by_another_instance() -> None:
    """换一个实例（= 别人的工具调用）也能看到并解释进程。

    旧实现在这里返回空表，模型会得出"没有进程在跑"的结论，然后重复启动。
    """

    async def body() -> None:
        first = LocalSandbox.open("user-1", "session-1")
        pid = await _start(first)

        second = LocalSandbox.open("user-1", "session-1")
        listed = await second.commands.list()
        entry = next((item for item in listed if item.pid == pid), None)
        assert entry is not None, "台账没有跨实例共享"
        assert entry.cmd == LONG_RUNNING
        # cwd 必须是虚拟路径：宿主路径回给模型后会被下一轮判越界
        assert entry.cwd.startswith("/home/user"), entry.cwd
        await _settle()

    asyncio.run(body())


def test_kill_from_another_instance_terminates_process() -> None:
    async def body() -> None:
        first = LocalSandbox.open("user-1", "session-1")
        pid = await _start(first)

        second = LocalSandbox.open("user-1", "session-1")
        await second.commands.kill(pid)

        assert await _wait_dead(pid), "跨实例 kill 没有结束进程"
        assert all(item.pid != pid for item in await second.commands.list())
        await _settle()

    asyncio.run(body())


def test_unregistered_pid_is_refused_without_blind_kill(monkeypatch) -> None:
    """未登记的 pid 必须拒绝，且**不得**调用终结逻辑。

    旧实现走 `taskkill` 兜底，任何会话都能结束同机任意进程。
    """
    calls: list[int] = []
    monkeypatch.setattr(
        processes, "terminate_tree", lambda pid: calls.append(pid) or True
    )

    async def body() -> None:
        sandbox = LocalSandbox.open("user-1", "session-1")
        with pytest.raises(processes.ProcessNotOwned):
            await sandbox.commands.kill(424242)

    asyncio.run(body())
    assert calls == [], "未登记的 pid 被盲杀了"


def test_another_session_cannot_kill_this_session_process() -> None:
    async def body() -> None:
        owner = LocalSandbox.open("user-1", "session-a")
        pid = await _start(owner)

        intruder = LocalSandbox.open("user-1", "session-b")
        with pytest.raises(processes.ProcessNotOwned):
            await intruder.commands.kill(pid)
        assert _alive(pid), "越权 kill 不应该影响目标进程"

        # 目标会话自己仍然可以结束它（证明上面的拒绝是归属问题，不是记录丢了）
        await owner.commands.kill(pid)
        assert await _wait_dead(pid)
        await _settle()

    asyncio.run(body())


def test_release_session_only_affects_that_session() -> None:
    """删会话按归属回收：另一个会话的后台进程不能受牵连。"""

    async def body() -> None:
        first = LocalSandbox.open("user-1", "session-a")
        second = LocalSandbox.open("user-1", "session-b")
        pid_a = await _start(first)
        pid_b = await _start(second)

        released = processes.release_session("user-1", "session-a")
        assert released == 1
        assert await _wait_dead(pid_a)
        assert _alive(pid_b), "回收 session-a 时误杀了 session-b 的进程"
        assert any(item.pid == pid_b for item in await second.commands.list())
        await _settle()

    asyncio.run(body())


def test_sandbox_kill_releases_the_whole_session() -> None:
    """`LocalSandbox.kill()` 指向会话而不是某一个实例。"""

    async def body() -> None:
        first = LocalSandbox.open("user-1", "session-1")
        pid = await _start(first)

        other_instance = LocalSandbox.open("user-1", "session-1")
        await other_instance.kill()
        assert await _wait_dead(pid)
        assert await first.commands.list() == []
        await _settle()

    asyncio.run(body())


def test_list_prunes_exited_process() -> None:
    async def body() -> None:
        sandbox = LocalSandbox.open("user-1", "session-1")
        info = await sandbox.commands.run(QUICK_EXIT, background=True)
        assert info.pid > 0

        assert await _wait_dead(info.pid)
        assert await sandbox.commands.list() == []
        await _settle()

    asyncio.run(body())


def test_reused_pid_is_detected_and_not_killed() -> None:
    """pid 被系统复用后，旧的登记不能用来杀新进程。

    伪造创建时刻就是在模拟"记录里的 pid 已经是别人了"：list 应该摘掉它，
    kill 必须拒绝，而不是把复用该 pid 的进程杀掉。
    """

    async def body() -> None:
        sandbox = LocalSandbox.open("user-1", "session-1")
        pid = await _start(sandbox)

        entry = processes.registry._by_owner[("user-1", "session-1")][pid]
        if entry.create_time is None:  # pragma: no cover - 平台拿不到创建时刻
            pytest.skip("当前平台无法取得进程创建时刻，pid 复用无法识别")
        entry.create_time += 3600.0  # 差一小时：不可能是同一个进程

        assert all(item.pid != pid for item in await sandbox.commands.list())
        with pytest.raises(processes.ProcessNotOwned):
            await sandbox.commands.kill(pid)
        assert _alive(pid), "识别出 pid 复用后不应再执行终结"
        # 记录已被摘除，release_all 找不到它，直接按 pid 收尾（这是测试自己的进程）
        processes.terminate_tree(pid)
        await _settle()

    asyncio.run(body())


def test_cross_loop_kill_does_not_raise() -> None:
    """创建进程的那个事件循环关闭之后，另一个循环调用 kill 不能抛跨循环异常。

    这是台账"只存 pid、不存 Process"的直接后果：终结走 `taskkill` / `killpg`，
    不 `await` 原循环上的 Future，所以不会出现 "attached to a different loop"。
    如果哪天有人把 Process 对象放回台账，这条用例会以跨 loop 异常失败。

    **不断言进程一定还活着**：`asyncio` 的 `BaseSubprocessTransport.close()` 在
    子进程仍在运行时本身就会 `kill()` 它，所以 `asyncio.run()` 返回后进程可能
    已经被 stdlib 带走。生产是常驻单 loop，不存在这一步。
    """
    first = LocalSandbox.open("user-1", "session-1")
    info = asyncio.run(first.commands.run(LONG_RUNNING, background=True))
    pid = info.pid
    assert pid > 0

    async def later() -> None:
        second = LocalSandbox.open("user-1", "session-1")
        try:
            await second.commands.kill(pid)
        except processes.ProcessNotOwned:
            # 进程已被前一个循环的 transport 结束：台账摘除后这是正确结果
            pass
        await _settle()

    asyncio.run(later())


def test_kill_terminates_registered_process_from_another_loop() -> None:
    """真实终结能力：登记一个游离进程，在另一个循环里 kill，进程必须消失。

    用 `subprocess.Popen` 而不是 `commands.run`：后者的 transport 绑在创建它的
    循环上，循环关闭时会替我们把进程杀掉，于是根本测不出 kill 是否生效。
    """
    detached = spawn_detached()
    processes.register(
        ("user-1", "session-1"),
        pid=detached.pid,
        cmd=LONG_RUNNING,
        cwd="/home/user/work",
    )
    pid = detached.pid

    async def later() -> None:
        second = LocalSandbox.open("user-1", "session-1")
        await second.commands.kill(pid)
        assert await _wait_dead(pid), "跨循环的终结没有生效"
        await _settle()

    asyncio.run(later())


def test_owner_is_stable_for_same_session() -> None:
    """归属键只由 user/session 决定，与实例、目录、时间无关。"""
    a = LocalSandbox.open("user-1", "session-1")
    b = LocalSandbox.open("user-1", "session-1")
    assert a._process_owner == b._process_owner
    assert a._process_owner != LocalSandbox.open("user-2", "session-1")._process_owner
