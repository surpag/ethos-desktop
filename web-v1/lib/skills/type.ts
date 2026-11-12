// Skills 列表的浏览器契约。
//
// 单层结构：磁盘上有什么就列什么。没有 system / custom 之分，
// 也没有 shadows / conflicts——那些是「多层覆盖」才需要的产物。

export type SkillListItem = {
  name: string;
  description: string;
  /** 沙箱内相对路径，如 `skills/daily-summary`。 */
  path: string;
};

export type SkillsResponse = {
  skills: SkillListItem[];
  /** `ok` / `partial`（有条目解析失败）/ `missing`（目录还不存在，不是错误） */
  status: string;
  reason: string;
};
