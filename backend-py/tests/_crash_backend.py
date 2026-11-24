"""被测试强杀的后端替身：登记一个游离进程，然后什么都不做。

它不走 `lifespan`、不注册 atexit。测试会直接 `TerminateProcess` 它，用来复现
"登记已落盘、内存台账随进程消失"的真实崩溃现场。
"""

from __future__ import annotations

import json
import subprocess
import sys
import time

from edith_backend.internal.sandbox import process_store, processes


def main() -> int:
    command = sys.argv[1]
    kwargs: dict = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    child = subprocess.Popen(
        command,
        shell=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **kwargs,
    )
    processes.register(
        ("user-1", "session-1"),
        pid=child.pid,
        cmd=command,
        cwd="/home/user/work",
    )
    print(
        json.dumps(
            {"pid": child.pid, "ledger": str(process_store.ledger_path())},
            ensure_ascii=False,
        ),
        flush=True,
    )
    time.sleep(120)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
