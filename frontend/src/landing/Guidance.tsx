import { GUIDANCE, SOURCES } from './content'
import { Reveal } from './Reveal'
import { ScrollText } from './icons'

/** Static, wrapping cloud of the guidance the product is structured around
 *  (no marquee - nothing loops), plus the real sources it draws on. */
export function Guidance() {
  return (
    <section aria-labelledby="guidance-heading" className="relative pb-28 sm:pb-36">
      <Reveal className="mx-auto max-w-4xl px-5">
        <h2 id="guidance-heading" className="lp-eyebrow text-center">
          Structured around the guidance your reviewers use
        </h2>
        <ul className="mx-auto mt-7 flex list-none flex-wrap justify-center gap-2.5 p-0">
          {GUIDANCE.map(g => (
            <li
              key={g}
              className="inline-flex h-9 items-center gap-2 rounded-pill border border-line bg-white/[0.04] px-3.5 text-small font-medium text-fg"
            >
              <ScrollText aria-hidden className="h-4 w-4 flex-none text-accent-300" />
              {g}
            </li>
          ))}
        </ul>
        <p className="mt-7 text-center text-small text-fg-muted">Grounded in real sources: {SOURCES.join(' · ')}</p>
      </Reveal>
    </section>
  )
}
