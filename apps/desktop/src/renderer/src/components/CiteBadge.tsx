import type { JSX } from 'react'
import type { CiteChip } from '@lawbuddy/contracts'
import { Icon } from './Icon'
import { useApp } from '../state/store'

const REASON_LABEL: Record<string, string> = {
  QUOTE_MISMATCH: '引语与原文不匹配',
  PAGE_AMBIGUOUS: '原文多页出现，页码需人工确认',
  LOW_OCR_CONFIDENCE: 'OCR 置信度低',
  OUT_OF_EVIDENCE_SCOPE: '超出已检索证据范围',
  WEAK_SUPPORT: '证据支撑不足',
  SOURCE_CONFLICT: '来源冲突',
  CONFLICT_REJECTED: '矛盾裁决未采纳'
}

/** 单条目引证芯片：supported 可跳转原文；needs_human 打开核实队列（缺省跳原文）（PRD 11 引证线） */
export function CiteBadge({
  cite,
  onCite,
  onPending
}: {
  cite?: CiteChip
  onCite: (docId: string, pageNo: number) => void
  /** 待核实芯片点击打开核实队列（PRD 11.4） */
  onPending?: () => void
}): JSX.Element | null {
  if (!cite) return null
  const pending = cite.claimStatus === 'needs_human' || cite.claimStatus === 'conflict' || cite.claimStatus === 'stale'
  const reason = REASON_LABEL[cite.reasonCode ?? ''] ?? cite.reasonCode ?? ''
  if (!cite.documentId || cite.pageNo == null) {
    return (
      <button
        className="cite-chip warn static"
        title={`待人工核实：${reason || '超出已检索证据范围'}`}
        onClick={() => onPending?.()}
      >
        <Icon id="i-alert" />
        待核实
      </button>
    )
  }
  return (
    <button
      className={`cite-chip${pending ? ' warn' : ''}`}
      title={pending ? `待人工核实：${reason}（点击打开核实队列）` : '引证已校验（原文匹配）'}
      onClick={() => {
        if (pending && onPending) onPending()
        else if (cite.quote) void useApp.getState().openCitationTarget({
          documentId: cite.documentId!, pageNo: cite.pageNo!, quote: cite.quote,
          documentVersionId: cite.documentVersionId
        }).catch(() => useApp.getState().toast('原文定位失败，请重新核验'))
        else onCite(cite.documentId!, cite.pageNo!)
      }}
    >
      <Icon id={pending ? 'i-alert' : 'i-link'} />
      {cite.label ?? `${cite.documentId} · 第${cite.pageNo}页`}
    </button>
  )
}
