import type { ReactNode } from 'react'

/**
 * Scroll-reveal helpers. They only add data attributes: the motion engine
 * (motion/engine.ts) finds `[data-lp="fade"]`, `[data-lp="split"]`, ... and
 * animates them. Without the engine (reduced motion, no JS, tests) the content
 * is plain and fully visible.
 */

/** Fade a block up the first time it scrolls into view. */
export function Reveal({
  children,
  delay = 0,
  className = '',
}: {
  children: ReactNode
  /** Seconds. */
  delay?: number
  className?: string
}) {
  return (
    <div data-lp="fade" data-lp-delay={delay || undefined} className={className}>
      {children}
    </div>
  )
}

/** Children marked with <StaggerItem> fade up one after another. */
export function Stagger({
  children,
  className = '',
  as: Tag = 'div',
  gap,
}: {
  children: ReactNode
  className?: string
  as?: 'div' | 'ul' | 'ol'
  /** Seconds between items (default 0.1). */
  gap?: number
}) {
  return (
    <Tag data-lp="stagger" data-lp-stagger={gap} className={className}>
      {children}
    </Tag>
  )
}

export function StaggerItem({
  children,
  className = '',
  as: Tag = 'div',
}: {
  children: ReactNode
  className?: string
  as?: 'div' | 'li'
}) {
  return (
    <Tag data-lp-item className={className}>
      {children}
    </Tag>
  )
}

/** Small label above a section heading (sentence case, grey; cyan with `accent`). */
export function Eyebrow({ children, accent = false }: { children: ReactNode; accent?: boolean }) {
  return <p className={`lp-eyebrow ${accent ? 'lp-eyebrow--accent' : ''}`}>{children}</p>
}

/** Centred section heading block (eyebrow, big blur-in h2, optional lead). */
export function SectionHeading({
  id,
  eyebrow,
  title,
  lead,
}: {
  /** id for the h2 (sections use it for aria-labelledby). */
  id?: string
  eyebrow: string
  title: string
  lead?: string
}) {
  return (
    <div className="mx-auto max-w-3xl text-center">
      <div data-lp="fade">
        <Eyebrow accent>{eyebrow}</Eyebrow>
      </div>
      <h2 id={id} data-lp="split" className="lp-h2 mt-4">
        {title}
      </h2>
      {lead ? (
        <p data-lp="fade" data-lp-delay={0.3} className="lp-lead mx-auto mt-6 max-w-2xl">
          {lead}
        </p>
      ) : null}
    </div>
  )
}
