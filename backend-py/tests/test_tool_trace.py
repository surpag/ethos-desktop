"""工具追踪元数据与脱敏（阶段 8）。

这些断言守的是「trace 能还原操作链，但不复制页面内容」这条边界：
- 入参：`browser_fill.value` 可能是密码，不能原样进 trace；
- 结果：快照元素名称可能是用户刚填进去的内容，trace 只保留元信息；
- 收集器：ContextVar 只在工具执行期间有效，不能把上一轮的字段漏到下一轮。
"""

from __future__ import annotations

import json

from edith_backend.internal.tools import (
    record_tool_trace,
    redact_tool_input,
    redact_tool_output,
    tool_trace_scope,
)


def test_fill_value_is_redacted_in_trace_input() -> None:
    raw = json.dumps(
        {"observation_id": "obs_1", "ref": "el_003", "value": "hunter2"}
    )
    redacted = redact_tool_input("browser_fill", raw)
    assert "hunter2" not in redacted
    assert "<redacted: 7 chars>" in redacted
    assert json.loads(redacted)["ref"] == "el_003"


def test_other_inputs_are_untouched() -> None:
    raw = json.dumps({"url": "https://example.com"})
    assert redact_tool_input("browser_open_url", raw) == raw


def test_snapshot_output_drops_element_details() -> None:
    payload = {
        "observationId": "obs_1",
        "tabId": "tab_1",
        "pageVersion": 2,
        "url": "https://example.com/search",
        "elements": [
            {"ref": "el_001", "role": "textbox", "name": "alice@example.com", "tag": "input"}
        ],
        "truncated": False,
        "omitted": 0,
        "nextCursor": None,
        "page": {"offset": 0, "returned": 1, "total": 1},
        "budget": {"maxElements": 120, "maxChars": 10000, "usedChars": 100},
    }
    out = json.loads(redact_tool_output("browser_get_page_snapshot", json.dumps(payload)))
    assert out["observationId"] == "obs_1"
    assert out["tabId"] == "tab_1"
    assert out["elements_count"] == 1
    assert "elements" not in out
    assert "alice@example.com" not in json.dumps(out)


def test_error_output_keeps_diagnostic_fields() -> None:
    payload = {
        "error": "页面上找不到这个元素",
        "kind": "command_error",
        "retryable": True,
        "code": "element_missing",
        "suggestion": "重新取快照",
    }
    out = json.loads(redact_tool_output("browser_click", json.dumps(payload)))
    assert out["code"] == "element_missing"
    assert out["retryable"] is True


def test_non_browser_output_is_untouched() -> None:
    raw = '{"ok": true}'
    assert redact_tool_output("sandbox_read_file", raw) == raw


def test_scope_collects_fields_and_ignores_writes_after_exit() -> None:
    with tool_trace_scope() as meta:
        record_tool_trace(action="snapshot", tab_id="tab_1")
    assert meta == {"action": "snapshot", "tab_id": "tab_1"}

    # 退出后写入必须被忽略：否则上一轮 run 的字段会污染下一轮。
    record_tool_trace(action="leak")
    assert meta == {"action": "snapshot", "tab_id": "tab_1"}
