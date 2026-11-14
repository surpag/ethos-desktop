import type {
  ApprovalPayload,
  AssistantBlock,
  ChatStreamEvent,
  CompletionEvidence,
  ErrorBlock,
  RunOutcome,
  Timeline,
  ToolApproval,
} from "./type";

// Read ETHOS's SSE frames without exposing transport details to ChatPage.
export async function readChatStream(
  response: Response,
  onEvent: (event: ChatStreamEvent) => void,
) {
  if (!response.body) {
    throw new Error("ETHOS did not return a response stream");
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { done, value } = await reader.read();
    buffer += decoder.decode(value, { stream: !done });

    let boundary = buffer.indexOf("\n\n");
    while (boundary >= 0) {
      const frame = buffer.slice(0, boundary);
      buffer = buffer.slice(boundary + 2);
      readFrame(frame, onEvent);
      boundary = buffer.indexOf("\n\n");
    }

    if (done) {
      return;
    }
  }
}

// applyStreamEvent is the single bridge from an SSE event to visible state.
//
// 它必须对同一事件**幂等**：断流重连时服务端会补发一段已经发过的事件，重复
// 应用不能产生重复的助手块或重复的工具卡片。所有分支都按 id 定位再更新，
// 不存在「无脑 append」——那是重放会炸的唯一写法。
export function applyStreamEvent(timeline: Timeline, event: ChatStreamEvent): Timeline {
  if (event.type === "run.started") {
    const existing = timeline.blocks.find((block) => block.id === event.assistantId);
    if (existing) {
      // 重放：这一轮的助手块已经在时间线里。若它是被增量事件「救回来」的
      // （没有 run.started 就先落了内容），这里补上 requestId —— 计划面板靠它定位。
      if (existing.type === "assistant" && !existing.requestId) {
        return updateAssistant(timeline, event.assistantId, (assistant) => ({
          ...assistant,
          requestId: event.requestId,
        }));
      }
      return timeline;
    }
    return {
      blocks: [...timeline.blocks, {
        type: "assistant",
        id: event.assistantId,
        // 让助手块记住自己属于哪一次 run：计划是按 run 存的，
        // 没有这个关联就只能把计划画在时间线末尾（看起来像属于最新一条消息）。
        requestId: event.requestId,
        createdAt: new Date().toISOString(),
        blocks: [],
      }],
    };
  }

  // 日志断层只影响「还能不能接着补」，不影响已渲染的内容。
  if (event.type === "run.log_truncated") {
    return timeline;
  }

  if (event.type === "run.error") {
    // 先落下错误卡片，再把本轮耗时挂到本次运行的助手块上（失败也给出耗时）
    return stampDuration(
      { blocks: [...timeline.blocks, errorTimelineEvent(event.error.message)] },
      event.assistantId,
      event.durationMs,
    );
  }

  // 终态事件只补耗时与完成证据：内容块早已由增量事件累积完成
  if (event.type === "run.completed" || event.type === "run.canceled") {
    const stamped = stampDuration(timeline, event.assistantId, event.durationMs);
    if (event.type === "run.canceled") {
      return stamped; // 用户取消没有「完成证据」可言
    }
    return stampOutcome(
      stampEvidence(stamped, event.assistantId, event.completionEvidence),
      event.assistantId,
      event.outcome,
      event.endReason,
    );
  }

  if (event.type === "reasoning.delta" || event.type === "message.delta") {
    return updateAssistant(timeline, event.assistantId, (assistant) => {
      const index = assistant.blocks.findIndex((block) => block.id === event.blockId);
      if (index < 0) {
        return {
          ...assistant,
          blocks: [
            ...assistant.blocks,
            { type: event.blockType, id: event.blockId, content: event.delta },
          ],
        };
      }

      const blocks = [...assistant.blocks];
      const block = blocks[index];
      if (block.type === "tool") {
        return assistant;
      }
      blocks[index] = { ...block, content: block.content + event.delta };
      return { ...assistant, blocks };
    });
  }

  if (event.type === "tool.started") {
    return updateAssistant(timeline, event.assistantId, (assistant) => {
      // 重放去重：同一次工具调用只能有一张卡片，否则重连后会出现两份「已调用」。
      const index = assistant.blocks.findIndex((block) => block.id === event.toolCallId);
      if (index >= 0) {
        const blocks = [...assistant.blocks];
        const block = blocks[index];
        if (block.type === "tool") {
          blocks[index] = {
            ...block,
            toolName: event.toolName,
            arguments: event.arguments ?? block.arguments,
            status: event.toolStatus,
          };
        }
        return { ...assistant, blocks };
      }
      return {
        ...assistant,
        blocks: [...assistant.blocks, {
          type: "tool",
          id: event.toolCallId,
          toolName: event.toolName,
          arguments: event.arguments ?? "",
          status: event.toolStatus,
        }],
      };
    });
  }

  if (event.type === "tool.finished") {
    return updateAssistant(timeline, event.assistantId, (assistant) => ({
      ...assistant,
      blocks: assistant.blocks.map((block) =>
        block.id === event.toolCallId
          ? { ...block, status: event.toolStatus, result: event.toolResult }
          : block,
      ),
    }));
  }

  // 审批：挂起与终态都只是「给已有那张工具卡片补一个状态」。
  // 卡片必须按 toolCallId 定位——不能按「最后一个块」猜：重放时 tool.started
  // 与 approval.requested 可能分批到达，猜错就把审批面板挂到了别的调用上。
  if (event.type === "approval.requested" || event.type === "approval.resolved") {
    return applyApproval(timeline, event.assistantId, event.approval);
  }

  // 计划不是 Timeline 的一部分：它是会话级状态（一个会话一份），由 ChatPage 单独
  // 持有并按 revision 覆盖。这里显式返回原时间线，避免被当成「漏处理」。
  if (event.type === "plan.updated") {
    return timeline;
  }

  // 用量更正同理：会话级数据，由 ChatPage 直接写入 session.usage。
  if (event.type === "usage.updated") {
    return timeline;
  }

  return timeline;
}

