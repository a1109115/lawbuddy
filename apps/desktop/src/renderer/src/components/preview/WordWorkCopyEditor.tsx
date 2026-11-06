import { useEffect, useRef, useState, type JSX } from 'react'
import type { WordNode, WordWorkCopy } from '@lawbuddy/contracts'
import { getBridge } from '../../services/bridge'
import { useApp } from '../../state/store'
import { Icon } from '../Icon'
import { WordModelEditor } from './WordModelEditor'

const errText = (e: unknown): string => (e instanceof Error ? e.message : String(e))

/**
 * 合同审查工作副本编辑器：复用统一 Word 编辑工作台。
 *
 * - 原件始终只读；本组件只编辑 `word_work_copies` 工作副本；
 * - 保存走 `word.saveModel`（携带 expectedRevision，冲突时拒绝覆盖并要求重新载入）；
 * - 自动保存不形成新修订，手动保存/完成形成 revision。
 */
export function WordWorkCopyEditor({
  targetId,
  onClose,
  onSaved
}: {
  targetId: string
  onClose: () => void
  onSaved?: (workCopy: WordWorkCopy) => void
}): JSX.Element {
  const [loaded, setLoaded] = useState<WordWorkCopy | null>(null)
  const [error, setError] = useState('')
  const [liveWarnings, setLiveWarnings] = useState<string[]>([])
  const revision = useRef(0)

  const load = (): void => {
    const bridge = getBridge()
    if (!bridge) {
      setError('本地服务未连接，无法打开工作副本')
      return
    }
    setError('')
    setLoaded(null)
    bridge.word
      .getModel(targetId)
      .then((wc) => {
        revision.current = wc.revision
        setLoaded(wc)
      })
      .catch((e) => setError(errText(e)))
  }

  useEffect(() => {
    load()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [targetId])

  if (error) {
    return (
      <div className="artifact-editor">
        <div className="artifact-editor-error" role="alert">
          <Icon id="i-alert" />
          {error}
          <button className="btn-ghost" onClick={load}>
            重新载入
          </button>
          <button className="btn-ghost" onClick={onClose}>
            返回审查
          </button>
        </div>
      </div>
    )
  }
  if (!loaded) {
    return (
      <div className="artifact-editor">
        <div className="artifact-editor-loading">正在打开工作副本…</div>
      </div>
    )
  }

  return (
    <WordModelEditor
      title={loaded.title}
      initialModel={loaded.model as WordNode}
      warnings={loaded.warnings}
      liveWarnings={liveWarnings}
      save={async (model, createVersion) => {
        const bridge = getBridge()
        if (!bridge) return { ok: false, message: '保存失败：本地服务未连接' }
        try {
          const saved = await bridge.word.saveModel({
            targetId,
            model,
            expectedRevision: revision.current,
            clientRequestId: crypto.randomUUID(),
            kind: createVersion ? 'edit' : 'autosave'
          })
          revision.current = saved.revision
          setLiveWarnings(saved.warnings ?? [])
          onSaved?.(saved)
          return { ok: true, message: `已保存 · 修订 r${saved.revision}` }
        } catch (e) {
          const message = errText(e)
          // 版本冲突：保留用户内容，提示重新载入后合并
          const hint = message.includes('revision') || message.includes('修订')
            ? '；工作副本已被其他操作更新，请关闭后重新载入再编辑'
            : ''
          return { ok: false, message: `保存失败：${message}${hint}` }
        }
      }}
      onClose={() => {
        useApp.getState().toast('工作副本已保存，原件未改动')
        onClose()
      }}
    />
  )
}
