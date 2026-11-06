import { useEffect, useRef, type JSX } from 'react'
import { ChatView } from './ChatView'
import { Dock } from './Dock'
import { Hero } from './Hero'
import { ModeTabs } from './ModeTabs'
import { ContractWorkspace } from './ContractWorkspace'
import { DocGenWorkspace } from './DocGenWorkspace'
import { useApp } from '../state/store'

/** 中间区宽度分级阈值（clientWidth，含左右 padding）：逐级收纳按钮，规则见 app.css 的 data-narrow-* 段 */
const NARROW_STEPS = [860, 760, 660, 560, 460, 380]

/** 中间主区：模式页签 + 功能条 + 舞台（Hero/对话）+ 输入坞 */
export function Center(): JSX.Element {
  const bodyRef = useRef<HTMLDivElement>(null)
  const { mode, currentCaseId } = useApp()
  const messages = useApp((s) => s.messages)
  const docgenDraftId = useApp((s) => s.docgenDraftId)

  // 宽度自适应：按中间区实际宽度写入 data-narrow-* 分级。
  // 不用 CSS 容器查询：contain:layout 会劫持 .pop 等 fixed 弹层的包含块，导致下拉定位错乱。
  useEffect(() => {
    const el = bodyRef.current
    if (!el) return
    const apply = (): void => {
      NARROW_STEPS.forEach((max, i) => {
        el.toggleAttribute(`data-narrow-${i + 1}`, el.clientWidth <= max)
      })
    }
    apply()
    const ro = new ResizeObserver(apply)
    ro.observe(el)
    return () => ro.disconnect()
  }, [])

  return (
    <main className="center">
      <div className="center-deco" aria-hidden="true">
        <div className="blob blob-1"></div>
        <div className="blob blob-2"></div>
        <svg className="deco-wave" viewBox="0 0 1200 260" preserveAspectRatio="none" height="260">
          <path d="M0 200 C 260 120, 480 240, 720 150 C 900 85, 1060 140, 1200 60" stroke="#d7e4fb" strokeWidth="1.5" fill="none" />
          <path d="M0 230 C 280 150, 500 265, 740 175 C 920 110, 1070 165, 1200 90" stroke="#e2ecfc" strokeWidth="1.5" fill="none" />
          <path d="M0 170 C 240 95, 460 210, 700 122 C 880 60, 1050 112, 1200 30" stroke="#e8f0fd" strokeWidth="1.5" fill="none" />
        </svg>
      </div>
      <div className="center-body" ref={bodyRef}>
        <ModeTabs />
        {currentCaseId && mode === 'ht' ? (
          <div className="stage cw-stage">
            <ContractWorkspace key={currentCaseId} />
          </div>
        ) : currentCaseId && mode === 'ws' ? (
          // 文书生成（升级方案 §7.1 / 阶段二）：中间区固定为对话与任务流，
          // 当前草稿的预览、版本与导出统一在右侧预览区，不再用工作台替换对话。
          // 打开草稿后即使还没有消息也进入对话，底部输入坞即"对当前稿提修改意见"。
          messages.length > 0 || docgenDraftId ? (
            <>
              <div className="stage">
                <ChatView />
              </div>
              <Dock />
            </>
          ) : (
            <div className="stage">
              <DocGenWorkspace key={currentCaseId} />
            </div>
          )
        ) : (
          <>
            <div className="stage">
              <Hero />
              <ChatView />
            </div>
            <Dock />
          </>
        )}
      </div>
    </main>
  )
}
