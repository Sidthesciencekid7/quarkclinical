import { forwardRef, type ElementType, type HTMLAttributes } from 'react'
import { cx } from './cx'

export interface SurfaceProps extends HTMLAttributes<HTMLElement> {
  /** `light` (the app) or `dark` (navy landing / glass surfaces). */
  tone?: 'light' | 'dark'
  as?: ElementType
}

/**
 * Sets `data-surface`, which flips every surface-aware token (text, borders,
 * pill buttons, composer, chips...) for everything inside it, and paints the
 * matching background. Surfaces nest: a light Surface inside a dark one resets.
 */
export const Surface = forwardRef<HTMLElement, SurfaceProps>(function Surface(
  { tone = 'light', as: Tag = 'div', className, children, ...rest },
  ref,
) {
  return (
    <Tag ref={ref} data-surface={tone} className={cx('bg-surface text-fg', className)} {...rest}>
      {children}
    </Tag>
  )
})
