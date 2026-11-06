import { useEffect, useRef, useState, type JSX } from 'react'
import type { DocGenDraft } from '@lawbuddy/contracts'
import { getBridge } from '../../services/bridge'
import { useApp } from '../../state/store'
import { Icon } from '../Icon'
import { MarkdownView } from '../markdown/MarkdownView'

const errText = (e: unknown): string => (e instanceof Error ? e.message : String(e))

/** 版本冲突（升级方案 §7.4）：保存基准与当前稿不一致，需先载入最新稿再继续 */
const isConflict = (e: unknown): boolean => errText(e).includes('VERSION_CONFLICT')

/**
 * 文书正文编辑器（Markdown 源稿 + 实时预览）。
 *
 * 文书草稿的正文是带槽位标记的 Markdown，直接转成 Word 模型会丢失槽位语义，
 * 因此这里保留源稿编辑：自动保存（2 秒）、Ctrl+S 手动保存形成版本、完成时保存。
 * 槽位是否可定稿仍由 Core 校验（存在待补充占位符时定稿按钮保持禁用）。
 *
 * 并发协同（升级方案 §7.4 / 阶段二）：编辑器持有打开时的当前稿版本号作为保存基准
 * （expectedVersionNo）。另一窗口编辑或 AI 修订产生了新版本时，保存被 Core 拒绝，
 * 界面提示"版本已变化"并提供载入最新稿的入口，不会静默覆盖较新内容。
 */
