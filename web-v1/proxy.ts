import { clerkMiddleware } from "@clerk/nextjs/server";
import { NextResponse } from "next/server";

const desktop = process.env.ETHOS_DESKTOP === "1";

// 桌面端：本机单用户，没有 Clerk 会话。且缺失 publishable key 时
// clerkMiddleware() 会在构造阶段抛错，所以这里必须**短路求值**——
// 三元只在 desktop 为 false 时才真正调用 clerkMiddleware()。
// 页面与 BFF 的用户身份统一来自 `lib/auth/current-user.ts`。
const middleware = desktop ? () => NextResponse.next() : clerkMiddleware();

export default middleware;

export const config = {
  matcher: [
    "/((?!_next|[^?]*\\.(?:html?|css|js(?!on)|jpe?g|webp|png|gif|svg|ttf|woff2?|ico|csv|docx?|xlsx?|zip|webmanifest)).*)",
    "/(api|trpc)(.*)",
    "/__clerk/:path*",
  ],
};
