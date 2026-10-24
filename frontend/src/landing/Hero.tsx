import type { RefObject } from 'react'
import { Button, ButtonLink, Surface } from '../ui'
import { HeroComposer } from './HeroComposer'

/**
 * Dark navy hero. Above-the-fold content renders visible immediately (no
 * scroll-reveal) so the first paint, link-preview renderers and crawlers all
 * see the message. No product picture here by request - the live composer is
 * the hero's interactive element.
 */
export function Hero({
  onDemo,
  textareaRef,
}: {
  onDemo: () => void
  textareaRef: RefObject<HTMLTextAreaElement>
}) {
  return (
    <Surface
      as="section"
      tone="dark"
      id="top"
      aria-labelledby="hero-heading"
      className="lp-hero-bg relative overflow-hidden"
    >
      <div aria-hidden className="lp-grid absolute inset-0" />
      <div
        aria-hidden
        className="blob absolute -left-24 -top-32 h-[36rem] w-[36rem] rounded-full motion-reduce:hidden"
        style={{ background: 'radial-gradient(circle, rgba(34,211,238,0.16), transparent 60%)' }}
      />
      <div
        aria-hidden
        className="blob absolute -right-24 top-10 h-[34rem] w-[34rem] rounded-full motion-reduce:hidden"
        style={{
          background: 'radial-gradient(circle, rgba(13,148,136,0.16), transparent 60%)',
          animationDelay: '-6s',
        }}
      />

      <div className="relative mx-auto max-w-5xl px-5 pb-16 pt-32 text-center sm:pt-36">
        <p className="lp-glass inline-flex items-center gap-2 rounded-pill px-4 py-1.5 text-small font-medium text-fg">
          <span aria-hidden className="h-1.5 w-1.5 rounded-full bg-accent-300" />
          For biosimilar and bioequivalence study teams
        </p>

        <h1
          id="hero-heading"
          className="mt-7 text-[2.5rem] font-semibold leading-[1.06] tracking-[-0.03em] sm:text-[3.25rem] md:text-display"
        >
          <span className="block text-fg">Study protocols and budgets,</span>{' '}
          <span className="text-gradient block">built on computed evidence.</span>
        </h1>

        <p className="mx-auto mt-6 max-w-2xl text-[1.0625rem] leading-relaxed text-fg-muted sm:text-lg">
          Describe your study. Tested engines compute the sample size, sampling schedule and design decisions;
          QuarkClinical drafts the protocol and budget around numbers your team can verify.
        </p>

        <HeroComposer textareaRef={textareaRef} />

        <div className="mt-8 flex flex-col items-center justify-center gap-3 sm:flex-row">
          <Button variant="secondary" size="xl" onClick={onDemo}>
            Book a demo
          </Button>
          <ButtonLink variant="ghost" size="xl" href="#how">
            See how it works
          </ButtonLink>
        </div>

        <p className="mt-6 text-small text-fg-subtle">
          Pilot programs available · Drafts for review by qualified experts · FDA, EMA and ICH guidance sets
        </p>
      </div>
      <div aria-hidden className="relative h-20 bg-gradient-to-b from-transparent to-ink-50" />
    </Surface>
  )
}
