import { useEffect, useRef, useState, type JSX } from 'react'
import type { ArtifactEditable, SlideTextChange } from '@lawbuddy/contracts'
import { getBridge } from '../../services/bridge'
import { useApp } from '../../state/store'

/**
 * AI 产物 PPTX 文本框轻量编辑：
 * 只改文本框文字，形状位置/版式/图表/图片保持原样；大改仍交给 AI 重新生成。
 */

const errText = (e: unknown): string => (e instanceof Error ? e.message : String(e))

export function SlideArtifactEditor({
  info,
  onClose
}: {
  info: ArtifactEditable
  onClose: () => void
}): JSX.Element {
  const slides = info.slides ?? []
  const [active, setActive] = useState(0)
  const [edits, setEdits] = useState<Map<string, SlideTextChange>>(new Map())
  const [status, setStatus] = useState('已保存')
  const [saving, setSaving] = useState(false)
  const [dirty, setDirty] = useState(false)
  const latest = useRef(edits)
  latest.current = edits

  const save = async (createVersion: boolean): Promise<boolean> => {
    const changes = [...latest.current.values()]
    if (!changes.length) {
      if (createVersion) onClose()
      return true
    }
    const bridge = getBridge()
    if (!bridge) {
      setStatus('保存失败：本地服务未连接')
      return false
    }
    setSaving(true)
    try {
      const result = await bridge.artifact.saveSlideTexts({
        artifactId: info.artifactId,
        edits: changes,
        createVersion
      })
      setStatus(createVersion && result.versionNo != null ? `已保存 · v${result.versionNo}` : '已保存')
      setEdits(new Map())
      setDirty(false)
      useApp.setState({ wordEditingDirty: false })
      if (createVersion) void useApp.getState().loadArtifacts()
      if (result.warnings?.length) useApp.getState().toast(result.warnings.join('；'))
      return true
    } catch (e) {
      setStatus(`保存失败：${errText(e)}。修改保留在当前窗口，请重试。`)
      return false
    } finally {
      setSaving(false)
    }
  }

  useEffect(() => {
    const onKey = (e: KeyboardEvent): void => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 's') {
        e.preventDefault()
        void save(true)
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [info.artifactId])

  useEffect(() => {
    if (!dirty) return undefined
    const onBeforeUnload = (e: BeforeUnloadEvent): void => {
      e.preventDefault()
      e.returnValue = ''
    }
    window.addEventListener('beforeunload', onBeforeUnload)
    return () => window.removeEventListener('beforeunload', onBeforeUnload)
  }, [dirty])

  const slide = slides[active]

  const editText = (slideIndex: number, shapeId: number, text: string): void => {
    const next = new Map(latest.current)
    next.set(`${slideIndex}:${shapeId}`, { slideIndex, shapeId, text })
    setEdits(next)
    setDirty(true)
    useApp.setState({ wordEditingDirty: true })
  }

  return (
    <div className="artifact-editor">
      <div className="artifact-editor-scroll">
        <div className="slide-edit-paper">
          {!slides.length ? (
            <p className="doc-p" style={{ color: 'var(--ink-300)' }}>
              该演示文稿没有可编辑的文本框。
            </p>
          ) : (
            <>
              <div className="xls-tabs" role="tablist" aria-label="幻灯片">
                {slides.map((item, index) => (
                  <button
                    key={item.index}
                    type="button"
                    role="tab"
                    aria-selected={index === active}
                    className={`xls-tab${index === active ? ' on' : ''}`}
                    onClick={() => setActive(index)}
                  >
                    {index + 1}. {item.title}
                  </button>
                ))}
              </div>
              {slide && (
                <div className="slide-edit">
                  <div className="sheet-edit-head">
                    <b>
                      第 {slide.index + 1} 页 · {slide.title}
                    </b>
                    <span className="sheet-edit-chip">{slide.boxes.length} 个文本框</span>
                  </div>
                  {slide.boxes.length === 0 && (
                    <p className="sheet-edit-note">该页没有可编辑的文本框（图表/图片保持原样）。</p>
                  )}
                  {slide.boxes.map((box) => {
                    const key = `${slide.index}:${box.shapeId}`
                    const value = latest.current.get(key)?.text ?? box.text
                    return (
                      <label key={box.shapeId} className="slide-edit-box">
                        <span className="slide-edit-label">
                          {box.name}
                          {latest.current.has(key) ? <em className="slide-edit-changed">已修改</em> : null}
                        </span>
                        <textarea
                          value={value}
                          spellCheck={false}
                          onChange={(event) => editText(slide.index, box.shapeId, event.target.value)}
                        />
                      </label>
                    )
                  })}
                  {edits.size > 0 && <p className="sheet-edit-note">待保存修改：{edits.size} 个文本框</p>}
                </div>
              )}
            </>
          )}
        </div>
      </div>
      <div className="edit-toolbar" role="toolbar" aria-label="演示文稿编辑工具栏">
        <span className="et-label">PPT 文本框编辑</span>
        <span className="et-sep" />
        <span className="et-count">{edits.size} 处修改</span>
        <span className="et-sep" />
        <span className={`et-status${dirty ? ' dirty' : ''}`} role="status">
          {saving ? '保存中…' : status}
        </span>
        <button className="et-finish" disabled={saving} onClick={() => void save(true)}>
          完成
        </button>
      </div>
      {info.warnings?.length ? (
        <div className="artifact-editor-warn" role="status">
          {info.warnings.join('；')}
        </div>
      ) : null}
    </div>
  )
}
