import { useEffect, useRef, useState, type JSX } from 'react'
import type { ArtifactEditable, WordNode } from '@lawbuddy/contracts'
import { getBridge } from '../../services/bridge'
import { useApp } from '../../state/store'
import { Icon } from '../Icon'
import { WordModelEditor } from './WordModelEditor'
import { SheetArtifactEditor } from './SheetArtifactEditor'
import { SlideArtifactEditor } from './SlideArtifactEditor'

const errText = (e: unknown): string => (e instanceof Error ? e.message : String(e))

/**
 * AI 生成文档的直接编辑器（无工作副本）：
 * - docx：Core 解析为 Word 模型，保存时序列化回原 DOCX 并原子替换工作空间文件；
 * - xlsx：Core 返回可编辑网格，保存只增量写回改动单元格；
 * - pptx：Core 返回每页文本框，保存只改文字、保留形状与版式；
 * - md/html/txt：源码编辑，保存直接写回原文件。
 * 案件卷宗（用户导入的原件）不进入本编辑器，保持只读。
 *
 * Word 部分复用统一的 `WordModelEditor`（与合同审查工作副本同一套工具栏与模型约束）。
 */
export function ArtifactEditor({
  artifactId,
  onClose
}: {
  artifactId: string
  onClose: () => void
}): JSX.Element {
  const [info, setInfo] = useState<ArtifactEditable | null>(null)
  const [error, setError] = useState('')

  useEffect(() => {
    const bridge = getBridge()
    if (!bridge) {
      setError('本地服务未连接，无法编辑')
      return
    }
    let alive = true
    bridge.artifact
      .openEditable({ artifactId })
      .then((data) => {
        if (alive) setInfo(data)
      })
      .catch((e) => {
        if (alive) setError(errText(e))
      })
    return () => {
      alive = false
    }
  }, [artifactId])

  if (error) {
    return (
      <div className="artifact-editor">
        <div className="artifact-editor-error" role="alert">
          <Icon id="i-alert" />
          {error}
          <button className="btn-ghost" onClick={onClose}>
            返回预览
          </button>
        </div>
      </div>
    )
  }
  if (!info) {
    return (
      <div className="artifact-editor">
        <div className="artifact-editor-loading">正在打开文档…</div>
      </div>
    )
  }
  return info.kind === 'word' && info.model ? (
    <WordArtifactEditor info={info} onClose={onClose} />
  ) : info.kind === 'sheet' && info.sheets ? (
    <SheetArtifactEditor info={info} onClose={onClose} />
  ) : info.kind === 'slide' && info.slides ? (
    <SlideArtifactEditor info={info} onClose={onClose} />
  ) : (
    <TextEditable info={info} onClose={onClose} />
  )
}

// ---------------------------------------------------------------------------
// DOCX：统一 Word 模型编辑器 → artifact.save_editable
// ---------------------------------------------------------------------------

function WordArtifactEditor({ info, onClose }: { info: ArtifactEditable; onClose: () => void }): JSX.Element {
  return (
    <WordModelEditor
      title={info.title}
      initialModel={info.model as WordNode}
      warnings={info.warnings}
      save={async (model, createVersion) => {
        const bridge = getBridge()
        if (!bridge) return { ok: false, message: '保存失败：本地服务未连接' }
        try {
          const r = await bridge.artifact.saveEditable({
            artifactId: info.artifactId,
            model,
            createVersion
          })
          // 编辑版本追加后刷新产物概览，版本号/版本数立即同步
          if (createVersion) void useApp.getState().loadArtifacts()
          return {
            ok: true,
            message: createVersion && r.versionNo != null ? `已保存 · v${r.versionNo}` : '已保存'
          }
        } catch (e) {
          return { ok: false, message: `保存失败：${errText(e)}。内容保留在当前窗口，请重试。` }
        }
      }}
      onClose={onClose}
    />
  )
}

// ---------------------------------------------------------------------------
// Markdown / HTML / 纯文本：源码直接编辑
// ---------------------------------------------------------------------------

