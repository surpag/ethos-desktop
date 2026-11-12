"""本地沙箱的最小可用性验证：挂载点、路径翻译、编码。

跑法：ETHOS_DATA_DIR=/tmp/sbtest PYTHONPATH=src .venv/Scripts/python.exe ../scripts/probe_local.py
"""
from __future__ import annotations

import asyncio
import shutil

from edith_backend.internal.sandbox.local import POSIX_SHELL, LocalSandbox

WS = "ws-abc"


async def main() -> None:
    print("POSIX_SHELL =", POSIX_SHELL)
    sb = LocalSandbox.open("u1", "s1")

    await sb.files.write(f"/home/user/workspace/{WS}/a.txt", "hello")

    r = await sb.commands.run(
        f"ls -la /home/user/workspace/{WS} && cat /home/user/workspace/{WS}/a.txt"
    )
    print("--- 虚拟绝对路径 ---")
    print("exit", r.exit_code)
    print(r.stdout.strip())
    print("ERR:", r.stderr.strip()[:200])

    r2 = await sb.commands.run(f"ls ../workspace/{WS}")
    print("--- 相对路径（cwd=work）---")
    print("exit", r2.exit_code, "|", r2.stdout.strip())

    r3 = await sb.commands.run("pwd && mkdir -p ../workspace/ws-abc/sub && echo made")
    print("--- pwd + mkdir -p ---")
    print("exit", r3.exit_code, "|", r3.stdout.strip(), "|", r3.stderr.strip()[:120])

    r4 = await sb.commands.run("ls ../workspace/ws-abc")
    print("--- mkdir 结果可见 ---")
    print("exit", r4.exit_code, "|", r4.stdout.strip())

    # 中文输出：走 GBK 编码的子进程
    r5 = await sb.commands.run(
        'python -c "import sys;sys.stderr.buffer.write(bytes([0xcf,0xb5,0xcd,0xb3,0xd5,0xd2,0xb2,0xbb,0xb5,0xbd]))"'
    )
    print("--- GBK stderr 解码 ---")
    print("exit", r5.exit_code, "| stderr =", r5.stderr.strip()[:60])


if __name__ == "__main__":
    shutil.rmtree("/tmp/sbtest", ignore_errors=True)
    asyncio.run(main())
