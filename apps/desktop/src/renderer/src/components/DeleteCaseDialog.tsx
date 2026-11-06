import { useState, type JSX } from 'react'
import { useDialogA11y } from '../hooks/useDialogA11y'
import { useApp } from '../state/store'
import { Icon } from './Icon'

/** 永久删除任务确认（删除任务及其文档、索引、会话与引证数据） */
export function DeleteCaseDialog(): JSX.Element | null {
  const { deletingCase, setDeletingCase, deleteCase } = useApp()
  const [submitting, setSubmitting] = useState(false)

  const close = (): void => {
    setDeletingCase(null)
  }
  const cardRef = useDialogA11y(close, deletingCase !== null)

  if (!deletingCase) return null

  const submit = async (): Promise<void> => {
    if (submitting) return
    setSubmitting(true)
    await deleteCase(deletingCase.id)
    setSubmitting(false)
  }

  return (
    <div
      className="modal-overlay"
      role="alertdialog"
      aria-modal="true"
      aria-label="永久删除任务"
      onClick={(e) => {
        if (e.target === e.currentTarget) close()
      }}
    >
      <div ref={cardRef} tabIndex={-1} className="modal-card">
        <div className="modal-head">
          <h3>永久删除任务</h3>
          <button className="icon-btn" aria-label="关闭" onClick={close}>
            <Icon id="i-x" />
          </button>
        </div>

        <div className="modal-body">
          <p className="del-warning">
            <Icon id="i-alert" />
            <span>
              将永久删除任务「{deletingCase.title}」及其全部文档、索引、会话与引证数据，
              <strong>此操作不可恢复</strong>。
            </span>
          </p>
          <p className="form-hint">
            内容存储按文件哈希共享、审计日志按规范保留，删除动作将写入审计。
          </p>
        </div>

        <div className="modal-foot">
          <button className="btn-ghost" onClick={close}>
            取消
          </button>
          <button className="btn-danger" disabled={submitting} onClick={() => void submit()}>
            {submitting ? '删除中…' : '永久删除'}
          </button>
        </div>
      </div>
    </div>
  )
}
