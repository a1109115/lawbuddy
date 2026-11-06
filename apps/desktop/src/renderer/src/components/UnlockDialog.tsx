import { useState, type JSX } from 'react'
import { useApp } from '../state/store'
import { Icon } from './Icon'

/** 加密文档解锁（REQ-DOC-004/011）：密码经专用窗口输入，不入库、不进日志 */
export function UnlockDialog(): JSX.Element | null {
  const { unlockTarget, closeUnlock, unlockDocument } = useApp()
  const [value, setValue] = useState('')
  const [busy, setBusy] = useState(false)
  if (!unlockTarget) return null

  const submit = async (): Promise<void> => {
    if (!value.trim() || busy) return
    setBusy(true)
    const ok = await unlockDocument(value)
    setBusy(false)
    if (ok) {
      setValue('')
    }
  }

  return (
    <div
      className="modal-overlay"
      role="dialog"
      aria-modal="true"
      aria-label="加密文档解锁"
      onClick={(e) => {
        if (e.target === e.currentTarget && !busy) closeUnlock()
      }}
    >
      <div className="modal-card apikey-card">
        <div className="modal-head">
          <h3>加密文档解锁</h3>
          <button
            className="icon-btn"
            aria-label="关闭"
            disabled={busy}
            onClick={closeUnlock}
          >
            <Icon id="i-x" />
          </button>
        </div>
        <div className="modal-body">
          <p className="form-hint">
            文件「{unlockTarget.title}」已加密，请输入密码以解锁阅读。
            密码仅经本机受控通道传给隔离解密进程，不会保存、不进入日志与提示词。
          </p>
          <div className="form-row">
            <label className="form-label" htmlFor="unlock-input">
              文档密码
            </label>
            <input
              id="unlock-input"
              className="form-input"
              type="password"
              value={value}
              placeholder="请输入文档密码"
              autoComplete="off"
              disabled={busy}
              onChange={(e) => setValue(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') void submit()
              }}
              autoFocus
            />
          </div>
        </div>
        <div className="modal-foot">
          <button className="btn-ghost" disabled={busy} onClick={closeUnlock}>
            取消
          </button>
          <button className="btn-primary" disabled={!value.trim() || busy} onClick={() => void submit()}>
            {busy ? '解锁中…' : '解锁'}
          </button>
        </div>
      </div>
    </div>
  )
}