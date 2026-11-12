"use client";

import { FolderOpen, Sparkles } from "lucide-react";
import { useEffect, useState } from "react";

import type { SkillListItem, SkillsResponse } from "@/lib/skills/type";

// 展示当前数据目录里的 Skill。只有一层：`skills/<名字>/SKILL.md`。
export function Skills() {
  const [skills, setSkills] = useState<SkillsResponse | null>(null);
  const [message, setMessage] = useState("");

  useEffect(() => {
    async function load() {
      try {
        const response = await fetch("/api/skills", { cache: "no-store" });
        if (!response.ok) {
          setMessage("无法加载 Skills。");
          return;
        }
        setSkills(await response.json() as SkillsResponse);
      } catch {
        setMessage("无法连接 ETHOS Runtime。");
      }
    }
    void load();
  }, []);

  return (
    <section className="ui-surface overflow-hidden">
      <div className="flex items-start gap-3 border-b border-border px-5 py-4">
        <span className="inline-flex size-9 shrink-0 items-center justify-center rounded-xl bg-accent-soft text-accent"><Sparkles className="size-4" /></span>
        <div>
          <h2 className="text-sm font-semibold text-ink">Skills</h2>
          <p className="mt-1 text-xs leading-5 text-muted">
            Agent 会按需读取这些工作方法。目录 <span className="font-mono">skills/</span> 与沙箱里的
            <span className="font-mono"> /home/user/skills</span> 是同一个位置——放进去的文件下一轮就生效。
          </p>
        </div>
      </div>

      {message && <p className="p-5 text-sm text-muted">{message}</p>}
      {!message && !skills && <p className="p-5 text-sm text-muted">加载中…</p>}
      {skills && <div className="space-y-3 p-5">
        {skills.status === "missing" && (
          <p className="rounded-lg bg-surface-subtle px-3 py-4 text-sm text-muted">
            还没有 Skill 目录。让 Agent 建一个，或手动往 <span className="font-mono">skills/</span> 里放。
          </p>
        )}
        {skills.status === "partial" && (
          <p className="rounded-lg bg-surface-subtle px-3 py-4 text-xs leading-5 text-muted">
            部分 Skill 无法解析：{skills.reason}
          </p>
        )}
        {skills.skills.length === 0 && skills.status === "ok" && (
          <p className="rounded-lg bg-surface-subtle px-3 py-4 text-sm text-muted">目录是空的。</p>
        )}
        {skills.skills.length > 0 && (
          <div className="grid gap-2 sm:grid-cols-2">
            {skills.skills.map((skill) => <SkillCard key={skill.path || skill.name} skill={skill} />)}
          </div>
        )}
      </div>}
    </section>
  );
}

function SkillCard({ skill }: { skill: SkillListItem }) {
  return (
    <article className="rounded-xl border border-border bg-surface px-4 py-3.5 transition-colors hover:border-border-strong hover:bg-surface-subtle">
      <h4 className="font-mono text-xs font-semibold text-ink">{skill.name}</h4>
      <p className="mt-2 text-sm leading-6 text-muted">{skill.description}</p>
      {skill.path && (
        <p className="mt-2 flex items-center gap-1.5 font-mono text-[11px] text-faint">
          <FolderOpen className="size-3" />{skill.path}
        </p>
      )}
    </article>
  );
}
