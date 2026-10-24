import type { ReactNode } from 'react'
import { cx } from './cx'

export interface EmptyStateProps {
  icon?: ReactNode
  title: ReactNode
  description?: ReactNode
  /** Primary action, e.g. `<Button>New protocol</Button>`. */
  action?: ReactNode
  className?: string
}

/** Centred empty / zero-data panel. */
export function EmptyState({ icon, title, description, action, className }: EmptyStateProps) {
  return (
    <div className={cx('flex flex-col items-center px-6 py-12 text-center', className)}>
      {icon ? (
        <span
          aria-hidden
          className="mb-4 flex h-12 w-12 items-center justify-center rounded-tile bg-surface-muted text-fg-muted [&>svg]:h-5 [&>svg]:w-5"
        >
          {icon}
        </span>
      ) : null}
      <h3 className="text-h3 text-fg">{title}</h3>
      {description ? <p className="mt-1 max-w-sm text-body text-fg-muted">{description}</p> : null}
      {action ? <div className="mt-5">{action}</div> : null}
    </div>
  )
}
