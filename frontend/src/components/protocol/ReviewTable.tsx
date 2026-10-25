import type { ReactNode } from 'react'
import { cx } from '../../ui'

export interface ReviewRow {
  label: string
  value: ReactNode
  /** Small trailing badge(s), e.g. "Default" or "Check". */
  badge?: ReactNode
  /** Amber background: the value still needs the reader's attention. */
  highlight?: boolean
}

export interface ReviewTableProps {
  rows: ReviewRow[]
  /** Accessible name of the table (it is a description list). */
  ariaLabel?: string
  className?: string
}

/**
 * The "review before you commit" table (moved out of the retired
 * GenerateButton wizard step). Presentational: it renders the rows it is
 * given, so every value shown comes from whoever built the rows (for chat, the
 * server's summary card).
 */
export function ReviewTable({ rows, ariaLabel, className }: ReviewTableProps) {
  if (rows.length === 0) return null
  return (
    <dl
      aria-label={ariaLabel}
      className={cx('m-0 divide-y divide-line overflow-hidden rounded-input border border-line text-small', className)}
    >
      {rows.map((row, i) => (
        <div
          key={`${row.label}-${i}`}
          className={cx(
            'flex flex-col gap-0.5 px-4 py-2.5 sm:flex-row sm:items-baseline sm:gap-4',
            row.highlight && 'bg-[var(--warn-bg)]',
          )}
        >
          <dt className="flex-none text-eyebrow uppercase text-fg-subtle sm:w-36">{row.label}</dt>
          <dd className="m-0 flex min-w-0 flex-1 flex-wrap items-center gap-x-2 gap-y-1 font-medium text-fg">
            <span className="min-w-0 break-words">{row.value}</span>
            {row.badge}
          </dd>
        </div>
      ))}
    </dl>
  )
}

export default ReviewTable
