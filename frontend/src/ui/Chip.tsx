import { forwardRef, type ButtonHTMLAttributes, type ReactNode } from 'react'
import { cx } from './cx'
import { Check } from './icons'

export interface ChipProps extends Omit<ButtonHTMLAttributes<HTMLButtonElement>, 'color'> {
  /** Leading 16px icon (suggestion chips: e.g. `<Sparkles />`). */
  icon?: ReactNode
  /**
   * Makes the chip selectable: sets `aria-pressed` and shows a check mark when
   * true. Leave undefined for a plain suggestion chip (no aria-pressed).
   */
  selected?: boolean
}

/** Pill chip: a suggestion (click to fill/send) or, with `selected`, a toggle. */
export const Chip = forwardRef<HTMLButtonElement, ChipProps>(function Chip(
  { icon, selected, type = 'button', className, children, ...rest },
  ref,
) {
  const selectable = selected !== undefined
  return (
    <button
      ref={ref}
      type={type}
      aria-pressed={selectable ? selected : undefined}
      className={cx('chip', selectable && selected && 'chip-selected', className)}
      {...rest}
    >
      {selectable && selected ? <Check aria-hidden /> : icon}
      {children}
    </button>
  )
})
