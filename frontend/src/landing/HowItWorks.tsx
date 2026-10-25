import { STEPS } from './content'
import { ProductShot } from './ProductShot'
import { Eyebrow, Reveal, SectionHeading, Stagger, StaggerItem } from './Reveal'

export function HowItWorks() {
  return (
    <section id="how" aria-labelledby="how-heading" className="relative scroll-mt-20 py-28 sm:py-36">
      <div aria-hidden className="lp-hair absolute inset-x-0 top-0" />
      <div className="mx-auto max-w-6xl px-5">
        <SectionHeading
          id="how-heading"
          eyebrow="How it works"
          title="From a description to a reviewed draft."
        />

        <div className="relative mt-20">
          {/* The rail draws itself as the section scrolls past. */}
          <div aria-hidden className="absolute left-[12%] right-[12%] top-7 hidden h-px bg-white/10 md:block">
            <div
              data-lp="line"
              className="h-full w-full bg-gradient-to-r from-accent-400 via-accent-300 to-accent-400/60"
            />
          </div>
          <Stagger as="ol" gap={0.14} className="relative m-0 grid list-none gap-10 p-0 md:grid-cols-4 md:gap-6">
            {STEPS.map(s => (
              <StaggerItem as="li" key={s.n} className="relative">
                <div className="relative z-10 flex gap-5 md:flex-col md:items-center md:text-center">
                  <span
                    aria-hidden
                    className="flex h-14 w-14 flex-none items-center justify-center rounded-full border border-white/15 bg-[#0b0b0d] text-[1.0625rem] font-semibold tabular-nums text-accent-300 shadow-[0_0_40px_-8px_rgba(34,211,238,0.5)]"
                  >
                    {s.n}
                  </span>
                  <div>
                    <h3 className="lp-title mb-2 !text-[1.375rem]">
                      <span className="sr-only">Step {Number(s.n)}: </span>
                      {s.title}
                    </h3>
                    <p className="m-0 text-small text-fg-muted">{s.body}</p>
                  </div>
                </div>
              </StaggerItem>
            ))}
          </Stagger>
        </div>

        <div className="mt-28 grid items-center gap-12 lg:mt-36 lg:grid-cols-5 lg:gap-16">
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
            <Reveal>
              <Eyebrow accent>Design review</Eyebrow>
            </Reveal>
            <h3 data-lp="split" className="lp-h2 mt-4 !text-[clamp(2rem,4vw,3.25rem)]">
              Every design decision, explained.
            </h3>
            <Reveal delay={0.25}>
              <p className="lp-lead mt-6">
                Before anything is written, QuarkClinical shows its reasoning: why patients instead of healthy
                volunteers, why multiple doses to steady state, which indication gives the cleanest PK comparison.
                You can change any of them, or enter your own indication and see how it compares.
              </p>
            </Reveal>
          </div>
        </div>
      </div>
    </section>
  )
}
