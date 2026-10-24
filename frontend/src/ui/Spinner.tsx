import { cx } from './cx'

export interface SpinnerProps {
  /** Pixel size (default 16). */
  size?: number
  /** Accessible name. Omit when the spinner sits next to visible text (it is then hidden from AT). */
  label?: string
  className?: string
}

/** Indeterminate spinner. Inherits `currentColor`. Keeps turning (slowly) under reduced motion. */
export function Spinner({ size = 16, label, className }: SpinnerProps) {
  return (
    <svg
      className={cx('ui-spinner animate-spin shrink-0', className)}
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      role={label ? 'status' : undefined}
      aria-label={label}
      aria-hidden={label ? undefined : true}
      data-ui="spinner"
    >
      <circle cx="12" cy="12" r="9" stroke="currentColor" strokeOpacity="0.25" strokeWidth="3" />
      <path d="M21 12a9 9 0 0 0-9-9" stroke="currentColor" strokeWidth="3" strokeLinecap="round" />
    </svg>
  )
}
