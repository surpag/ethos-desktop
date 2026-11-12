import { spawn, type ChildProcess } from "node:child_process";
import net from "node:net";

export interface StartOptions {
  cwd: string;
  env: NodeJS.ProcessEnv;
  /** 子进程日志前缀，便于在同一个终端里区分来源。 */
  label: string;
}

export interface ManagedProcess {
  readonly label: string;
  readonly pid: number | undefined;
  stop(): Promise<void>;
}

/**
 * 取一个空闲端口。
 *
 * 先 bind(0) 让内核分配，再立刻关闭。理论上存在「关闭到子进程 bind 之间被
 * 别人抢走」的竞态，但窗口极短；相比写死 8080，它换来的是「不和用户自己
 * 跑的后端打架」。真要彻底消除竞态，需要子进程把实际端口写回 stdout 再解析，
 * 那会让启动流程依赖日志格式，代价更高。
 */
export function findFreePort(): Promise<number> {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.unref();
    server.on("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const address = server.address();
      if (typeof address === "string" || address === null) {
        server.close(() => reject(new Error("无法分配端口")));
        return;
      }
      const { port } = address;
      server.close(() => resolve(port));
    });
  });
}

export function startProcess(command: string, args: string[], options: StartOptions): ManagedProcess {
  const child: ChildProcess = spawn(command, args, {
    cwd: options.cwd,
    env: options.env,
    stdio: ["ignore", "pipe", "pipe"],
    windowsHide: true,
  });

  const prefix = `[${options.label}]`;
  const forward = (chunk: Buffer, stream: NodeJS.WriteStream) => {
    for (const line of chunk.toString().split(/\r?\n/)) {
      if (line.trim()) stream.write(`${prefix} ${line}\n`);
    }
  };
  child.stdout?.on("data", (chunk: Buffer) => forward(chunk, process.stdout));
  child.stderr?.on("data", (chunk: Buffer) => forward(chunk, process.stderr));

  child.on("exit", (code, signal) => {
    if (code !== 0 && signal !== "SIGTERM") {
      process.stderr.write(`${prefix} 退出，code=${code} signal=${signal}\n`);
    }
  });

  return {
    label: options.label,
    get pid() {
      return child.pid;
    },
    stop: () =>
      new Promise<void>((resolve) => {
        if (child.exitCode !== null || child.pid === undefined) {
          resolve();
          return;
        }
        const pid = child.pid;
        const done = () => resolve();
        child.once("exit", done);

        if (process.platform === "win32") {
          // python / node 都可能再派生子进程；只 kill 父进程会留下孤儿继续占着库锁。
          spawn("taskkill", ["/pid", String(pid), "/T", "/F"], { stdio: "ignore", windowsHide: true });
        } else {
          child.kill("SIGTERM");
        }

        // 兜底：5 秒内没退就放弃等待，避免退出流程被挂住。
        setTimeout(done, 5000).unref();
      }),
  };
}

/** 轮询直到目标 URL 返回 2xx/3xx。 */
export async function waitForHttp(url: string, timeoutMs = 60_000): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  let lastError: unknown = null;

  while (Date.now() < deadline) {
    try {
      const response = await fetch(url, { signal: AbortSignal.timeout(2000) });
      if (response.ok) return;
      lastError = new Error(`HTTP ${response.status}`);
    } catch (error) {
      lastError = error;
    }
    await new Promise((resolve) => setTimeout(resolve, 300));
  }

  throw new Error(`等待服务就绪超时：${url}（最后错误：${String(lastError)}）`);
}
