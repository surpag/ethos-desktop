// M0 阶段渲染进程不需要主进程能力：所有数据都走 HTTP（BFF → sidecar）。
// 保留这个文件是为了让 webPreferences.preload 指向一个真实存在的文件，
// 后续要加「打开本地目录」「系统通知」时在这里扩展。
export {};
