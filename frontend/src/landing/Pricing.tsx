import { Button } from '../ui'
import { PLANS } from './content'
import { Reveal, SectionHeading } from './Reveal'
import { Check } from './icons'

/** Three tiers, all "Price on request". Team is marked by an ink outline and
 *  a black primary pill; the others use secondary pills. Every button opens
 *  the contact modal with the matching interest chip pre-selected. */
export function Pricing({ onContact }: { onContact: (interest: string) => void }) {
  return (
    <section id="pricing" aria-labelledby="pricing-heading" className="scroll-mt-24 bg-surface py-24">
      <div className="mx-auto max-w-6xl px-5">
        <Reveal>
          <SectionHeading
            id="pricing-heading"
            eyebrow="Pricing"
            title="Plans for how your team works"
            lead="Priced per organization with seats and an included monthly usage allowance. Tell us about your studies and we will send a quote."
          />
        </Reveal>

        <ul className="m-0 mt-12 grid list-none items-stretch gap-5 p-0 md:grid-cols-3">
          {PLANS.map((p, i) => (
            <li key={p.name} className="flex">
              <Reveal delay={i * 90} className="flex w-full">
                <div
                  className={`flex w-full flex-col rounded-[24px] border-2 bg-surface p-6 sm:p-8 ${
                    p.featured ? 'border-ink-950' : 'border-line'
                  }`}
                >
                  <h3 className="text-h2 text-fg">{p.name}</h3>
                  <p className="mt-1 min-h-[2.5rem] text-small text-fg-muted">{p.blurb}</p>
                  <p className="mb-6 mt-4 text-[1.5rem] font-semibold tracking-[-0.02em] text-fg">Price on request</p>
                  <ul className="mb-8 flex flex-1 list-none flex-col gap-3 p-0">
                    {p.features.map(f => (
                      <li key={f} className="flex items-start gap-2.5 text-small text-fg-muted">
                        <Check aria-hidden className="mt-0.5 h-4 w-4 flex-none text-accent-700" />
                        <span>{f}</span>
                      </li>
                    ))}
                  </ul>
                  <Button
                    variant={p.featured ? 'primary' : 'secondary'}
                    size="lg"
                    fullWidth
                    onClick={() => onContact(p.interest)}
                    aria-label={`${p.cta}: ${p.name} plan`}
                  >
                    {p.cta}
                  </Button>
                </div>
              </Reveal>
            </li>
          ))}
        </ul>
      </div>
    </section>
  )
}