/**
 * 把审批状态写到对应的工具卡片上。
 *
 * 定位顺序：事件自带的 assistantId（最可靠，服务端已盖章）→ 按 toolCallId 反查
 * 已在时间线里的卡片 → 最后一个助手块。**绝不按「猜」新建一张无归属的卡片**：
 * 卡片自带工具名与参数，落在哪一段对话下面都不影响它说清「在批准什么」，
 * 但它必须挂在某个助手块里，否则刷新后就成了孤儿。
 */
function applyApproval(
  timeline: Timeline,
  assistantID: string | undefined,
  payload: ApprovalPayload,
): Timeline {
  const approval = toApproval(payload);
  const target = assistantID
    || findAssistantByToolCall(timeline, payload.toolCallId)
    || lastAssistantID(timeline);
  if (!target) {
    return timeline;
  }
  return updateAssistant(timeline, target, (assistant) => {
    const index = assistant.blocks.findIndex((block) => block.id === payload.toolCallId);
    if (index < 0) {
      // 重放从审批事件开始（前面的 tool.started 已被裁剪）：补一张卡片，
      // 否则面板无处可挂，用户看到「有个操作在等确认」却没有按钮。
      return {
        ...assistant,
        blocks: [...assistant.blocks, {
          type: "tool",
          id: payload.toolCallId,
          toolName: payload.toolName,
          arguments: payload.arguments ?? "",
          status: "running",
          approval,
        }],
      };
    }
    const blocks = [...assistant.blocks];
    const block = blocks[index];
    if (block.type !== "tool") {
      return assistant;
    }
    blocks[index] = {
      ...block,
      toolName: payload.toolName || block.toolName,
      approval,
    };
    return { ...assistant, blocks };
  });
}

function toApproval(payload: ApprovalPayload): ToolApproval {
  return {
    approvalId: payload.approvalId,
    status: payload.status || "pending",
    capabilities: payload.capabilities ?? [],
    ...(payload.expiresAt ? { expiresAt: payload.expiresAt } : {}),
  };
}

function findAssistantByToolCall(timeline: Timeline, toolCallID: string): string | undefined {
  for (const block of timeline.blocks) {
    if (block.type !== "assistant") continue;
    if (block.blocks.some((content) => content.id === toolCallID)) {
      return block.id;
    }
  }
  return undefined;
}