export function DraftBodyEditor({
  draftId,
  title,
  initialBody,
  baseVersionNo,
  onClose,
  onSaved
}: {
  draftId: string
  title: string
  initialBody: string
  /** 打开编辑器时草稿的当前稿版本号（保存基准，冲突检测用） */
  baseVersionNo: number
  onClose: () => void
  onSaved?: (draft: DocGenDraft) => void
}): JSX.Element {
  const [body, setBody] = useState(initialBody)
  const [tab, setTab] = useState<'edit' | 'preview'>('edit')
  const [status, setStatus] = useState('已保存')
  const [saving, setSaving] = useState(false)
  const [dirty, setDirty] = useState(false)
  // 版本冲突：当前稿已被其他窗口/AI 修订推进，本地编辑不再自动落库
  const [conflict, setConflict] = useState<{ currentVersionNo: number } | null>(null)
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const latest = useRef(body)
  latest.current = body
  // 保存基准：每次成功落库后前移到新版本号
  const expected = useRef<number>(baseVersionNo)

  const save = async (createVersion: boolean, text: string): Promise<boolean> => {
    const bridge = getBridge()
    if (!bridge) {
      setStatus('保存失败：本地服务未连接')
      return false
    }
    if (conflict) return false // 冲突未处理前不覆盖当前稿
    setSaving(true)
    try {
      const r = await bridge.docgen.saveBody({
        draftId,
        bodyMd: text,
        kind: createVersion ? 'edit' : 'autosave',
        expectedVersionNo: expected.current
      })
      expected.current = r.versionNo
      setStatus(createVersion ? `已保存 · v${r.versionNo}` : '已保存（自动保存）')
      onSaved?.(r.draft)
      return true
    } catch (e) {
      if (isConflict(e)) {
        // 读取最新稿版本号，明确告知用户"已变化到哪一版"
        const latestDraft = await getBridge()
          ?.docgen.getDraft(draftId)
          .catch(() => null)
        setConflict({ currentVersionNo: latestDraft?.currentVersionNo ?? 0 })
        setStatus('版本已变化，未保存')
      } else {
        setStatus(`保存失败：${errText(e)}。内容保留在当前窗口，请重试。`)
      }
      return false
    } finally {
      setSaving(false)
    }
  }

  /** 冲突处理：载入最新稿覆盖编辑器内容（用户主动放弃本地编辑，不做静默合并） */
  const reloadLatest = async (): Promise<void> => {
    const bridge = getBridge()
    if (!bridge) return
    try {
      const d = await bridge.docgen.getDraft(draftId)
      const fresh = d.bodyMd ?? ''
      setBody(fresh)
      latest.current = fresh
      expected.current = d.currentVersionNo
      setConflict(null)
      setDirty(false)
      setStatus(`已载入最新稿 · v${d.currentVersionNo}`)
      onSaved?.(d)
    } catch (e) {
      setStatus(`载入最新稿失败：${errText(e)}`)
    }
  }

  useEffect(() => {
    useApp.setState({ wordEditingDirty: true })
    return () => useApp.setState({ wordEditingDirty: false })
  }, [])

  useEffect(() => {
    const onKey = (e: KeyboardEvent): void => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 's') {
        e.preventDefault()
        if (timer.current) clearTimeout(timer.current)
        void save(true, latest.current).then((ok) => ok && setDirty(false))
      }
    }
    window.addEventListener('keydown', onKey)
    return () => {
      window.removeEventListener('keydown', onKey)
      if (timer.current) clearTimeout(timer.current)
    }
  }, [conflict])

  const finish = async (): Promise<void> => {
    if (timer.current) clearTimeout(timer.current)
    if (conflict) {
      // 冲突未解决时不关闭，避免用户的本地修改被默默丢弃
      setStatus('版本已变化：请先载入最新稿或复制本地内容后再离开')
      return
    }
    if (dirty) {
      const ok = await save(true, latest.current)
      if (!ok) return
    }
    onClose()
  }

  return (
    <div className="docgen-editor-mask" role="dialog" aria-modal="true" aria-label={`${title} 正文编辑`}>
      <div className="docgen-editor">
        <header className="docgen-editor-head">
          <span className="docgen-editor-title">
            <Icon id="i-doc" />
            {title} · 正文编辑
          </span>
          <div className="docgen-editor-tabs" role="tablist">
            <button
              type="button"
              role="tab"
              aria-selected={tab === 'edit'}
              className={tab === 'edit' ? 'on' : ''}
              onClick={() => setTab('edit')}
            >
              编辑
            </button>
            <button
              type="button"
              role="tab"
              aria-selected={tab === 'preview'}
              className={tab === 'preview' ? 'on' : ''}
              onClick={() => setTab('preview')}
            >
              预览
            </button>
          </div>
          <span className={`et-status${dirty ? ' dirty' : ''}`} role="status">
            {saving ? '保存中…' : status}
          </span>
          <button className="et-finish" disabled={saving} onClick={() => void finish()}>
            完成
          </button>
        </header>
        {conflict && (
          <div className="docgen-editor-conflict" role="alert">
            <Icon id="i-alert" />
            <span>
              当前稿已被推进到 v{conflict.currentVersionNo}（另一窗口编辑或 AI 修订），本地修改未保存。
              载入最新稿会覆盖编辑框内的内容，请先复制需要保留的段落。
            </span>
            <button className="cr-btn ok" disabled={saving} onClick={() => void reloadLatest()}>
              载入最新稿
            </button>
          </div>
        )}
        <div className="docgen-editor-body">
          {tab === 'edit' ? (
            <textarea
              className="docgen-editor-area"
              aria-label={`${title} Markdown 正文`}
              spellCheck={false}
              value={body}
              onChange={(e) => {
                setBody(e.target.value)
                setDirty(true)
                if (timer.current) clearTimeout(timer.current)
                timer.current = setTimeout(() => {
                  void save(false, latest.current).then((ok) => ok && setDirty(false))
                }, 2000)
              }}
            />
          ) : (
            <div className="docgen-editor-preview">
              <MarkdownView source={body} className="md-body" />
            </div>
          )}
        </div>
        <footer className="docgen-editor-foot">
          <span>{body.replace(/\s/g, '').length} 字</span>
          <span>待补充占位符需补全后才能定稿；保存会写入版本历史</span>
        </footer>
      </div>
    </div>
  )
}
