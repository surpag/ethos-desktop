import { ClerkProvider } from "@clerk/nextjs";
import type { Metadata } from "next";
import "@fontsource-variable/jetbrains-mono/wght.css";
import "@fontsource-variable/noto-sans-sc/wght.css";

import { ThemeProvider } from "@/components/theme-provider";
import { isDesktopMode } from "@/lib/auth/current-user";

import "./globals.css";

export const metadata: Metadata = {
  title: "ETHOS",
  description: "ETHOS Agent Platform",
  icons: {
    icon: "/icon.svg",
  },
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  const content = <ThemeProvider>{children}</ThemeProvider>;
  // 桌面端是本机单用户，没有 Clerk 会话；包上 ClerkProvider 会在缺 key 时报错。
  // 与 `lib/auth/current-user.ts` 同一个判断依据，避免两处语义漂移。
  const body = isDesktopMode() ? content : <ClerkProvider>{content}</ClerkProvider>;

  return (
    <html
      lang="zh-CN"
      className="h-full antialiased"
      suppressHydrationWarning
    >
      <body className="min-h-full bg-canvas text-ink">{body}</body>
    </html>
  );
}
