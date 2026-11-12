#!/usr/bin/env node
/**
 * 构建桌面版前端并补齐 standalone 缺的两块静态资源。
 *
 * Next 的 `output: "standalone"` 只产出 server.js + 必需依赖，
 * **不会**复制 `.next/static` 与 `public`——缺了它们的表现是页面能打开但
 * 没有任何样式与字体（CSS 404），很容易被误判成"样式写错了"。
 *
 */
import { execSync } from "node:child_process";
import { cpSync, existsSync, rmSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const webDir = path.resolve(here, "..", "..", "web-v1");
const standalone = path.join(webDir, ".next", "standalone");

console.log("[build-web] web 目录:", webDir);

execSync("npm run build", {
  cwd: webDir,
  stdio: "inherit",
  env: { ...process.env },
});

if (!existsSync(path.join(standalone, "server.js"))) {
  console.error(`[build-web] 未找到 ${path.join(standalone, "server.js")}`);
  process.exit(1);
}

const copies = [
  [path.join(webDir, ".next", "static"), path.join(standalone, ".next", "static")],
  [path.join(webDir, "public"), path.join(standalone, "public")],
];

for (const [from, to] of copies) {
  if (!existsSync(from)) {
    console.log(`[build-web] 跳过（源不存在）: ${from}`);
    continue;
  }
  rmSync(to, { recursive: true, force: true });
  cpSync(from, to, { recursive: true });
  console.log(`[build-web] 已复制 → ${to}`);
}

console.log("[build-web] 完成");
