import { useMemo, useState, type JSX, type KeyboardEvent as RKeyboardEvent } from 'react'
import type { HumanAnswer, HumanQuestion, HumanQuestionSet } from '@lawbuddy/contracts'
import { useApp } from '../state/store'
import { Icon } from './Icon'

/** 已选答案 → 一行摘要文案（回答后卡片转只读时展示） */
function answerLabel(question: HumanQuestion, answer?: HumanAnswer): string {
  if (!answer) return '未回答'
  if (answer.deferred) return '暂不决定'
  if (answer.text?.trim()) return answer.text.trim()
  const picked = (answer.optionIds ?? [])
    .map((id) => question.options?.find((o) => o.id === id)?.label ?? id)
    .filter(Boolean)
  return picked.length ? picked.join('、') : '未回答'
}

/** 单个问题的选项与自由输入（选择状态由父组件统一持有） */
function QuestionBody({
  question,
  optionIds,
  text,
  deferred,
  disabled,
  onToggle,
  onText
}: {
  question: HumanQuestion
  optionIds: string[]
  text: string
  deferred: boolean
  disabled: boolean
  onToggle: (id: string) => void
  onText: (v: string) => void
}): JSX.Element {
  const single = question.kind !== 'multi_choice'
  const allowOther = question.allowOther !== false
  const useText = question.kind === 'text'
  const options = question.options ?? []

  /** 键盘操作（升级方案 §11.2 前端覆盖）：选项组用方向键切换，符合 radiogroup 语义 */
  const onKeyDown = (e: RKeyboardEvent<HTMLDivElement>): void => {
    if (useText || deferred || !options.length) return
    const forward = e.key === 'ArrowDown' || e.key === 'ArrowRight'
    const back = e.key === 'ArrowUp' || e.key === 'ArrowLeft'
    if (!forward && !back) return
    e.preventDefault()
    const at = options.findIndex((o) => optionIds.includes(o.id))
    const step = forward ? 1 : -1
    const next = at < 0 ? (forward ? 0 : options.length - 1) : (at + step + options.length) % options.length
    onToggle(options[next].id)
  }

  return (
    <div className="hq-body">
      {question.reason && <p className="hq-reason">{question.reason}</p>}
      {!useText && (
        <div
          className="hq-options"
          role={single ? 'radiogroup' : 'group'}
          aria-label={question.title}
          onKeyDown={onKeyDown}
        >
          {options.map((opt) => {
            const on = !deferred && optionIds.includes(opt.id)
            return (
              <button
                key={opt.id}
                type="button"
                role={single ? 'radio' : 'checkbox'}
                aria-checked={on}
                className={`hq-opt${on ? ' on' : ''}`}
                disabled={disabled}
                onClick={() => onToggle(opt.id)}
              >
                <span className={`hq-mark ${single ? 'radio' : 'check'}`} aria-hidden="true">
                  {on && <Icon id="i-check" />}
                </span>
                <span className="hq-opt-copy">
                  <span className="hq-opt-label">
                    {opt.label}
                    {opt.recommended && <span className="hq-rec">推荐</span>}
                  </span>
                  {opt.description && <span className="hq-opt-desc">{opt.description}</span>}
                  {opt.recommended && opt.recommendationReason && (
                    <span className="hq-opt-why">推荐理由：{opt.recommendationReason}</span>
                  )}
                </span>
              </button>
            )
          })}
        </div>
      )}
      {(useText || allowOther) && (
        <label className="hq-other">
          <span>{useText ? '请填写' : '其他'}</span>
          <input
            className="form-input"
            value={text}
            disabled={disabled || deferred}
            placeholder={useText ? '补充说明（会作为你的决策写入文书）' : '以上选项都不合适时，可在此填写'}
            onChange={(e) => onText(e.target.value)}
          />
        </label>
      )}
    </div>
  )
}

/**
 * 文书生成问题卡（升级方案 §5.1）：
 * 一次只问会改变下一步生成行为的问题；回答后转只读摘要，保留当时问题与选择便于审计与版本追溯。
 */
