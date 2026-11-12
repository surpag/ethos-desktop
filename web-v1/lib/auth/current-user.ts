/**
 * 当前用户 ID 的**唯一**来源（仅服务端）。
 *
 * 桌面端是本机单用户，没有登录流程，固定 `LOCAL_USER_ID`。
 *
 * 约定：所有 BFF route 必须调用 `currentUserId()`，不得自己拼一个用户标识。
 * 保留函数形状（async、返回字符串）而不是到处写常量，是为了让「身份从哪来」
 * 始终只有一个答案——将来若真的加上账号体系，改这一个文件即可。
 */

export const LOCAL_USER_ID = "local-user";

export async function currentUserId(): Promise<string> {
  return LOCAL_USER_ID;
}
