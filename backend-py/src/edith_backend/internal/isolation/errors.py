"""隔离层的失败类型：**只有一种正确结局——拒绝执行**。

这里的异常不是"警告"：调用方（`LocalCommands`）不捕获它，工具层统一转成
`tool_failure`（retryable=False）。沙箱不可用时换命令、换路径、重试都不会成功，
写成可重试的错误只会让模型在同一个不可能通过的路径上循环。
"""

from __future__ import annotations


class SandboxUnavailable(RuntimeError):
    """没有可用的隔离后端 / 初始化失败 / 策略非法。

    `reason` 是给审计与诊断用的**稳定标签**（不要拿它做控制流分支）：
    用户在设置页与文档里看到的是同一个词，排查时才能对上号。
    """

    #: 该平台没有实现后端（当前只有 win32 的 ASRT / AppContainer 与 POSIX 的 ASRT）
    NOT_SUPPORTED = "not_supported"
    #: 后端存在但依赖没装（ASRT 未安装 / Node 版本不足 / 未执行 windows-install）
    NOT_INSTALLED = "not_installed"
    #: 后端存在但初始化失败（探测命令非零退出、settings 校验失败）
    INIT_FAILED = "init_failed"
    #: 策略构建失败（工作区根无法确定、敏感目录解析不出来）
    POLICY_INVALID = "policy_invalid"
    #: 显式关闭（`ETHOS_SANDBOX_BACKEND=off`）
    DISABLED = "disabled"

    def __init__(self, message: str, *, reason: str = INIT_FAILED) -> None:
        super().__init__(message)
        self.reason = reason
