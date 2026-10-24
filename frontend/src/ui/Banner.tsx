import type { ReactNode } from 'react'
import { cx } from './cx'
import { AlertTriangle, CheckCircle2, Info, X, XCircle } from './icons'
import { IconButton } from './IconButton'

export type BannerTone = 'info' | 'success' | 'warning' | 'danger'

const TONE: Record<BannerTone, { box: string; icon: ReactNode; role: 'status' | 'alert' }> = {
  info: {
    box: 'bg-[var(--info-bg)] border-[color:var(--info-border)] text-[color:var(--info-fg)]',
    icon: <Info aria-hidden />,
    role: 'status',
  },
  success: {
    box: 'bg-[var(--ok-bg)] border-[color:var(--ok-border)] text-[color:var(--ok-fg)]',
    icon: <CheckCircle2 aria-hidden />,
    role: 'status',
  },
  warning: {
    box: 'bg-[var(--warn-bg)] border-[color:var(--warn-border)] text-[color:var(--warn-fg)]',
    icon: <AlertTriangle aria-hidden />,
    role: 'alert',
  },
  danger: {
    box: 'bg-[var(--bad-bg)] border-[color:var(--bad-border)] text-[color:var(--bad-fg)]',
    icon: <XCircle aria-hidden />,
    role: 'alert',
  },
}

export interface BannerProps {
  /** Default `info`. `warning` and `danger` are announced assertively (role=alert). */
  tone?: BannerTone
  title?: ReactNode
  children?: ReactNode
  /** Trailing action, e.g. a small `Button`. */
  action?: ReactNode
  /** Shows a dismiss (x) button. */
  onDismiss?: () => void
  className?: string
}

/** Inline status message. Colours are surface-aware and meet AA on light and dark. */
export function Banner({ tone = 'info', title, children, action, onDismiss, className }: BannerProps) {
  const t = TONE[tone]
  return (
    <div
      role={t.role}
      className={cx('flex items-start gap-3 rounded-card border px-4 py-3', t.box, className)}
    >
      <span className="mt-0.5 flex flex-none [&>svg]:h-4 [&>svg]:w-4">{t.icon}</span>
      <div className="min-w-0 flex-1 text-small">
        {title ? <p className="font-semibold">{title}</p> : null}
        {children ? <div className={cx('text-fg', title && 'mt-0.5')}>{children}</div> : null}
      </div>
      {action ? <div className="flex flex-none items-center">{action}</div> : null}
      {onDismiss ? (
        <IconButton label="Dismiss" icon={<X />} size="sm" onClick={onDismiss} className="-mr-2 -mt-1 text-current" />
      ) : null}
    </div>
  )
}