export function HumanQuestionCard({
  msgId,
  taskId,
  questionSet
}: {
  msgId: string
  taskId: string
  questionSet: HumanQuestionSet
}): JSX.Element {
  const answerHumanQuestions = useApp((s) => s.answerHumanQuestions)
  const stopTask = useApp((s) => s.stopTask)
  const [index, setIndex] = useState(0)
  const [collapsed, setCollapsed] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  // 每题草稿答案：optionIds / text / deferred，提交时统一校验
  const [draft, setDraft] = useState<Record<string, { optionIds: string[]; text: string; deferred: boolean }>>({})

  const questions = questionSet.questions
  const answered = questionSet.status !== 'pending'
  const current = questions[Math.min(index, Math.max(questions.length - 1, 0))]

  const answeredSummary = useMemo(
    () =>
      questions.map((q) => ({
        id: q.id,
        title: q.title,
        affects: q.affects,
        label: answerLabel(q, questionSet.answers?.find((a) => a.questionId === q.id))
      })),
    [questions, questionSet.answers]
  )

  if (answered) {
    return (
      <div className="hq-card done" aria-label="已提交的文书决策">
        <div className="hq-head">
          <Icon id="i-check" />
          <span className="hq-title">{questionSet.title}</span>
          <span className="hq-state">{questionSet.status === 'cancelled' ? '已取消' : '已回答'}</span>
        </div>
        <ul className="hq-summary">
          {answeredSummary.map((row) => (
            <li key={row.id}>
              <span className="hq-sum-q">{row.title}</span>
              <span className="hq-sum-a">{row.label}</span>
              {row.affects && <span className="hq-sum-af">影响：{row.affects}</span>}
            </li>
          ))}
        </ul>
        <p className="hq-note">该选择已作为用户确认写入本次文书；如需调整，可直接在对话中说明新的方向。</p>
      </div>
    )
  }

  if (!current) return <></>

  const cur = draft[current.id] ?? { optionIds: [], text: '', deferred: false }
  const patch = (next: Partial<{ optionIds: string[]; text: string; deferred: boolean }>): void => {
    setDraft((d) => ({ ...d, [current.id]: { ...cur, ...next } }))
  }
  const toggle = (id: string): void => {
    if (current.kind === 'multi_choice') {
      const has = cur.optionIds.includes(id)
      patch({
        optionIds: has ? cur.optionIds.filter((x) => x !== id) : [...cur.optionIds, id],
        deferred: false
      })
    } else {
      patch({ optionIds: cur.optionIds[0] === id ? [] : [id], deferred: false })
    }
  }
  const hasAnswer = (q: HumanQuestion): boolean => {
    const a = draft[q.id] ?? { optionIds: [], text: '', deferred: false }
    if (a.deferred) return true
    if (q.kind === 'text') return a.text.trim().length > 0
    return a.optionIds.length > 0 || (q.allowOther !== false && a.text.trim().length > 0)
  }
  const canSubmit = (): boolean => questions.every((q) => q.required === false || hasAnswer(q))

  const submit = async (): Promise<void> => {
    const answers: HumanAnswer[] = questions.map((q) => {
      const a = draft[q.id] ?? { optionIds: [], text: '', deferred: false }
      if (a.deferred) return { questionId: q.id, deferred: true }
      const text = a.text.trim()
      return {
        questionId: q.id,
        ...(a.optionIds.length ? { optionIds: a.optionIds } : {}),
        ...(text ? { text } : {})
      }
    })
    setSubmitting(true)
    const ok = await answerHumanQuestions(msgId, taskId, questionSet.id, answers)
    setSubmitting(false)
    if (ok) setCollapsed(true)
  }

  if (collapsed) {
    return (
      <button
        type="button"
        className="hq-collapsed"
        aria-expanded={false}
        onClick={() => setCollapsed(false)}
      >
        <Icon id="i-bulb" />
        <span className="hq-title">{questionSet.title}</span>
        <span className="hq-collapsed-tip">点击展开继续回答</span>
        <span className="hq-chevron" aria-hidden="true">›</span>
      </button>
    )
  }

  const last = index >= questions.length - 1
  return (
    <div className="hq-card" role="dialog" aria-label={questionSet.title}>
      <div className="hq-head">
        <Icon id="i-bulb" />
        <span className="hq-title">{questionSet.title}</span>
        {questions.length > 1 && (
          <span className="hq-counter" aria-live="polite">
            {index + 1} / {questions.length}
          </span>
        )}
        <button
          type="button"
          className="hq-icon-btn"
          aria-label="收起问题卡（稍后回答）"
          onClick={() => setCollapsed(true)}
        >
          <Icon id="i-x" />
        </button>
      </div>
      {questionSet.contextDigest && <p className="hq-digest">{questionSet.contextDigest}</p>}

      <div className="hq-question">
        <h4 className="hq-q-title">
          {current.title}
          {current.required === false && <span className="hq-optional">可选</span>}
        </h4>
        {current.affects && <p className="hq-affects">影响：{current.affects}</p>}
        <QuestionBody
          question={current}
          optionIds={cur.optionIds}
          text={cur.text}
          deferred={cur.deferred}
          disabled={submitting}
          onToggle={toggle}
          onText={(v) => patch({ text: v })}
        />
        {cur.deferred && <p className="hq-defer-note">已选择暂不决定：相关部分将以 xx 标注并进入待核实。</p>}
      </div>

      <div className="hq-actions">
        {questions.length > 1 && (
          <button
            type="button"
            className="btn-ghost hq-btn"
            disabled={index === 0 || submitting}
            onClick={() => setIndex((i) => Math.max(0, i - 1))}
          >
            <Icon id="i-chevl" />
            上一题
          </button>
        )}
        {current.allowDefer !== false && (
          <button
            type="button"
            className="btn-ghost hq-btn"
            disabled={submitting}
            onClick={() => patch(cur.deferred ? { deferred: false } : { deferred: true, text: '', optionIds: [] })}
          >
            {cur.deferred ? '改为回答' : '暂不决定'}
          </button>
        )}
        <span className="hq-spacer" />
        <button
          type="button"
          className="btn-ghost hq-btn danger"
          disabled={submitting}
          onClick={() => void stopTask(msgId, taskId)}
        >
          取消任务
        </button>
        {!last ? (
          <button
            type="button"
            className="btn-primary hq-btn"
            disabled={submitting || (current.required !== false && !hasAnswer(current))}
            onClick={() => setIndex((i) => Math.min(questions.length - 1, i + 1))}
          >
            下一步
            <Icon id="i-chevr" />
          </button>
        ) : (
          <button
            type="button"
            className="btn-primary hq-btn"
            disabled={submitting || !canSubmit()}
            onClick={() => void submit()}
          >
            {submitting ? '提交中…' : '提交并继续'}
          </button>
        )}
      </div>
    </div>
  )
}
