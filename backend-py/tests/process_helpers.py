"""后台进程测试的共用辅助。

**为什么需要"游离进程"**：`LocalCommands.run(background=True)` 走
`asyncio.create_subprocess_*`，它的 transport 绑在创建它的事件循环上。
`asyncio.run()` 返回时 `BaseSubprocessTransport.close()` 会 **kill 仍在运行的
子进程**——于是用 `commands.run` 起的进程，在测试换到下一个循环时可能已经死了，
根本测不出"另一次调用能否终结它"。

`subprocess.Popen` 直接起的进程不归任何事件循环管，生命周期只受操作系统控制，
更接近真实场景里"上一次运行遗留下来的那个进程"。
"""

from __future__ import annotations

import subprocess
import sys

LONG_RUNNING = "ping -n 30 127.0.0.1 > nul" if sys.platform == "win32" else "sleep 30"

_KEEP: list[subprocess.Popen] = []


def spawn_detached(cmd: str = LONG_RUNNING) -> subprocess.Popen:
    kwargs: dict = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    proc = subprocess.Popen(
        cmd,
        shell=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **kwargs,
    )
    _KEEP.append(proc)
    return proc


def cleanup_detached() -> None:
    for proc in _KEEP:
        try:
            proc.kill()
        except OSError:
            pass
    _KEEP.clear()
