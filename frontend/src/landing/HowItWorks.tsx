import { STEPS } from './content'
import { ProductShot } from './ProductShot'
import { Reveal, SectionHeading } from './Reveal'

export function HowItWorks() {
  return (
    <section id="how" aria-labelledby="how-heading" className="scroll-mt-24 bg-surface-canvas py-24">
      <div className="mx-auto max-w-6xl px-5">
        <Reveal>
          <SectionHeading
            id="how-heading"
            eyebrow="How it works"
            title="From a description to a reviewed draft"
          />
        </Reveal>

        <div className="relative mt-14">
          <div
            aria-hidden
            className="absolute left-[12%] right-[12%] top-7 hidden h-px bg-gradient-to-r from-ink-200 via-ink-300 to-ink-200 md:block"
          />
          <ol className="relative m-0 grid list-none gap-8 p-0 md:grid-cols-4 md:gap-6">
          {STEPS.map((s, i) => (
            <li key={s.n} className="relative">
              <Reveal delay={i * 110}>
                <div className="relative z-10 flex gap-4 md:flex-col md:items-center md:text-center">
                  <span
                    aria-hidden
                    className="flex h-14 w-14 flex-none items-center justify-center rounded-2xl bg-ink-950 text-[1.0625rem] font-semibold tabular-nums text-white shadow-card"
                  >
                    {s.n}
                  </span>
                  <div>
                    <h3 className="mb-1.5 text-h3 text-fg">
                      <span className="sr-only">Step {Number(s.n)}: </span>
                      {s.title}
                    </h3>
                    <p className="m-0 text-small text-fg-muted">{s.body}</p>
                  </div>
                </div>
              </Reveal>
            </li>
          ))}
          </ol>
        </div>

        <Reveal className="mt-20">
          <div className="grid items-center gap-10 lg:grid-cols-5">
            <div className="order-2 lg:order-1 lg:col-span-3">
              <ProductShot
                src="/screenshots/review-design.webp"
                alt="The design review showing the population, dose and study indication decisions, each with its rationale"
                label="Review design"
                width={654}
                height={556}
              />
            </div>
            <div className="order-1 lg:order-2 lg:col-span-2">
              <h3 className="text-[1.5rem] font-semibold leading-tight tracking-[-0.02em] text-fg sm:text-[1.875rem]">
                Every design decision, explained
              </h3>
              <p className="mt-4 text-[1.0625rem] leading-relaxed text-fg-muted">
                Before anything is written, QuarkClinical shows its reasoning: why patients instead of healthy
                volunteers, why multiple doses to steady state, which indication gives the cleanest PK comparison.
                You can change any of them, or enter your own indication and see how it compares.
              </p>
            </div>
          </div>
        </Reveal>
      </div>
    </section>
  )
}
