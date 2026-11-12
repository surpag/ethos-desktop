/**
 * 当前用户 ID 的**唯一**来源（仅服务端）。
 *
 * 桌面端（`ETHOS_DESKTOP=1`）是本机单用户，没有登录流程，固定 `local-user`。
 * 云端仍走 Clerk。
 *
 * 约定：所有 BFF route 必须调用 `currentUserId()`，**不得**直接调用 Clerk 的
 * `auth()`。这样「桌面 / 云端」的分歧只存在于这一个文件里；否则 51 个调用点
 * 各自持有一份分支，改一次要改 51 处，且必然漏。
 */

import { auth } from "@clerk/nextjs/server";

import { DESKTOP_USER_ID, isDesktopMode } from "./desktop";

export { DESKTOP_USER_ID, isDesktopMode };

export async function currentUserId(): Promise<string | null> {
  if (isDesktopMode()) return DESKTOP_USER_ID;
  const { userId } = await auth();
  return userId;
}
