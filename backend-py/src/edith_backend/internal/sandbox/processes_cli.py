"""后台进程遗留记录的运维入口：inspect / retry / acknowledge。

**刻意不提供 kill。** 终结只能由 `retry` 在"身份确认（创建时刻 + 镜像一致）"
之后执行——人工拿 pid 直接杀会绕过归属校验，而归属校验正是这套台账存在的理由。

三个命令的边界：

- `inspect`：只读。打印登记状态（ok / missing / corrupt / unreadable / shape_error）、
  记录与实时核验结果（含记录值与当前值），不写文件、不杀进程；
- `retry`：重新核验。`gone` / `reused` 只删记录，`unknown` 原地保留，只有
  `confirmed` 才终结。`acknowledged` 会被拒绝——那是人的决定，不能被命令翻案；
- `acknowledge`：人工确认"知道它存在、暂不处理"。不杀进程、不删记录、不删审计，
  只在记录上写处置时间/原因/结果；重复执行幂等。

退出码：0 成功，1 找不到记录 / 参数错误，2 被拒绝（acknowledged 上执行 retry），
3 登记不可读（inspect 专用：`corrupt` / `unreadable` / `shape_error`）。
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Sequence

from . import processes


def _emit(payload: object) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="edith-processes",
        description="查看与处置上一次运行遗留的后台进程记录",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser(
        "inspect",
        help="只读查看遗留记录与身份核验详情（不杀进程、不改文件）",
    )

    retry = sub.add_parser(
        "retry",
        help="重新核验一条记录；只有确认归属才终结进程",
    )
    retry.add_argument("pid", type=int)

    ack = sub.add_parser(
        "acknowledge",
        help="人工确认暂不处理（不杀进程，记录保留 status=acknowledged）",
    )
    ack.add_argument("pid", type=int)
    ack.add_argument(
        "--reason",
        required=True,
        help="处置原因（必填，会写进记录与审计）",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)

    if args.command == "inspect":
        payload = processes.describe_registry()
        _emit(payload)
        # 登记损坏 / 不可读 / 形状错误时返回非零：空的 records 在这里的意思是
        # "读不出来"，不是"没有遗留进程"。两者混同会让运维得出错误结论。
        return 0 if payload["status"] in ("ok", "missing") else 3

    try:
        if args.command == "retry":
            _emit(processes.retry(args.pid))
        else:
            _emit(processes.acknowledge(args.pid, args.reason))
    except LookupError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except PermissionError as exc:
        # 不是"参数错了"，是"这条记录不允许这样做"——分开退出码，脚本能区分。
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
