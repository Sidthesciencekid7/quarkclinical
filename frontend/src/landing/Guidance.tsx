import { GUIDANCE, SOURCES } from './content'
import { Reveal } from './Reveal'
import { ScrollText } from './icons'

/** Static, wrapping cloud of the guidance the product is structured around
 *  (no marquee - nothing moves), plus the real sources it draws on. */
export function Guidance() {
  return (
    <section
      aria-labelledby="guidance-heading"
      className="border-y border-line bg-surface-canvas py-14"
    >
      <Reveal className="mx-auto max-w-4xl px-5">
        <h2 id="guidance-heading" className="text-center text-eyebrow uppercase text-fg-subtle">
          Structured around the guidance your reviewers use
        </h2>
        <ul className="mx-auto mt-6 flex list-none flex-wrap justify-center gap-2.5 p-0">
          {GUIDANCE.map(g => (
            <li
              key={g}
              className="inline-flex h-9 items-center gap-2 rounded-pill border border-line bg-surface px-3.5 text-small font-medium text-fg shadow-xs"
            >
              <ScrollText aria-hidden className="h-4 w-4 flex-none text-accent-700" />
              {g}
            </li>
          ))}
        </ul>
        <p className="mt-6 text-center text-small text-fg-muted">
          Grounded in real sources: {SOURCES.join(' · ')}
        </p>
      </Reveal>
    </section>
  )
}
