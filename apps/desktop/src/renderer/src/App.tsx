import { useEffect, useRef, type JSX } from 'react'
import { getBridge } from './services/bridge'
import { useApp } from './state/store'
import { Center } from './components/Center'
import { ApiKeyDialog } from './components/ApiKeyDialog'
import { AuditDialog } from './components/AuditDialog'
import { ClaimsQueueDialog } from './components/ClaimsQueueDialog'
import { ContractStartDialog } from './components/ContractStartDialog'
import { DeleteCaseDialog } from './components/DeleteCaseDialog'
import { DocGenStartDialog } from './components/DocGenStartDialog'
import { NewCaseDialog } from './components/NewCaseDialog'
import { SkillDialog } from './components/SkillDialog'
import { MemoryDialog } from './components/MemoryDialog'
import { RedactionDialog } from './components/RedactionDialog'
import { StrategyStartDialog } from './components/StrategyStartDialog'
import { RightPanel } from './components/RightPanel'
import { Sidebar } from './components/Sidebar'
import { Splitter } from './components/Splitter'
import { TitleBar } from './components/TitleBar'
import { Toasts } from './components/Toasts'
import { UnlockDialog } from './components/UnlockDialog'

export default function App(): JSX.Element {
  const { setOpenPop, closeArt, fullscreen, setFullscreen, toggleLeft, toggleRight, toast, initFromCore } = useApp()
  const initialized = useRef(false)

  // 启动序列：Core ready → ensureWorkspace → 案件/文档加载（真实落库数据）
  useEffect(() => {
    const bridge = getBridge()
    if (!bridge) return
    let off: (() => void) | null = null
    const boot = (): void => {
      if (initialized.current) return
      initialized.current = true
      void initFromCore()
    }
    // 事件可能早于 Renderer 订阅到达，轮询兜底
    const poll = setInterval(() => {
      void bridge.core.status().then((s) => {
        if (s.state === 'ready') {
          clearInterval(poll)
          boot()
        }
      })
    }, 1200)
    setTimeout(() => clearInterval(poll), 30_000)
    void bridge.core.status().then((s) => {
      if (s.state === 'ready') boot()
    })
    off = bridge.core.onStatus((s) => {
      if (s.state === 'degraded') toast(`Python Core：${s.detail}（本地阅读不受影响）`)
      if (s.state === 'ready') {
        // Core 崩溃后被主进程自动拉起时是全新进程，数据库单例尚未打开；
        // 不重新 ensure，后续 RPC 全会报 workspace not initialized。
        if (initialized.current) {
          toast('Python Core 已重连')
          // 新进程的数据库单例尚未打开：重新走一遍初始化（含工作空间回填与案件重载）；
          // 已有当前案件时 initFromCore 不会重置案件视图
          void useApp.getState().initFromCore()
          return
        }
        toast('Python Core 已连接')
        boot()
      }
    })
    return () => {
      clearInterval(poll)
      if (off) off()
    }
  }, [toast, initFromCore])

  // 全局键盘：Esc 层级（弹层 → 产物弹窗 → 退出全屏 → 关闭新建案件），Alt+[ / ] 切换左右栏
  useEffect(() => {
    const onKey = (e: KeyboardEvent): void => {
      const s = useApp.getState()
      if (e.key === 'Escape') {
        if (s.openPop) setOpenPop(null)
        else if (s.creatingCase) s.setCreatingCase(false)
        else if (s.deletingCase) s.setDeletingCase(null)
        else if (s.claimsQueueOpen) s.setClaimsQueueOpen(false)
        else if (s.auditOpen) s.setAuditOpen(false)
        else if (s.apiKeyDialogOpen) s.setApiKeyDialogOpen(false)
        else if (s.unlockTarget) s.closeUnlock()
        else if (s.redactionDialogOpen) s.setRedactionDialogOpen(false)
        // 技能库弹窗的 Esc 由组件内 useDialogA11y 分层处理（先关删除确认再关弹窗）
        else if (s.memoryOpen) s.setMemoryOpen(false)
        else if (s.artOpen) closeArt(true)
        else if (s.fullscreen) setFullscreen(false)
      }
      if (e.altKey && e.key === '[') {
        e.preventDefault()
        toggleLeft()
      }
      if (e.altKey && e.key === ']') {
        e.preventDefault()
        toggleRight()
      }
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [setOpenPop, closeArt, fullscreen, setFullscreen, toggleLeft, toggleRight])

  // 点击空白处关闭产物弹窗（固定时除外）
  useEffect(() => {
    const onClick = (e: MouseEvent): void => {
      const t = e.target as HTMLElement
      if (t.closest('.art-pop') || t.closest('[aria-label="文档概览"]')) return
      closeArt()
    }
    document.addEventListener('click', onClick)
    return () => document.removeEventListener('click', onClick)
  }, [closeArt])

  return (
    <div className="app">
      <Sidebar />
      <Splitter side="left" />
      <Center />
      <Splitter side="right" />
      <RightPanel />
      <TitleBar />
      <Toasts />
      <NewCaseDialog />
      <DeleteCaseDialog />
      <ClaimsQueueDialog />
      <AuditDialog />
      <ApiKeyDialog />
      <RedactionDialog />
      <SkillDialog />
      <MemoryDialog />
      <ContractStartDialog />
      <StrategyStartDialog />
      <DocGenStartDialog />
      <UnlockDialog />
    </div>
  )
}
