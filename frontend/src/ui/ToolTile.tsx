import { forwardRef, type ElementType, type HTMLAttributes, type MouseEvent, type ReactNode } from 'react'
import { cx } from './cx'

export interface ToolTileProps
  extends Omit<HTMLAttributes<HTMLElement>, 'onClick' | 'children' | 'className'> {
  label: ReactNode
  /** Small illustration (SVG / element), centred on the grey tile. */
  illustration: ReactNode
  /** Optional second line under the label. */
  description?: ReactNode
  /** Renders an `<a>` unless `as` is given. */
  href?: string
  /** Render as this element/component, e.g. react-router `Link` together with `to`. */
  as?: ElementType
  /** Forwarded to `as` (react-router `Link`). */
  to?: string
  onClick?: (e: MouseEvent<HTMLElement>) => void
  disabled?: boolean
  className?: string
}

/**
 * Home-screen tool tile: ink-100 tile (20px radius, ~1.15 aspect) with the
 * illustration centred and the label underneath (ElevenLabs home).
 */
export const ToolTile = forwardRef<HTMLElement, ToolTileProps>(function ToolTile(
  { label, illustration, description, href, as, onClick, disabled, className, ...rest },
  ref,
) {
  const Comp: ElementType = as ?? (href ? 'a' : 'button')
  const isButton = Comp === 'button'
  return (
    <Comp
      ref={ref}
      href={href}
      type={isButton ? 'button' : undefined}
      disabled={isButton ? disabled : undefined}
      aria-disabled={disabled || undefined}
      onClick={(e: MouseEvent<HTMLElement>) => {
        if (disabled) {
          e.preventDefault()
          return
        }
        onClick?.(e)
      }}
      className={cx(
        'group flex min-w-0 flex-col gap-3 rounded-tile text-left text-fg outline-none',
        disabled && 'opacity-40',
        className,
      )}
      {...rest}
    >
      <span className="tile transition-shadow duration-fast group-focus-visible:ring-2 group-focus-visible:ring-[color:var(--ring)] group-focus-visible:ring-offset-2">
        {illustration}
      </span>
      <span className="px-1 text-center">
        <span className="block text-body font-medium text-fg">{label}</span>
        {description ? <span className="mt-0.5 block text-small text-fg-muted">{description}</span> : null}
      </span>
    </Comp>
  )
})
