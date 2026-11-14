/**
 * 审批事件的投影回归（`lib/chat/stream.ts`）。
 *
 *   node --test tests/approval-stream.test.ts
 *
 * 这一层只做一件事：把 `approval.requested/resolved` 落到**正确的那张工具卡片**上。
 * 覆盖：正常挂起 → 终态、重放幂等、卡片缺失时补建、assistantId 缺失时按
 *      toolCallId 定位（不能挂到最后一个块上）、未知状态不被改写。
 */
import test from "node:test";
import assert from "node:assert/strict";

import { applyStreamEvent } from "../lib/chat/stream.ts";
import type { Timeline } from "../lib/chat/type.ts";

const APPROVAL = {
  approvalId: "a1",
  toolCallId: "call_1",
  toolName: "sandbox_write_file",
  arguments: '{"path": "a.md"}',
  capabilities: ["write"],
  status: "pending",
  expiresAt: "2026-11-14T09:00:00+00:00",
};

/** 一个只有「工具调用卡」的时间线（run.started + tool.started 之后的样子）。 */
function timelineWithToolCall(): Timeline {
  return applyStreamEvent(
    applyStreamEvent({ blocks: [] }, {
      type: "run.started",
      sessionId: "s1",
      requestId: "r1",
      assistantId: "assistant_r1",
    }),
    {
      type: "tool.started",
      assistantId: "assistant_r1",
      toolCallId: "call_1",
      toolName: "sandbox_write_file",
      arguments: '{"path": "a.md"}',
      toolStatus: "running",
    },
  );
}

function toolBlock(timeline: Timeline, assistantID: string) {
  const assistant = timeline.blocks.find((block) => block.id === assistantID);
  assert.ok(assistant && assistant.type === "assistant", `assistant ${assistantID} missing`);
  const tool = assistant.blocks.find((block) => block.id === "call_1");
  assert.ok(tool && tool.type === "tool", "tool block missing");
  return tool;
}

test("approval.requested 挂到对应的工具卡片上", () => {
  const next = applyStreamEvent(timelineWithToolCall(), {
    type: "approval.requested",
    assistantId: "assistant_r1",
    approval: APPROVAL,
  });

  const tool = toolBlock(next, "assistant_r1");
  assert.equal(tool.approval?.approvalId, "a1");
  assert.equal(tool.approval?.status, "pending");
  assert.deepEqual(tool.approval?.capabilities, ["write"]);
  // 卡片本身仍然是「执行中」：挂起不是结束，工具还没跑
  assert.equal(tool.status, "running");
});

test("approval.resolved 把同一张卡片改成终态，不新开卡片", () => {
  const pending = applyStreamEvent(timelineWithToolCall(), {
    type: "approval.requested",
    assistantId: "assistant_r1",
    approval: APPROVAL,
  });
  const resolved = applyStreamEvent(pending, {
    type: "approval.resolved",
    assistantId: "assistant_r1",
    approval: { ...APPROVAL, status: "approved" },
  });

  const tool = toolBlock(resolved, "assistant_r1");
  assert.equal(tool.approval?.approvalId, "a1");
  assert.equal(tool.approval?.status, "approved");
  const assistant = resolved.blocks.find((block) => block.id === "assistant_r1");
  assert.ok(assistant && assistant.type === "assistant");
  assert.equal(assistant.blocks.filter((block) => block.id === "call_1").length, 1);
});

test("重放同一条 approval.requested 是幂等的", () => {
  const once = applyStreamEvent(timelineWithToolCall(), {
    type: "approval.requested",
    assistantId: "assistant_r1",
    approval: APPROVAL,
  });
  const twice = applyStreamEvent(once, {
    type: "approval.requested",
    assistantId: "assistant_r1",
    approval: APPROVAL,
  });
  assert.deepEqual(twice, once);
});

test("卡片还没建出来时（重放从审批开始）补一张，参数来自事件本身", () => {
  const started = applyStreamEvent({ blocks: [] }, {
    type: "run.started",
    sessionId: "s1",
    requestId: "r1",
    assistantId: "assistant_r1",
  });
  const next = applyStreamEvent(started, {
    type: "approval.requested",
    assistantId: "assistant_r1",
    approval: APPROVAL,
  });

  const tool = toolBlock(next, "assistant_r1");
  assert.equal(tool.toolName, "sandbox_write_file");
  assert.equal(tool.arguments, '{"path": "a.md"}');
  assert.equal(tool.approval?.status, "pending");
});

test("assistantId 缺失时按 toolCallId 定位，不挂到最后一个助手块上", () => {
  // 两轮对话：审批属于第一轮，最后一个块是第二轮 —— 挂错就会让人以为
  // 当前这一轮要批准一个它根本没发起的操作。
  const first = applyStreamEvent(timelineWithToolCall(), {
    type: "run.completed",
    requestId: "r1",
    assistantId: "assistant_r1",
    outcome: "complete",
  });
  const second = applyStreamEvent(
    applyStreamEvent({ blocks: [...first.blocks, {
      type: "user",
      id: "u2",
      content: "再来一次",
      images: [],
      createdAt: new Date().toISOString(),
    }] }, {
      type: "run.started",
      sessionId: "s1",
      requestId: "r2",
      assistantId: "assistant_r2",
    }),
    { type: "message.delta", assistantId: "assistant_r2", blockId: "t2", blockType: "text", delta: "好的" },
  );

  const next = applyStreamEvent(second, {
    type: "approval.requested",
    approval: APPROVAL, // 没有 assistantId（旧后端）
  });

  assert.equal(toolBlock(next, "assistant_r1").approval?.approvalId, "a1");
  const latest = next.blocks.find((block) => block.id === "assistant_r2");
  assert.ok(latest && latest.type === "assistant");
  assert.equal(latest.blocks.some((block) => block.type === "tool"), false);
});

test("认不出的状态原样保留（不擅自当成 pending 或 approved）", () => {
  const next = applyStreamEvent(timelineWithToolCall(), {
    type: "approval.resolved",
    assistantId: "assistant_r1",
    approval: { ...APPROVAL, status: "revoked_by_policy" },
  });
  // 界面只对 status === "pending" 渲染按钮，所以原样保留 = 不可操作，
  // 这比猜成 approved（看起来批准成功）或 pending（允许再点）都安全。
  assert.equal(toolBlock(next, "assistant_r1").approval?.status, "revoked_by_policy");
});