function lastAssistantID(timeline: Timeline): string | undefined {
  const last = [...timeline.blocks].reverse().find((block) => block.type === "assistant");
  return last?.id;
}

export function errorTimelineEvent(message: string): ErrorBlock {
  return {
    type: "error",
    id: crypto.randomUUID(),
    message,
    createdAt: new Date().toISOString(),
  };
}

function readFrame(frame: string, onEvent: (event: ChatStreamEvent) => void) {
  const data = frame
    .split("\n")
    .filter((line) => line.startsWith("data: "))
    .map((line) => line.slice("data: ".length))
    .join("\n");
  if (!data) {
    return;
  }

  onEvent(JSON.parse(data) as ChatStreamEvent);
}

// 把后端给出的本轮耗时写到对应的助手块上。
// 实时事件用 exclude_none（无耗时即缺字段），历史投影则可能给 null——两者都视为"没有"。
function stampDuration(
  timeline: Timeline,
  assistantID?: string,
  durationMs?: number | null,
): Timeline {
  if (!assistantID || typeof durationMs !== "number") {
    return timeline;
  }
  return updateAssistant(timeline, assistantID, (assistant) => ({ ...assistant, durationMs }));
}

// 把完成证据等级写到对应的助手块上。
// 缺字段（旧后端）或非法值一律视为「未知」，宁可不显示，也不给错误的结论。
// metadata_only = 只更新了计划之类的元数据：既不是「有产出」，也不是「没动过」。
const COMPLETION_EVIDENCE_VALUES = ["mutated", "external", "metadata_only", "read_only", "none"] as const;

function stampEvidence(
  timeline: Timeline,
  assistantID?: string,
  evidence?: string | null,
): Timeline {
  if (!assistantID || typeof evidence !== "string") {
    return timeline;
  }
  if (!COMPLETION_EVIDENCE_VALUES.includes(evidence as (typeof COMPLETION_EVIDENCE_VALUES)[number])) {
    return timeline;
  }
  return updateAssistant(timeline, assistantID, (assistant) => ({
    ...assistant,
    completionEvidence: evidence as CompletionEvidence,
  }));
}

// 把任务语义写到对应的助手块上。
// 只认 complete / incomplete：未知（旧后端、旧消息）不写，界面不显示任何标记 ——
// 把「不知道」显示成「没做完」是凭空指控。原因（endReason）只留给文案用，
// 未知原因由展示层走兜底，绝不影响这里是否标记。
const RUN_OUTCOME_VALUES = ["complete", "incomplete"] as const;

function stampOutcome(
  timeline: Timeline,
  assistantID?: string,
  outcome?: string | null,
  endReason?: string | null,
): Timeline {
  if (!assistantID || typeof outcome !== "string") {
    return timeline;
  }
  if (!RUN_OUTCOME_VALUES.includes(outcome as (typeof RUN_OUTCOME_VALUES)[number])) {
    return timeline;
  }
  return updateAssistant(timeline, assistantID, (assistant) => ({
    ...assistant,
    outcome: outcome as RunOutcome,
    endReason: typeof endReason === "string" ? endReason : null,
  }));
}

function updateAssistant(
  timeline: Timeline,
  assistantID: string,
  update: (assistant: AssistantBlock) => AssistantBlock,
): Timeline {
  const index = timeline.blocks.findIndex(
    (block) => block.type === "assistant" && block.id === assistantID,
  );
  if (index < 0) {
    // 没有 run.started 也要能落地：断流重连时补发可能从任意一条事件开始
    // （`after` 指向的内容早于本页记录、或日志被裁剪过）。此前这里直接返回
    // 原时间线，等于把整段回复连同后续工具卡片一起丢掉——表现就是「刷新后
    // 一片空白，等任务结束再刷新才出现」。
    const created: AssistantBlock = update({
      type: "assistant",
      id: assistantID,
      createdAt: new Date().toISOString(),
      blocks: [],
    });
    return { blocks: [...timeline.blocks, created] };
  }
  return {
    blocks: timeline.blocks.map((block, position) =>
      position === index && block.type === "assistant" ? update(block) : block,
    ),
  };
}
