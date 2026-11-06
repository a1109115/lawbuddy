import { spawn, type ChildProcessWithoutNullStreams } from 'node:child_process'
import { randomUUID } from 'node:crypto'
import { EventEmitter } from 'node:events'
import { app } from 'electron'
import { dirname, join } from 'node:path'
import { existsSync, createWriteStream, type WriteStream } from 'node:fs'
import type { AgentEventNotification, CoreStatus } from '@lawbuddy/contracts'

interface PendingCall {
  resolve: (v: unknown) => void
  reject: (e: Error) => void
  timer: NodeJS.Timeout
  /** 发起时间：心跳据此判断 Core 是否正忙于长任务（导入/解析），避免误杀 */
  startedAt: number
  timeoutMs: number
}

const HEARTBEAT_MS = 5_000
const CALL_TIMEOUT_MS = 10_000
const HANDSHAKE_TIMEOUT_MS = 60_000
const MAX_RESTART = 3
/**
 * 心跳宽限：存在长任务 RPC（大文件导入、批量解析）在途时，ping 排队是正常现象，
 * 不能据此判定 Core 失联。超过该宽限仍无响应才视为真挂死。
 */
const BUSY_GRACE_MS = 10 * 60_000

/**
 * Python Core 进程管理（技术方案 §2.2 / §17）：
 * - 父子进程 stdio 承载换行分隔的 JSON-RPC 2.0；
 * - 会话令牌在启动后通过 stdin 握手传递，不进命令行 / 环境变量 / 日志；
 * - 心跳 + 崩溃有限重启；
 * - Core 不可用时应用降级运行（本地 UI 不受影响）。
 */
export class CoreProcess extends EventEmitter {
  private proc: ChildProcessWithoutNullStreams | null = null
  private token = randomUUID()
  private seq = 1
  private pending = new Map<number, PendingCall>()
  private buffer = ''
  private status: CoreStatus = { state: 'starting', detail: '正在启动 Python Core…' }
  private heartbeat: NodeJS.Timeout | null = null
  private restartCount = 0
  private stoppedByUser = false
  private stderrLog: WriteStream | null = null

  constructor(private onStatus: (s: CoreStatus) => void) {
    super()
  }

  getStatus(): CoreStatus {
    return this.status
  }

  /** 供 Main IPC 转发使用的 RPC 调用：等待 Core ready 后转发（技术方案 §15）。 */
  async rpc(method: string, params: Record<string, unknown>, timeoutMs = 30_000): Promise<unknown> {
    await this.waitReady(timeoutMs)
    return this.call(method, { ...params, _token: this.token }, timeoutMs)
  }

  /** 等待 Core 进入 ready 状态（握手完成），超时抛错。 */
  private waitReady(timeoutMs: number): Promise<void> {
    if (this.status.state === 'ready') return Promise.resolve()
    return new Promise((resolveP, rejectP) => {
      const deadline = Date.now() + timeoutMs
      const timer = setInterval(() => {
        if (this.status.state === 'ready') {
          clearInterval(timer)
          resolveP()
        } else if (Date.now() > deadline) {
          clearInterval(timer)
          rejectP(new Error(`Python Core 未就绪（${this.status.detail}）`))
        }
      }, 150)
    })
  }

