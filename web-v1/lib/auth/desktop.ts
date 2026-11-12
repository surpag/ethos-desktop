/**
 * 桌面 / 云端的**唯一**模式判断。
 *
 * 与 `current-user.ts` 分开的原因：那个文件 import 了 `@clerk/nextjs/server`，
 * 客户端组件无法引用。这里保持零依赖，所以 server 与 client 都能用。
 *
 * 两个变量都要看：
 * - `ETHOS_DESKTOP`          由 Electron 主进程注入，服务端（route / layout）可见；
 * - `NEXT_PUBLIC_ETHOS_DESKTOP` 在构建时内联进客户端 bundle，值必须相同。
 *
 * 构建桌面版时必须两者同时设为 "1"，否则会出现「服务端认为在桌面端、
 * 客户端认为在云端」——表现为页面一片空白（ClerkProvider 缺失）。
 */

export const DESKTOP_USER_ID = "local-user";

export function isDesktopMode(): boolean {
  return (
    process.env.NEXT_PUBLIC_ETHOS_DESKTOP === "1" ||
    process.env.ETHOS_DESKTOP === "1"
  );
}
