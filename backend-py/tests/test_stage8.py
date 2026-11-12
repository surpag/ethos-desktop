"""阶段 8 验收测试：上传文件处理与 MCP 运行时。"""

from __future__ import annotations

import asyncio

from edith_backend.internal.agentrun.configuration import (
    append_uploads,
    message_with_uploads,
)
from edith_backend.internal.userconfig.mcp import MCPServer
from edith_backend.internal.userconfig.mcp_runtime import MCPToolContext, _toolset_name


def test_message_with_uploads_empty() -> None:
    assert message_with_uploads("你好", []) == "你好"


def test_message_with_uploads_appends_attachments() -> None:
    result = message_with_uploads("分析一下", ["uploads/report.pdf", "uploads/data.csv"])
    assert result.startswith("分析一下\n\n附件：")
    assert "`report.pdf`" in result
    assert "`data.csv`" in result


def test_message_with_uploads_only_files() -> None:
    result = message_with_uploads("", ["uploads/a.txt"])
    assert result == "附件：\n- `a.txt`"


def test_append_uploads_without_text() -> None:
    instruction = append_uploads("", ["uploads/a.txt"])
    assert "本次用户上传文件" in instruction
    assert "uploads/a.txt" in instruction
    assert "uploads/ 读取" in instruction


def test_append_uploads_keeps_skill_block() -> None:
    """Skill 段落与上传清单是拼接而不是互相覆盖——两者都属于「本次运行的上下文」。"""
    instruction = append_uploads("## Skill\n- `current-time`", ["uploads/a.txt"])
    assert instruction.startswith("## Skill")
    assert "current-time" in instruction
    assert "本次用户上传文件" in instruction


def test_append_uploads_no_files_is_identity() -> None:
    assert append_uploads("## Skill\n- x", []) == "## Skill\n- x"
    assert append_uploads("", []) == ""


def test_mcp_toolset_name_normalization() -> None:
    server = MCPServer(id="abc123-def456", name="Search Service", url="", transport="", enabled=True)
    name = _toolset_name(server)
    assert name.startswith("search_service_")
    assert name.endswith("abc123de")  # 去掉横杠，前 8 位


def test_mcp_toolset_name_chinese_falls_back() -> None:
    server = MCPServer(id="xyz789", name="搜索", url="", transport="", enabled=True)
    name = _toolset_name(server)
    assert name == "mcp_xyz789"


def test_mcp_tool_context_execute_unknown() -> None:
    context = MCPToolContext()
    result = asyncio.run(context.execute("no_such_tool", {}))
    assert "unknown MCP tool" in result