function TextEditable({ info, onClose }: { info: ArtifactEditable; onClose: () => void }): JSX.Element {
  const [status, setStatus] = useState('已保存')
  const [saving, setSaving] = useState(false)
  const [value, setValue] = useState(info.text ?? '')
  const [dirty, setDirty] = useState(false)
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const latest = useRef(value)
  latest.current = value
  const changed = useRef(false)
  const queue = useRef(Promise.resolve<boolean>(true))

  const save = (createVersion: boolean, text: string): Promise<boolean> => {
    const task = async (): Promise<boolean> => {
      const bridge = getBridge()
      if (!bridge) {
        setStatus('保存失败：本地服务未连接')
        return false
      }
      setSaving(true)
      try {
        const r = await bridge.artifact.saveEditable({
          artifactId: info.artifactId,
          text,
          createVersion
        })
        setStatus(createVersion && r.versionNo != null ? `已保存 · v${r.versionNo}` : '已保存')
        if (createVersion) void useApp.getState().loadArtifacts()
        return true
      } catch (e) {
        setStatus(`保存失败：${errText(e)}。内容保留在当前窗口，请重试。`)
        return false
      } finally {
        setSaving(false)
      }
    }
    queue.current = queue.current.then(task, task)
    return queue.current
  }

  const autosave = async (): Promise<void> => {
    const ok = await save(false, latest.current)
    if (ok) {
      setDirty(false)
      useApp.setState({ wordEditingDirty: false })
    }
  }

  useEffect(() => {
    const onKey = (e: KeyboardEvent): void => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 's') {
        e.preventDefault()
        if (timer.current) clearTimeout(timer.current)
        void save(true, latest.current).then((ok) => {
          if (ok) {
            changed.current = false
            setDirty(false)
            useApp.setState({ wordEditingDirty: false })
          }
        })
      }
    }
    window.addEventListener('keydown', onKey)
    return () => {
      window.removeEventListener('keydown', onKey)
      if (timer.current) clearTimeout(timer.current)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  useEffect(() => {
    if (!dirty) return undefined
    const onBeforeUnload = (e: BeforeUnloadEvent): void => {
      e.preventDefault()
      e.returnValue = ''
    }
    window.addEventListener('beforeunload', onBeforeUnload)
    return () => window.removeEventListener('beforeunload', onBeforeUnload)
  }, [dirty])

  const finish = async (): Promise<void> => {
    if (timer.current) clearTimeout(timer.current)
    if (dirty || changed.current) {
      const ok = await save(true, latest.current)
      if (!ok) return
      changed.current = false
    }
    useApp.setState({ wordEditingDirty: false })
    onClose()
  }

  return (
    <div className="artifact-editor">
      <div className="artifact-editor-scroll">
        <div className="text-paper">
          <textarea
            className="text-edit-area"
            aria-label={`${info.title} 源码编辑`}
            spellCheck={false}
            value={value}
            onChange={(e) => {
              setValue(e.target.value)
              changed.current = true
              setDirty(true)
              useApp.setState({ wordEditingDirty: true })
              if (timer.current) clearTimeout(timer.current)
              timer.current = setTimeout(() => void autosave(), 2000)
            }}
          />
        </div>
      </div>
      <div className="edit-toolbar" role="toolbar" aria-label="文档编辑工具栏">
        <span className="et-label">
          {(() => {
            const ext = info.title.split('.').pop()?.toLowerCase() ?? ''
            if (ext === 'html' || ext === 'htm') return 'HTML 源码'
            if (ext === 'md' || ext === 'markdown') return 'Markdown 源码'
            return '纯文本'
          })()}
        </span>
        <span className="et-sep" />
        <span className="et-count">{value.length} 字符</span>
        <span className="et-sep" />
        <span className={`et-status${dirty ? ' dirty' : ''}`} role="status">
          {saving ? '保存中…' : status}
        </span>
        <button className="et-finish" disabled={saving} onClick={() => void finish()}>
          完成
        </button>
      </div>
    </div>
  )
}
