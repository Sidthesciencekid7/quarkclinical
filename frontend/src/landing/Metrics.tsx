import { METRICS } from './content'
import { SectionHeading, Stagger, StaggerItem } from './Reveal'

/**
 * Four countable facts, each counted up when it scrolls in, with a thin bar
 * that draws under it. Every number is a plain fact about the product (see
 * content.ts); nothing here is a customer count, a performance claim or a
 * regulatory status.
 */
export function Metrics() {
  return (
    <section id="proof" aria-labelledby="proof-heading" className="relative py-28 sm:py-36">
      <div aria-hidden className="lp-hair absolute inset-x-0 top-0" />
      <div className="mx-auto max-w-6xl px-5">
        <SectionHeading
          id="proof-heading"
          eyebrow="By the numbers"
          title="Counted, not claimed."
          lead="The language model writes prose. What a reviewer would check is calculated by code and handed to it as fixed facts."
        />

        <Stagger as="ul" className="m-0 mt-16 grid list-none gap-4 p-0 sm:grid-cols-2 lg:grid-cols-4">
          {METRICS.map(m => (
            <StaggerItem as="li" key={m.label} className="lp-card flex flex-col p-6 sm:p-7">
              <p className="m-0 text-[clamp(3.5rem,6vw,5rem)] font-bold leading-none tracking-[-0.06em] text-fg tabular-nums">
                <span aria-hidden data-lp="count" data-to={m.value}>
                  {m.value}
                </span>
                <span className="sr-only">{m.value}</span>
              </p>
              <div aria-hidden className="mt-5 h-[2px] w-full overflow-hidden rounded-full bg-white/10">
                <div data-lp="bar" className="h-full w-full bg-gradient-to-r from-accent-400 to-accent-300" />
              </div>
              <h3 className="mb-0 mt-5 text-[1.0625rem] font-semibold leading-6 text-fg">{m.label}</h3>
              <p className="m-0 mt-2 text-[0.9375rem] leading-6 text-fg-muted">{m.note}</p>
            </StaggerItem>
          ))}
        </Stagger>
      </div>
    </section>
  )
}
