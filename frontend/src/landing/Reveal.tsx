import type { CSSProperties, ReactNode } from 'react'
import { useReveal } from '../lib/useReveal'

/** Fade/slide a block in the first time it scrolls into view (see `.reveal`
 *  in index.css). Reduced motion and browsers without IntersectionObserver
 *  show the content immediately. */
export function Reveal({
  children,
  delay = 0,
  className = '',
}: {
  children: ReactNode
  delay?: number
  className?: string
}) {
  const { ref, shown } = useReveal<HTMLDivElement>()
  return (
    <div
      ref={ref}
      className={`reveal ${shown ? 'is-visible' : ''} ${className}`}
      style={{ '--reveal-delay': `${delay}ms` } as CSSProperties}
    >
      {children}
    </div>
  )
}

/** Small uppercase label above a section heading. */
export function Eyebrow({ children }: { children: ReactNode }) {
  return <p className="text-eyebrow uppercase text-accent-700">{children}</p>
}

/** Centred section heading block (eyebrow, h2, optional lead paragraph). */
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
    <div className="mx-auto max-w-2xl text-center">
      <Eyebrow>{eyebrow}</Eyebrow>
      <h2 id={id} className="mt-3 text-[1.75rem] font-semibold leading-tight tracking-[-0.025em] text-fg sm:text-[2.25rem]">
        {title}
      </h2>
      {lead ? <p className="mt-4 text-[1.0625rem] leading-relaxed text-fg-muted">{lead}</p> : null}
    </div>
  )
}
