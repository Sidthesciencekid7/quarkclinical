import { AlertTriangle, Check, Info, XCircle, cx } from '../ui'
import { FormattedText } from './safeFormat'
import type { ThreadRow } from './chatReducer'

export const UNVERIFIED_NOTE = 'Check the cards for authoritative values.'

/** Three softly pulsing dots: the assistant is working and has not written anything yet. */
export function TypingDots() {
  return (
    <span className="inline-flex items-center gap-1 py-2" aria-hidden>
      {[0, 1, 2].map(i => (
        <span key={i} className="h-1.5 w-1.5 animate-pulse rounded-full bg-ink-400" style={{ animationDelay: `${i * 160}ms` }} />
      ))}
    </span>
  )
}

/**
 * One message of the thread.
 *  - user: a right-aligned light bubble (plain text, newlines kept).
 *  - assistant: plain text through the safe formatter (paragraphs, lists, bold, code only).
 *  - note: a card click recorded by the server (a quiet line).
 *  - notice: a system message from the turn (e.g. "reply cut off").
 * Everything is rendered as text nodes; there is no raw HTML anywhere.
 */
export function MessageBubble({ row }: { row: ThreadRow }) {
  if (row.kind === 'user') {
    return (
      <div className="flex justify-end">
        <div className="max-w-[85%] whitespace-pre-wrap break-words rounded-[20px] bg-ink-100 px-4 py-2.5 text-body text-ink-950" data-role="user">
          {row.text}
        </div>
      </div>
    )
  }

  if (row.kind === 'note') {
    if (!row.text) return null
    return (
      <p className="flex items-center gap-1.5 text-small text-fg-subtle" data-role="note">
        <Check aria-hidden className="h-3.5 w-3.5 flex-none" />
        <span className="min-w-0 break-words">{row.text}</span>
      </p>
    )
  }

  if (row.kind === 'notice') {
    const Icon = row.level === 'error' ? XCircle : row.level === 'warning' ? AlertTriangle : Info
    return (
      <p
        role={row.level === 'error' ? 'alert' : undefined}
        className={cx(
          'flex items-start gap-2 text-small',
          row.level === 'error' ? 'text-[color:var(--bad-fg)]' : row.level === 'warning' ? 'text-[color:var(--warn-fg)]' : 'text-fg-muted',
        )}
        data-role="notice"
      >
        <Icon aria-hidden className="mt-0.5 h-4 w-4 flex-none" />
        <span className="min-w-0 break-words">{row.text}</span>
      </p>
    )
  }

  if (row.kind !== 'assistant') return null
  const hasText = row.text.trim() !== ''
  const unverified = row.flags?.unverified_numbers ?? []
  if (!hasText && !row.streaming) return null
  return (
    <div data-role="assistant" className="text-body text-fg">
      {hasText ? <FormattedText text={row.text} /> : <TypingDots />}
      {unverified.length > 0 ? (
        <p className="mt-2 flex items-start gap-1.5 text-small text-[color:var(--warn-fg)]" data-testid="unverified-note">
          <AlertTriangle aria-hidden className="mt-0.5 h-3.5 w-3.5 flex-none" />
          <span>{UNVERIFIED_NOTE}</span>
        </p>
      ) : null}
    </div>
  )
}
