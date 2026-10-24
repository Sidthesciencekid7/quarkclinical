import { Button, Surface } from '../ui'
import { Reveal } from './Reveal'

/** Dark call-to-action card. "Start with your study" scrolls back to the hero
 *  composer and focuses it (handled by the page). */
export function FinalCta({ onDemo, onStart }: { onDemo: () => void; onStart: () => void }) {
  return (
    <section id="cta" aria-labelledby="cta-heading" className="bg-surface py-24">
      <div className="mx-auto max-w-5xl px-5">
        <Reveal>
          <Surface
            tone="dark"
            className="lp-cta-bg relative overflow-hidden rounded-[28px] px-6 py-16 text-center sm:px-8"
          >
            <div aria-hidden className="lp-grid absolute inset-0 opacity-70" />
            <div className="relative">
              <h2
                id="cta-heading"
                className="text-[1.875rem] font-semibold leading-tight tracking-[-0.025em] text-fg sm:text-[2.5rem]"
              >
                See it on a study like yours
              </h2>
              <p className="mx-auto mt-4 max-w-xl text-[1.0625rem] leading-relaxed text-fg-muted">
                Pick a drug you are working on. We will build the protocol and budget with you in a live session.
              </p>
              <div className="mt-8 flex flex-col items-center justify-center gap-3 sm:flex-row">
                <Button variant="primary" size="xl" onClick={onDemo}>
                  Book a demo
                </Button>
                <Button variant="secondary" size="xl" onClick={onStart}>
                  Start with your study
                </Button>
              </div>
            </div>
          </Surface>
        </Reveal>
      </div>
    </section>
  )
}
