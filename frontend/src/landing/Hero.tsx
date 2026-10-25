import type { RefObject } from 'react'
import { Button, ButtonLink } from '../ui'
import { HeroComposer } from './HeroComposer'

/**
 * Near-black hero. Above-the-fold content is real text from the first render
 * (the motion engine only blurs / fades it in once loaded, and gives up and
 * shows it all if it cannot load). The live composer is the interactive
 * element; as the visitor scrolls on, the hero lifts and dims (scrubbed) into
 * the pinned story.
 */
export function Hero({
  onDemo,
  textareaRef,
}: {
  onDemo: () => void
  textareaRef: RefObject<HTMLTextAreaElement>
}) {
  return (
    <section id="top" aria-labelledby="hero-heading" className="lp-hero-bg relative overflow-hidden">
      <div aria-hidden className="lp-grid absolute inset-0" />
      <div
        aria-hidden
        data-lp="parallax"
        data-speed="0.18"
        className="lp-orb absolute -left-40 -top-24 h-[40rem] w-[40rem] rounded-full"
      />
      <div
        aria-hidden
        data-lp="parallax"
        data-speed="0.3"
        className="lp-orb lp-orb--teal absolute -right-40 top-24 h-[38rem] w-[38rem] rounded-full"
      />

      <div
        data-lp="hero-out"
        className="relative mx-auto max-w-6xl px-5 pb-24 pt-28 text-center sm:pt-32"
      >
        <div data-lp="fade">
          <p className="lp-glass inline-flex items-center gap-2 rounded-pill px-4 py-1.5 text-small font-medium text-fg">
            <span aria-hidden className="h-1.5 w-1.5 rounded-full bg-accent-300" />
            For biosimilar and bioequivalence study teams
          </p>
        </div>

        <h1 id="hero-heading" data-lp="split" className="lp-h1 mt-7">
          <span className="block text-fg">Study protocols and budgets,</span>{' '}
          <span className="block text-accent-300">built on computed evidence.</span>
        </h1>

        <p data-lp="fade" data-lp-delay={0.45} className="lp-lead mx-auto mt-6 max-w-2xl">
          Describe your study. Tested engines compute the sample size, sampling schedule and design decisions;
          QuarkClinical drafts the protocol and budget around numbers your team can verify.
        </p>

        <div data-lp="fade" data-lp-delay={0.6}>
          <HeroComposer textareaRef={textareaRef} />
        </div>

        <div
          data-lp="fade"
          data-lp-delay={0.75}
          className="mt-7 flex flex-col items-center justify-center gap-3 sm:flex-row"
        >
          <Button variant="primary" size="xl" onClick={onDemo}>
            Book a demo
          </Button>
          <ButtonLink variant="ghost" size="xl" href="#chat">
            See how it works
          </ButtonLink>
        </div>

        <p data-lp="fade" data-lp-delay={0.9} className="mt-6 text-small text-fg-subtle">
          Pilot programs available · Drafts for review by qualified experts · FDA, EMA and ICH guidance sets
        </p>
      </div>
    </section>
  )
}
