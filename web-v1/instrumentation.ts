// Next 服务进程启动钩子：给所有 BFF → Runtime 的请求注入入站令牌。
//
// 位置很重要——放在这里而不是各个 route 里，是为了让「新加的路由」默认就是
// 带令牌的。漏一个路由的表现是那条接口 401，而漏在 route 里时根本不会被发现。
//
// 用**静态** import：动态 `import()` 会在打包后留下一条运行时解析路径，
// 而这条链一旦在 standalone 里解析不到，整站所有接口都会 401——这是最坏的
// 失败形态，不值得为省一次 import 去赌。
import { installRuntimeToken } from "./lib/server/runtime-token.ts";

export async function register(): Promise<void> {
  // 只在 Node 运行时装。判据写成"排除 edge"而不是"等于 nodejs"：
  // `NEXT_RUNTIME` 在极少数启动路径上还没赋值，写成等值判断会让钩子静默不执行，
  // 而它不执行的后果是**所有** BFF 转发 401——用一个更宽的条件换掉一个整站故障面。
  if (process.env.NEXT_RUNTIME === "edge") return;
  installRuntimeToken();
}
