import { Check, ChevronDown, CircleAlert, LoaderCircle, ShieldAlert, Wrench, X } from "lucide-react";
import { memo, useState } from "react";

import type { ToolApproval, ToolBlock } from "@/lib/chat/type";

// 能力 → 人话。值是后端 Capability 的原样字符串（小写），认不出的原样显示：
// 后端新增一种能力时这里不必同步改，也不会把 unknown 藏起来。
const CAPABILITY_COPY: Record<string, string> = {
  read: "读取",
  write: "写入文件",
  exec: "执行命令",
  external: "产生外部影响",
  metadata: "更新计划",
};

export const ToolCard = memo(function ToolCard({ block }: { block: ToolBlock }) {
  const state = block.status === "running" ? "运行中" : block.status === "failed" ? "执行失败" : "已完成";
  const stateClass = block.status === "running"
    ? "bg-accent-soft text-accent"
    : block.status === "failed"
      ? "bg-danger-soft text-danger"
      : "bg-surface-hover text-muted";

  const StateIcon = block.status === "running" ? LoaderCircle : block.status === "failed" ? CircleAlert : Check;

  return (
    <div className="space-y-2">
      {/* 审批面板放在卡片**外面**：details 默认收起，放进去用户永远看不到有操作在等。 */}
      {block.approval && block.approval.status === "pending" && (
        <ApprovalPanel approval={block.approval} args={block.arguments} />
      )}
      <details className="group overflow-hidden rounded-xl border border-border bg-surface text-xs leading-5 shadow-sm shadow-black/[0.02]">
        <summary className="flex cursor-pointer list-none items-center gap-2.5 px-3 py-2.5 hover:bg-surface-subtle [&::-webkit-details-marker]:hidden">
          <span className="inline-flex size-7 shrink-0 items-center justify-center rounded-lg bg-surface-subtle text-muted"><Wrench className="size-3.5" /></span>
          <span className="min-w-0 flex-1">
            <span className="block truncate font-mono text-[11px] font-medium text-ink">{block.toolName}</span>
            <span className="block text-[10px] text-faint">工具调用</span>
          </span>
          <span className={`inline-flex shrink-0 items-center gap-1 rounded-full px-2 py-1 text-[10px] font-medium ${stateClass}`}><StateIcon className={`size-3 ${block.status === "running" ? "animate-spin" : ""}`} />{state}</span>
          <ChevronDown className="size-3.5 shrink-0 text-faint transition-transform group-open:rotate-180" />
        </summary>
        <div className="space-y-3 border-t border-border bg-surface-subtle p-3">
          <div>
            <p className="mb-1 text-[11px] font-medium text-muted">调用参数</p>
            <pre className="max-h-48 overflow-auto rounded-lg bg-surface p-2.5 font-mono whitespace-pre-wrap text-muted">{block.arguments}</pre>
          </div>
          {block.result !== undefined && (
            <div>
              <p className="mb-1 text-[11px] font-medium text-muted">工具结果</p>
              <pre className="max-h-80 overflow-auto rounded-lg bg-surface p-2.5 font-mono whitespace-pre-wrap text-ink">{block.result}</pre>
            </div>
          )}
        </div>
      </details>
    </div>
  );
});

/**
 * 审批决策面板。
 *
 * 状态只有服务端说了算：点完按钮不自行把卡片改成「已批准」，而是等
 * `approval.resolved` 事件——所以这里没有乐观更新，只有「已提交」。
 * 两个标签页同时点是常态，谁的点击先到由数据库裁决，抢输的那个收到 409。
 */
function ApprovalPanel({ approval, args }: { approval: ToolApproval; args: string }) {
  // busy 同时承担「按钮禁用」：一旦提交过就不再允许点。
  // 二次提交最坏只是 409，但界面上先后出现两个相反的决定（先批后拒）是误导。
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState("");

  async function decide(decision: "approve" | "deny", scope: "once" | "run") {
    if (busy) return;
    setBusy(true);
    setNote("");
    try {
      const response = await fetch(`/api/approvals/${encodeURIComponent(approval.approvalId)}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ decision, scope }),
      });
      if (response.ok) {
        // 等服务端的 resolved 事件把这张面板换掉；此时不能恢复按钮。
        setNote("已提交，等待服务端确认…");
        return;
      }
      if (response.status === 409) {
        setNote("这条操作已经被处理（可能在另一个标签页），正在同步状态…");
        return;
      }
      if (response.status === 404) {
        setNote("这条操作已失效（任务已经结束）。");
        return;
      }
      setBusy(false);
      setNote("提交失败，请重试。");
    } catch {
      setBusy(false);
      setNote("网络不可用，请重试。");
    }
  }

  // 参数可能很长（整段文件内容）：这里只给开头，完整内容在下面的卡片里展开看。
  const preview = args.length > 400 ? `${args.slice(0, 400)}…` : args;

  return (
    <div className="rounded-xl border border-warning/40 bg-warning-soft px-3.5 py-3 text-xs leading-5" role="alert">
      <div className="flex items-start gap-2.5">
        <ShieldAlert className="mt-0.5 size-4 shrink-0 text-warning" />
        <div className="min-w-0 flex-1">
          <p className="font-medium text-warning">这一步需要你确认后才会执行</p>
          {approval.capabilities.length > 0 && (
            <p className="mt-1 flex flex-wrap items-center gap-1.5">
              {approval.capabilities.map((capability) => (
                <span className="rounded bg-surface/70 px-1.5 py-0.5 text-[10px] font-medium text-ink" key={capability}>
                  {CAPABILITY_COPY[capability] ?? capability}
                </span>
              ))}
            </p>
          )}
          {preview && (
            <pre className="mt-2 max-h-32 overflow-auto rounded-lg bg-surface/80 p-2 font-mono text-[11px] whitespace-pre-wrap text-muted">{preview}</pre>
          )}
          <div className="mt-2.5 flex flex-wrap items-center gap-2">
            <button className="ui-button-primary h-8 px-3 text-xs" disabled={busy} onClick={() => void decide("approve", "once")} type="button">
              <Check className="size-3.5" />批准一次
            </button>
            <button className="ui-button-secondary h-8 px-3 text-xs" disabled={busy} onClick={() => void decide("approve", "run")} type="button">
              <Check className="size-3.5" />本次运行都允许
            </button>
            <button className="ui-button-danger h-8 px-3 text-xs" disabled={busy} onClick={() => void decide("deny", "once")} type="button">
              <X className="size-3.5" />拒绝
            </button>
            {busy && <LoaderCircle className="size-3.5 animate-spin text-warning" />}
          </div>
          <p className="mt-2 text-[11px] leading-5 text-warning/80">
            {note || "不操作的话，超时按拒绝处理，Agent 会继续用别的方式执行。批准只解除档位限制，敏感路径（密钥、凭据等）仍然会被拒绝。"}
          </p>
        </div>
      </div>
    </div>
  );
}
