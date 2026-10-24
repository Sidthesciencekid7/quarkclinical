import { forwardRef, type ElementType, type HTMLAttributes, type ReactNode } from 'react'
import { cx } from './cx'

export type CardPadding = 'none' | 'sm' | 'md' | 'lg'

const PADDING: Record<CardPadding, string> = {
  none: '',
  sm: 'p-4',
  md: 'p-5',
  lg: 'p-6',
}

export interface CardProps extends HTMLAttributes<HTMLElement> {
  as?: 'div' | 'section' | 'article' | 'li' | 'aside'
  /** Default `md` (20px). */
  padding?: CardPadding
  /** Adds hover elevation + pointer cursor (when the whole card is clickable). */
  interactive?: boolean
}

/** White card: ink-200 border, 16px radius, card shadow. Uses the `.card` class. */
export const Card = forwardRef<HTMLElement, CardProps>(function Card(
  { as = 'div', padding = 'md', interactive, className, children, ...rest },
  ref,
) {
  const Tag = as as ElementType
  return (
    <Tag
      ref={ref}
      className={cx(
        'card',
        PADDING[padding],
        interactive &&
          'cursor-pointer transition duration-fast hover:border-line-strong hover:shadow-pop',
        className,
      )}
      {...rest}
    >
      {children}
    </Tag>
  )
})

export interface CardHeaderProps {
  title: ReactNode
  subtitle?: ReactNode
  /** Leading icon (rendered in a small neutral square). */
  icon?: ReactNode
  /** Trailing status badge(s). */
  badge?: ReactNode
  /** Trailing actions (e.g. an IconButton). */
  actions?: ReactNode
  /** Heading level for the title (default 3). */
  level?: 2 | 3 | 4
  className?: string
}

export function CardHeader({ title, subtitle, icon, badge, actions, level = 3, className }: CardHeaderProps) {
  const Heading = `h${level}` as 'h2' | 'h3' | 'h4'
  return (
    <div className={cx('flex items-start gap-3', className)}>
      {icon ? (
        <span
          aria-hidden
          className="mt-0.5 flex h-8 w-8 flex-none items-center justify-center rounded-xl bg-surface-muted text-fg-muted [&>svg]:h-4 [&>svg]:w-4"
        >
          {icon}
        </span>
      ) : null}
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-2">
          <Heading className="text-h3 text-fg">{title}</Heading>
          {badge}
        </div>
        {subtitle ? <p className="mt-0.5 text-small text-fg-muted">{subtitle}</p> : null}
      </div>
      {actions ? <div className="flex flex-none items-center gap-1">{actions}</div> : null}
    </div>
  )
}

/** Right-aligned row of pill buttons. */
export function CardFooter({ className, children }: { className?: string; children: ReactNode }) {
  return <div className={cx('mt-4 flex flex-wrap items-center justify-end gap-2', className)}>{children}</div>
}

export interface ChatCardProps extends Omit<HTMLAttributes<HTMLElement>, 'title'> {
  icon?: ReactNode
  title: ReactNode
  /** A `Badge` (e.g. "Done", "Needs input"). */
  status?: ReactNode
  /**
   * Replaced by a newer card: drops to 60% opacity and every control in
   * `actions` is natively disabled (via a disabled fieldset).
   */
  superseded?: boolean
  /** Right-aligned pill buttons in the footer. */
  actions?: ReactNode
  children?: ReactNode
}

/**
 * Presentational shell for a chat card: header (icon, title, status badge),
 * body, and a right-aligned footer of pill buttons. Content comes from the
 * server; this component only lays it out.
 */
export function ChatCard({ icon, title, status, superseded, actions, className, children, ...rest }: ChatCardProps) {
  return (
    <article
      aria-disabled={superseded || undefined}
      data-superseded={superseded || undefined}
      className={cx('card p-5 transition-opacity duration-fast', superseded && 'opacity-60', className)}
      {...rest}
    >
      <CardHeader title={title} icon={icon} badge={status} />
      {children ? <div className="mt-3 text-body text-fg">{children}</div> : null}
      {actions ? (
        <fieldset disabled={superseded} className="m-0 min-w-0 border-0 p-0">
          <CardFooter>{actions}</CardFooter>
        </fieldset>
      ) : null}
    </article>
  )
}
