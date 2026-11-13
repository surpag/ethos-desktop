import { execFile, spawn, type ChildProcess } from "node:child_process";
import fs from "node:fs";
import net from "node:net";
import path from "node:path";

export interface ExitInfo {
  label: string;
  code: number | null;
  signal: NodeJS.Signals | null;
}

export interface StartOptions {
  cwd: string;
  env: NodeJS.ProcessEnv;
  /** 子进程日志前缀，便于在同一个终端里区分来源。 */
  label: string;
  /**
   * 命令行里一定会出现的片段。
   *
   * 为崩溃回收准备：孤儿进程只靠 pid 认不回来——pid 会被系统复用，而用户
   * 自己在跑 Python。误杀一个正在跑的 `python.exe` 比残留一个占着库锁的
   * 进程更糟，所以回收前要用命令行片段确认「这还是我上次起的那个」。
   */
  signature: string;
  /** 子进程自己挂掉时回调。主动 `stop()` 不会触发。 */
  onUnexpectedExit?: (info: ExitInfo) => void;
  /** 传了才记账。崩溃后能回收，靠的就是这份记录。 */
  registry?: ChildRegistry;
}

export interface ManagedProcess {
  readonly label: string;
  readonly pid: number | undefined;
  stop(): Promise<void>;
}

interface ChildRecord {
  pid: number;
  label: string;
  signature: string;
}

/**
 * 子进程 PID 台账。
 *
 * 语义刻意做得很窄：**文件存在 = 上次没有走正常退出**。正常路径下 stop()
 * 会把自己那条删掉，两条都删完文件也就没了。所以启动时看到文件，就说明
 * 上次 Electron 是被强杀 / 崩溃的，里面的 pid 有可能是还活着的孤儿。
 */
export class ChildRegistry {
  private readonly file: string;
  private records: ChildRecord[] = [];

  constructor(file: string) {
    this.file = file;
  }

  add(record: ChildRecord): void {
    this.records = this.records.filter((item) => item.pid !== record.pid);
    this.records.push(record);
    this.flush();
  }

  remove(pid: number | undefined): void {
    if (pid === undefined) return;
    this.records = this.records.filter((item) => item.pid !== pid);
    this.flush();
  }

  /**
   * 回收上次遗留的孤儿。
   *
   * 三种情况都当作「不是我的」跳过：进程已不在、pid 被复用给了别的程序、
   * 命令行读不到（权限不足等）。宁可留一个孤儿、让它报出 `database is locked`
   * ——那个错误至少指向真实原因——也不要盲杀。
   */
  async reapStale(): Promise<ChildRecord[]> {
    const stale = this.readStale();
    const killed: ChildRecord[] = [];
    for (const record of stale) {
      if (!isAlive(record.pid)) continue;
      if (!(await commandMatches(record.pid, record.signature))) continue;
      await killTree(record.pid);
      killed.push(record);
    }
    this.records = [];
    this.flush();
    return killed;
  }

  private readStale(): ChildRecord[] {
    let raw: string;
    try {
      raw = fs.readFileSync(this.file, "utf8");
    } catch {
      return [];
    }
    try {
      const parsed: unknown = JSON.parse(raw);
      if (!Array.isArray(parsed)) return [];
      return parsed.filter(isChildRecord);
    } catch {
      // 文件损坏（比如上次写到一半被强杀）：无从判断，丢掉即可。
      return [];
    }
  }

  private flush(): void {
    if (this.records.length === 0) {
      try {
        fs.unlinkSync(this.file);
      } catch {
        // 本来就不在，正常。
      }
      return;
    }
    fs.mkdirSync(path.dirname(this.file), { recursive: true });
    // 先写临时文件再 rename：避免「写到一半被强杀」留下半截 JSON。
    const temp = `${this.file}.tmp`;
    fs.writeFileSync(temp, JSON.stringify(this.records), "utf8");
    fs.renameSync(temp, this.file);
  }
}

