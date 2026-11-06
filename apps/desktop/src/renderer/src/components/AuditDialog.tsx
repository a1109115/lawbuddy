import { useCallback, useEffect, useState, type JSX } from 'react'
import type { AuditLogEntry, AuditVerifyResult } from '@lawbuddy/contracts'
import { useDialogA11y } from '../hooks/useDialogA11y'
import { getBridge } from '../services/bridge'
import { useApp } from '../state/store'
import { Icon } from './Icon'

const ACTION_LABEL: Record<string, string> = {
  'workspace.initialize': '初始化工作空间',
  'case.create': '新建案件',
  'case.archive': '归档案件',
  'case.restore': '恢复案件',
  'document.import': '导入文档'
}

function fmtTime(iso: string): string {
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  const p = (n: number): string => String(n).padStart(2, '0')
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
}

/** 审计日志查看器（PRD 12.6：按案件筛选；链完整性可见）+ 加密备份（DEV-FND-006 / DB-006） */
export function AuditDialog(): JSX.Element | null {
  const { auditOpen, setAuditOpen, cases, toast } = useApp()
  const [verify, setVerify] = useState<AuditVerifyResult | null>(null)
  const [entries, setEntries] = useState<AuditLogEntry[]>([])
  const [caseFilter, setCaseFilter] = useState('')
  const [loading, setLoading] = useState(false)
  // —— 加密备份（DB-006）：口令仅在本机输入并直传 Core，不落盘、不进日志 ——
  const [pass1, setPass1] = useState('')
  const [pass2, setPass2] = useState('')
  const [backupBusy, setBackupBusy] = useState(false)
  const [backupInfo, setBackupInfo] = useState<string | null>(null)

  const cardRef = useDialogA11y(() => setAuditOpen(false), auditOpen)

  const reload = useCallback(async () => {
    const bridge = getBridge()
    if (!bridge) return
    setLoading(true)
    try {
      const [v, list] = await Promise.all([
        bridge.audit.verify(),
        bridge.audit.list({ caseId: caseFilter || undefined, limit: 100 })
      ])
      setVerify(v)
      setEntries(list)
    } catch (e) {
      toast(`审计读取失败：${e instanceof Error ? e.message : String(e)}`)
    } finally {
      setLoading(false)
    }
  }, [caseFilter, toast])

  useEffect(() => {
    if (auditOpen) void reload()
  }, [auditOpen, reload])

  const createBackup = async (): Promise<void> => {
    const bridge = getBridge()
    if (!bridge) return
    if (pass1.length < 8) {
      toast('恢复口令至少 8 个字符（恢复数据库时需要）')
      return
    }
    if (pass1 !== pass2) {
      toast('两次输入的口令不一致')
      return
    }
    setBackupBusy(true)
    try {
      const r = await bridge.backup.create({ passphrase: pass1 })
      if (!r) {
        toast('已取消备份')
        return
      }
      setBackupInfo(`备份文件：${r.dbPath}（${(r.manifest.dbSizeBytes / 1024).toFixed(0)} KB，schema v${r.manifest.schemaVersion}）`)
      toast(`加密备份完成：${r.manifest.counts.cases ?? 0} 个案件、${r.manifest.audit.total} 条审计记录已纳入`)
      setPass1('')
      setPass2('')
    } catch (e) {
      toast(`备份失败：${e instanceof Error ? e.message : String(e)}`)
    } finally {
      setBackupBusy(false)
    }
  }

  if (!auditOpen) return null

  return (
    <div
      className="modal-overlay"
      role="dialog"
      aria-modal="true"
      aria-label="审计与诊断"
      onClick={(e) => {
        if (e.target === e.currentTarget) setAuditOpen(false)
      }}
    >
      <div ref={cardRef} tabIndex={-1} className="modal-card audit-card">
        <div className="modal-head">
          <h3>审计与诊断</h3>
          <div className="audit-head-right">
            <span
              className={`audit-chain${verify ? (verify.ok ? ' ok' : ' bad') : ''}`}
              title={verify?.ok ? '哈希链校验通过，记录未被篡改' : `哈希链在 seq=${verify?.brokenAt} 处断开：${verify?.reason ?? ''}`}
            >
              <Icon id={verify?.ok ? 'i-check' : 'i-alert'} />
              {verify ? (verify.ok ? `链完整 · ${verify.total} 条` : `链损坏 · 断点 seq ${verify.brokenAt}`) : '校验中…'}
            </span>
            <button className="icon-btn" aria-label="刷新" title="刷新" onClick={() => void reload()}>
              <Icon id="i-new" />
            </button>
            <button className="icon-btn" aria-label="关闭" onClick={() => setAuditOpen(false)}>
              <Icon id="i-x" />
            </button>
          </div>
        </div>

        <div className="audit-toolbar">
          <select
            className="form-input audit-filter"
            value={caseFilter}
            aria-label="按案件筛选"
            onChange={(e) => setCaseFilter(e.target.value)}
          >
            <option value="">全部案件</option>
            {cases.map((c) => (
              <option key={c.id} value={c.id}>
                {c.title}
              </option>
            ))}
          </select>
          <span className="audit-hint">只追加记录 · 哈希链防篡改 · 不含卷宗正文</span>
        </div>

        {/* 加密备份（DEV-FND-006 / DB-006）：在线备份，口令用于新机恢复 */}
        <div className="backup-bar" aria-label="加密备份">
          <Icon id="i-shield" />
          <span className="backup-title">加密备份</span>
          <input
            className="form-input backup-pass"
            type="password"
            value={pass1}
            placeholder="恢复口令（≥8 字符，务必离线保存）"
            aria-label="恢复口令"
            onChange={(e) => setPass1(e.target.value)}
          />
          <input
            className="form-input backup-pass"
            type="password"
            value={pass2}
            placeholder="再次输入口令"
            aria-label="确认恢复口令"
            onChange={(e) => setPass2(e.target.value)}
          />
          <button className="btn-primary backup-btn" disabled={backupBusy} onClick={() => void createBackup()}>
            {backupBusy ? '备份中…' : '创建加密备份'}
          </button>
          <span className="backup-hint" title="恢复需在应用关闭后执行">
            恢复：关闭应用后运行 python scripts/restore_backup.py
          </span>
        </div>
        {backupInfo && <div className="backup-info">{backupInfo}</div>}

        <div className="audit-list" role="table" aria-label="审计记录">
          {loading && entries.length === 0 ? (
            <div className="archived-empty">加载中…</div>
          ) : entries.length === 0 ? (
            <div className="archived-empty">暂无审计记录</div>
          ) : (
            entries.map((e) => (
              <div key={e.seq} className="audit-row" role="row">
                <span className="audit-seq" title={`eventHash ${e.eventHash}`}>#{e.seq}</span>
                <span className="audit-time">{fmtTime(e.createdAt)}</span>
                <span className="audit-action">{ACTION_LABEL[e.action] ?? e.action}</span>
                <span className="audit-meta">{metaSummary(e)}</span>
                <span className={`audit-result ${e.result}`}>
                  {e.result === 'ok' ? '成功' : e.result === 'denied' ? '拒绝' : '失败'}
                </span>
              </div>
            ))
          )}
        </div>
      </div>
    </div>
  )
}

function metaSummary(e: AuditLogEntry): string {
  const m = e.metadata as Record<string, unknown>
  const parts: string[] = []
  if (typeof m.fileName === 'string') parts.push(m.fileName)
  else if (typeof m.title === 'string') parts.push(m.title)
  if (typeof m.kind === 'string') parts.push(m.kind.toUpperCase())
  if (typeof m.sha256 === 'string') parts.push(`sha:${m.sha256}`)
  if (typeof m.reason === 'string' && e.result !== 'ok') parts.push(m.reason)
  return parts.join(' · ')
}
