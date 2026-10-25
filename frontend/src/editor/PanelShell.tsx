import type { ReactNode } from 'react'
import { Button, ChevronRight, cx, RefreshCw } from '../ui'
import type { BadgeVariant } from '../ui'

/**
 * Shared frame for the QC / Coverage / Risk panels: a right-hand column with a
 * titled header (icon tile, title, Re-run pill, last-checked time), a summary
 * slot, a scrolling body and a footnote. Overlays the editor on narrow screens.
 */
export const PANEL_CLASS =
  'absolute inset-y-0 right-0 z-20 flex w-[min(100%,24rem)] flex-col overflow-hidden border-l border-line bg-surface shadow-pop md:static md:z-auto md:w-96 md:flex-none md:shadow-none'

export type FindingTone = 'danger' | 'warning' | 'success' | 'info' | 'neutral'

/** Literal class strings so Tailwind keeps them. */
export const FINDING_TONE: Record<
  FindingTone,
  { box: string; text: string; bar: string; badge: BadgeVariant }
> = {
  danger: {
    box: 'border-[color:var(--bad-border)] bg-[var(--bad-bg)]',
    text: 'text-[color:var(--bad-fg)]',
    bar: 'bg-red-500',
    badge: 'danger',
  },
  warning: {
    box: 'border-[color:var(--warn-border)] bg-[var(--warn-bg)]',
    text: 'text-[color:var(--warn-fg)]',
    bar: 'bg-amber-500',
    badge: 'warning',
  },
  success: {
    box: 'border-[color:var(--ok-border)] bg-[var(--ok-bg)]',
    text: 'text-[color:var(--ok-fg)]',
    bar: 'bg-emerald-500',
    badge: 'success',
  },
  info: {
    box: 'border-[color:var(--info-border)] bg-[var(--info-bg)]',
    text: 'text-[color:var(--info-fg)]',
    bar: 'bg-accent-600',
    badge: 'info',
  },
  neutral: {
    box: 'border-line bg-surface-muted',
    text: 'text-fg-muted',
    bar: 'bg-ink-300',
    badge: 'neutral',
  },
}

interface PanelShellProps {
  title: string
  icon: ReactNode
  /** Accessible name of the region (defaults to the title). */
  label?: string
  loading: boolean
  onRerun: () => void
  rerunLabel?: string
  loadingLabel?: string
  lastChecked: Date | null
  lastCheckedPrefix?: string
  /** Rendered under the header row (readiness banner, counts...). */
  summary?: ReactNode
  /** Scrolling body. */
  children: ReactNode
}

export function PanelShell({
  title,
  icon,
  label,
  loading,
  onRerun,
  rerunLabel = 'Re-run',
  loadingLabel = 'Checking…',
  lastChecked,
  lastCheckedPrefix = 'Checked',
  summary,
  children,
}: PanelShellProps) {
  return (
    <aside aria-label={label ?? title} className={PANEL_CLASS}>
      <div className="flex-none border-b border-line px-4 pb-3 pt-4">
        <div className="flex items-center justify-between gap-2">
          <div className="flex min-w-0 items-center gap-2.5">
            <span
              aria-hidden
              className="flex h-8 w-8 flex-none items-center justify-center rounded-xl bg-surface-muted text-fg-muted [&>svg]:h-4 [&>svg]:w-4"
            >
              {icon}
            </span>
            <h2 className="truncate text-h3 text-fg">{title}</h2>
          </div>
          <div className="flex flex-none flex-col items-end gap-0.5">
            <Button
              variant="secondary"
              size="sm"
              onClick={onRerun}
              loading={loading}
              leftIcon={<RefreshCw aria-hidden />}
            >
              {loading ? loadingLabel : rerunLabel}
            </Button>
            {lastChecked && !loading ? (
              <span key={lastChecked.getTime()} className="qc-flash text-[11px] leading-none text-fg-subtle tabular-nums">
                {lastCheckedPrefix} {lastChecked.toLocaleTimeString()}
              </span>
            ) : null}
          </div>
        </div>
        {summary}
      </div>
      <div className="min-h-0 flex-1 space-y-2 overflow-y-auto px-3 py-3">{children}</div>
    </aside>
  )
}

interface FindingCardProps {
  tone: FindingTone
  icon: ReactNode
  title: ReactNode
  /** Small uppercase caption beside the title (category / severity). */
  caption?: ReactNode
  /** Right-aligned status text. */
  trailing?: ReactNode
  /** Secondary line under the title. */
  hint?: ReactNode
  children?: ReactNode
  jumpLabel?: string
  onJump?: () => void
}

/** One finding / requirement / risk factor. A coloured rail marks its tone. */
export function FindingCard({ tone, icon, title, caption, trailing, hint, children, jumpLabel, onJump }: FindingCardProps) {
  const t = FINDING_TONE[tone]
  return (
    <div className={cx('relative overflow-hidden rounded-xl border py-2.5 pl-4 pr-3', t.box)}>
      <span aria-hidden className={cx('absolute bottom-0 left-0 top-0 w-1', t.bar)} />
      <div className="flex items-start gap-2">
        <span aria-hidden className={cx('mt-0.5 flex flex-none [&>svg]:h-4 [&>svg]:w-4', t.text)}>
          {icon}
        </span>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-x-2">
            <h3 className={cx('text-small font-semibold', t.text)}>{title}</h3>
            {caption ? <span className="eyebrow">{caption}</span> : null}
            {trailing ? <span className={cx('ml-auto text-[11px] font-semibold uppercase tracking-wide', t.text)}>{trailing}</span> : null}
          </div>
          {hint ? <p className="mt-0.5 text-[12px] text-fg-subtle">{hint}</p> : null}
          {children ? <p className="mt-1 text-small text-fg-muted">{children}</p> : null}
          {onJump ? (
            <Button variant="ghost" size="sm" className="-ml-3 mt-1" rightIcon={<ChevronRight aria-hidden />} onClick={onJump}>
              {jumpLabel}
            </Button>
          ) : null}
        </div>
      </div>
    </div>
  )
}
