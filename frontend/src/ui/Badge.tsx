import type { HTMLAttributes, ReactNode } from 'react'
import { cx } from './cx'

export type BadgeVariant = 'neutral' | 'solid' | 'success' | 'warning' | 'danger' | 'info'

const VARIANT_CLASS: Record<BadgeVariant, string> = {
  neutral: 'badge-neutral',
  solid: 'badge-solid',
  success: 'badge-success',
  warning: 'badge-warning',
  danger: 'badge-danger',
  info: 'badge-info',
}

export interface BadgeProps extends HTMLAttributes<HTMLSpanElement> {
  /** `neutral` ("Beta"), `solid` ("New"), or a status tone. Default `neutral`. */
  variant?: BadgeVariant
  icon?: ReactNode
  /** Prefix a small status dot in the badge's colour. */
  dot?: boolean
}

/** Small status pill (h-6). Not interactive. */
export function Badge({ variant = 'neutral', icon, dot, className, children, ...rest }: BadgeProps) {
  return (
    <span className={cx('badge', VARIANT_CLASS[variant], className)} {...rest}>
      {dot ? <span aria-hidden className="h-1.5 w-1.5 rounded-full bg-current" /> : icon}
      {children}
    </span>
  )
}
