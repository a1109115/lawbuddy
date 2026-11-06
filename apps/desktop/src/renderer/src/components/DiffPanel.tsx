import type { JSX } from 'react'
import type { TextDiffResult } from '@lawbuddy/contracts'
import { useEscapeKey } from '../hooks/useDialogA11y'
import { Icon } from './Icon'

/**
 * 版本比较面板（DEV-MOD-009 / REQ-GEN-010、REQ-CON-002）：
 * 行级差异左右并排（左=旧版本，右=新版本），只读展示、不触发任何写入；
 * 文书草稿版本与合同工作副本版本共用（差异结构由 Core 的 editor.diff_texts 产出）。
 */
export function DiffPanel({ diff, onClose }: { diff: TextDiffResult; onClose: () => void }): JSX.Element {
  useEscapeKey(onClose)
  return (
    <div className="diff-panel" role="region" aria-label="版本比较结果">
      <div className="diff-head">
        <Icon id="i-list" />
        <span className="diff-title">
          {diff.fromLabel} → {diff.toLabel}
        </span>
        <span className="diff-stat diff-add">+{diff.stats.added}</span>
        <span className="diff-stat diff-del">-{diff.stats.removed}</span>
        <button className="icon-btn diff-x" aria-label="关闭版本比较" onClick={onClose}>
          <Icon id="i-x" />
        </button>
      </div>
      {diff.hunks.length === 0 ? (
        <div className="dg-empty-sm">两个版本内容一致</div>
      ) : (
        <div className="diff-body">
          {diff.hunks.map((h, i) => (
            <div key={i} className={`diff-hunk diff-${h.type}`}>
              <div className="diff-col diff-left" aria-label={`${diff.fromLabel} 第 ${h.leftStart} 行起`}>
                {h.leftLines.map((line, j) => (
                  <div key={j} className="diff-line">
                    {line === '' ? ' ' : line}
                  </div>
                ))}
              </div>
              <div className="diff-col diff-right" aria-label={`${diff.toLabel} 第 ${h.rightStart} 行起`}>
                {h.rightLines.map((line, j) => (
                  <div key={j} className="diff-line">
                    {line === '' ? ' ' : line}
                  </div>
                ))}
              </div>
            </div>
          ))}
          {diff.truncated && <div className="dg-empty-sm">差异过多，仅显示前 {diff.hunks.length} 处</div>}
        </div>
      )}
    </div>
  )
}