  start(): void {
    this.stoppedByUser = false
    // 防止旧实例泄漏：启动前先回收
    if (this.proc) {
      try {
        this.proc.kill()
      } catch {
        /* 已退出则忽略 */
      }
      this.proc = null
    }
    // 打包产物：spawn PyInstaller 冻结的 lawbuddy-core.exe（免装 Python，开箱即用）；
    // 开发模式：spawn `python -m lawbuddy`（venv 依赖见 requirements.txt）
    const frozen = resolveFrozenCore()
    const spawnEnv = {
      ...process.env,
      PYTHONUNBUFFERED: '1',
      PYTHONIOENCODING: 'utf-8',
      // 设备级配置（模型服务商/模型目录/API Key、已装技能）固定放在
      // 应用数据目录，与案件工作空间解耦：切换/新建工作空间不会丢失这些配置
      LAWBUDDY_PROFILE_DIR: process.env.LAWBUDDY_PROFILE_DIR || join(app.getPath('userData'), 'profile')
    }
    if (frozen) {
      this.proc = spawn(frozen, [], {
        cwd: dirname(frozen),
        env: spawnEnv,
        stdio: ['pipe', 'pipe', 'pipe'],
        windowsHide: true
      })
    } else {
      const python = resolvePython()
      const cwd = resolveCoreCwd()
      if (!python || !cwd) {
        this.setStatus({
          state: 'degraded',
          detail: !python ? '未找到 Python 解释器，Agent 能力不可用' : '未找到 services/core 目录'
        })
        return
      }
      this.proc = spawn(python, ['-m', 'lawbuddy'], {
        cwd,
        // 令牌不写命令行与环境变量（技术方案 §2.2）
        env: spawnEnv,
        stdio: ['pipe', 'pipe', 'pipe'],
        windowsHide: true
      })
    }

    this.proc.stdout.on('data', (d: Buffer) => this.onStdout(d.toString('utf-8')))
    // Core stderr 落调试日志文件（不向 Renderer 透传，防敏感信息泄漏；用于本地诊断）
    this.stderrLog = createWriteStream(join(app.getPath('userData'), 'core-stderr.log'), { flags: 'a' })
    this.proc.stderr.on('data', (d: Buffer) => {
      try {
        this.stderrLog?.write(d)
      } catch {
        /* 日志写入失败不影响主流程 */
      }
    })
    this.proc.on('error', () => {
      this.setStatus({ state: 'degraded', detail: 'Python Core 启动失败' })
    })
    this.proc.on('exit', (code) => {
      this.proc = null
      this.stopHeartbeat()
      this.failAllPending(new Error(`core exited: ${code}`))
      if (!this.stoppedByUser) this.tryRestart()
    })

    this.setStatus({ state: 'starting', detail: '握手…' })
    void this.handshake()
  }

  stop(): void {
    this.stoppedByUser = true
    this.stopHeartbeat()
    if (this.stderrLog) {
      try {
        this.stderrLog.end()
      } catch {
        /* 忽略 */
      }
      this.stderrLog = null
    }
    if (this.proc) {
      void this.call('shutdown', {}, 2_000).catch(() => undefined)
      const p = this.proc
      setTimeout(() => p.kill(), 1_500)
      this.proc = null
    }
    this.setStatus({ state: 'stopped', detail: '已停止' })
  }

  async ping(): Promise<{ pong: boolean; latencyMs: number }> {
    const t0 = Date.now()
    try {
      await this.call('ping', {}, CALL_TIMEOUT_MS)
      return { pong: true, latencyMs: Date.now() - t0 }
    } catch {
      return { pong: false, latencyMs: -1 }
    }
  }

  private async handshake(): Promise<void> {
    try {
      const caps = (await this.call('core.handshake', { protocol: 1, token: this.token }, HANDSHAKE_TIMEOUT_MS)) as {
        python?: string
        langgraph?: boolean
        paddleocr?: boolean
      }
      this.restartCount = 0
      this.setStatus({
        state: 'ready',
        detail: 'Python Core 已连接',
        pythonVersion: caps.python,
        langgraph: !!caps.langgraph,
        paddleocr: !!caps.paddleocr,
        restartCount: this.restartCount
      })
      this.startHeartbeat()
    } catch {
      this.setStatus({ state: 'degraded', detail: 'Core 握手失败，正在重试…' })
      this.tryRestart()
    }
  }

  private startHeartbeat(): void {
    this.stopHeartbeat()
    let missed = 0
    this.heartbeat = setInterval(() => {
      void this.ping().then(({ pong }) => {
        if (pong) {
          missed = 0
          return
        }
        // Core 的 RPC 单线程顺序处理：导入/解析等长任务在途时，ping 只是在排队，
        // 并非失联。此时若继续累计丢失次数会误杀进程（表现为「导入很久然后失败重连」）。
        // 只要还存在未超时的在途调用，就按忙处理，不累计失联次数。
        if (this.hasInflightLongCall()) {
          missed = 0
          return
        }
        missed += 1
        if (missed >= 2) {
          this.stopHeartbeat()
          this.proc?.kill()
        }
      })
    }, HEARTBEAT_MS)
  }

  /** 是否存在仍在超时窗口内的在途调用（含长任务导入）。 */
  private hasInflightLongCall(): boolean {
    const now = Date.now()
    for (const p of this.pending.values()) {
      if (now - p.startedAt < Math.min(p.timeoutMs, BUSY_GRACE_MS)) return true
    }
    return false
  }

  private stopHeartbeat(): void {
    if (this.heartbeat) clearInterval(this.heartbeat)
    this.heartbeat = null
  }

