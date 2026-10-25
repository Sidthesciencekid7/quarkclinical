import { Button } from '../ui'
import { Reveal } from './Reveal'

/** Closing call to action. "Start with your study" scrolls back to the hero
 *  composer and focuses it (handled by the page). */
export function FinalCta({ onDemo, onStart }: { onDemo: () => void; onStart: () => void }) {
  return (
    <section id="cta" aria-labelledby="cta-heading" className="relative py-28 sm:py-36">
      <div className="mx-auto max-w-5xl px-5">
        <div data-lp="rise">
          <div className="lp-cta-bg relative overflow-hidden rounded-[32px] border border-white/10 px-6 py-20 text-center sm:px-10 sm:py-28">
            <div aria-hidden className="lp-grid absolute inset-0 opacity-60" />
            <div className="relative">
              <h2 id="cta-heading" data-lp="split" className="lp-h2">
                See it on a study like yours.
              </h2>
              <Reveal delay={0.3}>
                <p className="lp-lead mx-auto mt-6 max-w-xl">
                  Pick a drug you are working on. We will build the protocol and budget with you in a live session.
                </p>
              </Reveal>
              <Reveal delay={0.45}>
                <div className="mt-10 flex flex-col items-center justify-center gap-3 sm:flex-row">
                  <Button variant="primary" size="xl" onClick={onDemo}>
                    Book a demo
                  </Button>
                  <Button variant="secondary" size="xl" onClick={onStart}>
                    Start with your study
                  </Button>
                </div>
              </Reveal>
            </div>
          </div>
        </div>
      </div>
    </section>
  )
}
