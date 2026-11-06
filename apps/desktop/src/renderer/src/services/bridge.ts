import type { LawBuddyBridge } from '@lawbuddy/contracts'

declare global {
  interface Window {
    lawbuddy?: LawBuddyBridge
  }
}

/** 获取 Preload 桥；在纯浏览器（开发调试）下返回 null，UI 自动降级为演示行为 */
export function getBridge(): LawBuddyBridge | null {
  return typeof window !== 'undefined' && window.lawbuddy ? window.lawbuddy : null
}

/**
 * 提取面向用户的错误文案。
 *
 * Electron 会把主进程异常包成 `Error invoking remote method 'x:y': Error: 真正原因`，
 * 内部 IPC 方法名属于实现细节，不应出现在界面或审计提示里。
 */
export function errorMessage(e: unknown): string {
  const raw = e instanceof Error ? e.message : String(e)
  const match = raw.match(/^Error invoking remote method '[^']*':\s*(?:[A-Za-z]*Error:\s*)?([\s\S]*)$/)
  return (match ? match[1] : raw).trim() || raw
}