  private tryRestart(): void {
    if (this.restartCount >= MAX_RESTART) {
      this.setStatus({ state: 'degraded', detail: 'Python Core 多次崩溃，已停止自动重启', restartCount: this.restartCount })
      return
    }
    this.restartCount += 1
    const delay = Math.min(2 ** this.restartCount * 1_000, 8_000)
    this.setStatus({ state: 'starting', detail: `Core 异常退出，${delay / 1000}s 后重启（第 ${this.restartCount} 次）…`, restartCount: this.restartCount })
    setTimeout(() => {
      if (!this.stoppedByUser) this.start()
    }, delay)
  }

  private call(method: string, params: Record<string, unknown>, timeoutMs: number): Promise<unknown> {
    if (!this.proc) return Promise.reject(new Error('core not running'))
    const id = this.seq++
    const line = JSON.stringify({ jsonrpc: '2.0', id, method, params: { ...params, _token: this.token } }) + '\n'
    return new Promise((resolveP, rejectP) => {
      const timer = setTimeout(() => {
        this.pending.delete(id)
        rejectP(new Error(`${method} timeout`))
      }, timeoutMs)
      this.pending.set(id, {
        resolve: resolveP as (v: unknown) => void,
        reject: rejectP,
        timer,
        startedAt: Date.now(),
        timeoutMs
      })
      this.proc!.stdin.write(line)
    })
  }

  private onStdout(chunk: string): void {
    this.buffer += chunk
    let idx: number
    while ((idx = this.buffer.indexOf('\n')) >= 0) {
      const line = this.buffer.slice(0, idx).trim()
      this.buffer = this.buffer.slice(idx + 1)
      if (!line) continue
      let msg: {
        id?: number
        result?: unknown
        error?: { message?: string }
        method?: string
        params?: AgentEventNotification
      }
      try {
        msg = JSON.parse(line) as typeof msg
      } catch {
        continue
      }
      if (typeof msg.id === 'number') {
        const p = this.pending.get(msg.id)
        if (!p) continue
        this.pending.delete(msg.id)
        clearTimeout(p.timer)
        if (msg.error) p.reject(new Error(msg.error.message ?? 'core error'))
        else p.resolve(msg.result)
      } else if (msg.method === 'agent.event' && msg.params) {
        // Core 主动推送的任务事件（REQ-AI-001）：转发 Main，轮询作为兜底
        this.emit('agent-event', msg.params)
      }
    }
  }

  private failAllPending(e: Error): void {
    for (const [, p] of this.pending) {
      clearTimeout(p.timer)
      p.reject(e)
    }
    this.pending.clear()
  }

  private setStatus(s: CoreStatus): void {
    this.status = s
    this.onStatus(s)
  }
}

function resolvePython(): string | null {
  if (process.env.LAWBUDDY_PYTHON && existsSync(process.env.LAWBUDDY_PYTHON)) return process.env.LAWBUDDY_PYTHON
  // 开发模式优先用 services/core/.venv：依赖（paddleocr/rapidocr 等）按 requirements.txt
  // 装在项目 venv，系统 PATH 的 python 通常是裸解释器——缺 rapidocr 会使 OCR 静默回退
  // Paddle 慢路径（实测 118 页卷宗 2 分钟 → ONNX 25 秒），缺其他依赖则模块导入失败
  const devVenv = join(app.getAppPath(), '..', '..', 'services', 'core', '.venv')
  const candidates =
    process.platform === 'win32'
      ? [join(devVenv, 'Scripts', 'python.exe')]
      : [join(devVenv, 'bin', 'python'), join(devVenv, 'bin', 'python3')]
  for (const candidate of candidates) {
    if (existsSync(candidate)) return candidate
  }
  return 'python'
}

/**
 * 打包模式：resources/core/lawbuddy-core.exe（PyInstaller onedir 冻结产物，
 * 由 electron-builder extraResources 复制）。构建脚本：services/core/lawbuddy-core.spec。
 * RapidTable 表格还原模型随包分发，路径由冻结入口自探测（lawbuddy/__main__.py），
 * 此处无需设环境变量。
 */
function resolveFrozenCore(): string | null {
  if (!app.isPackaged) return null
  const exe = join(process.resourcesPath, 'core', 'lawbuddy-core.exe')
  return existsSync(exe) ? exe : null
}

function resolveCoreCwd(): string | null {
  // 仅开发模式使用（打包模式 cwd = dirname(lawbuddy-core.exe)，见 start()）
  const dev = join(app.getAppPath(), '..', '..', 'services', 'core')
  return existsSync(dev) ? dev : null
}