function isChildRecord(value: unknown): value is ChildRecord {
  if (typeof value !== "object" || value === null) return false;
  const record = value as Record<string, unknown>;
  return (
    typeof record.pid === "number" &&
    Number.isInteger(record.pid) &&
    typeof record.label === "string" &&
    typeof record.signature === "string"
  );
}

/** pid 是否还活着。EPERM 说明进程在、只是不归我们管——按活着算。 */
function isAlive(pid: number): boolean {
  try {
    process.kill(pid, 0);
    return true;
  } catch (error) {
    return (error as NodeJS.ErrnoException).code === "EPERM";
  }
}

/** 取进程命令行。拿不到就返回空串，调用方据此认定「不匹配」。 */
async function readCommandLine(pid: number): Promise<string> {
  if (process.platform === "win32") {
    const script = `Get-CimInstance Win32_Process -Filter "ProcessId=${pid}" | ForEach-Object { $_.CommandLine }`;
    return run("powershell", ["-NoProfile", "-NonInteractive", "-Command", script]);
  }
  return run("ps", ["-p", String(pid), "-o", "command="]);
}

async function commandMatches(pid: number, signature: string): Promise<boolean> {
  const commandLine = await readCommandLine(pid);
  return commandLine.trim() !== "" && commandLine.includes(signature);
}

async function killTree(pid: number): Promise<void> {
  if (process.platform === "win32") {
    // /T 是必须的：python 与 node 都会再派生子进程，只杀父进程会留下继续占库锁的孩子。
    await run("taskkill", ["/pid", String(pid), "/T", "/F"]);
    return;
  }
  try {
    process.kill(pid, "SIGKILL");
  } catch {
    // 已经没了。
  }
}

/** 跑一个外部命令并收集 stdout；失败也返回已拿到的部分，不抛错。 */
function run(command: string, args: string[]): Promise<string> {
  return new Promise((resolve) => {
    execFile(command, args, { windowsHide: true, timeout: 5000 }, (error, stdout) => {
      resolve(error && !stdout ? "" : stdout);
    });
  });
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

  let stopping = false;

  if (child.pid !== undefined) {
    options.registry?.add({ pid: child.pid, label: options.label, signature: options.signature });
  }

  const prefix = `[${options.label}]`;
  const forward = (chunk: Buffer, stream: NodeJS.WriteStream) => {
    for (const line of chunk.toString().split(/\r?\n/)) {
      if (line.trim()) stream.write(`${prefix} ${line}\n`);
    }
  };
  child.stdout?.on("data", (chunk: Buffer) => forward(chunk, process.stdout));
  child.stderr?.on("data", (chunk: Buffer) => forward(chunk, process.stderr));

  child.on("exit", (code, signal) => {
    options.registry?.remove(child.pid);
    if (stopping) return;
    process.stderr.write(`${prefix} 退出，code=${code} signal=${signal}\n`);
    // 交给主进程决定怎么呈现。「崩了但没人知道」比崩了本身更坏：
    // 托盘常驻时窗口往往是隐藏的，用户会以为任务还在跑。
    options.onUnexpectedExit?.({ label: options.label, code, signal });
  });

  return {
    label: options.label,
    get pid() {
      return child.pid;
    },
    stop: () =>
      new Promise<void>((resolve) => {
        stopping = true;
        options.registry?.remove(child.pid);
        if (child.exitCode !== null || child.signalCode !== null || child.pid === undefined) {
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

/**
 * 轮询直到目标 URL 返回 2xx/3xx。
 *
 * `signal` 用于「子进程已经死了就别再等」：启动期后端挂掉时，等满 60 秒再报错
 * 的表现是「双击后黑屏一分钟」，而失败其实在第一秒就已经确定了。
 */
export async function waitForHttp(url: string, timeoutMs = 60_000, signal?: AbortSignal): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  let lastError: unknown = null;

  while (Date.now() < deadline) {
    if (signal?.aborted) {
      throw new Error(`等待服务就绪期间进程已退出：${url}`);
    }
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
