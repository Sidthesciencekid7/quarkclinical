import { Button } from '../ui'
import { PLANS } from './content'
import { SectionHeading, Stagger, StaggerItem } from './Reveal'
import { Check } from './icons'

/** Three tiers, all "Price on request". Team is marked by a cyan outline and a
 *  white primary pill; the others use glass secondary pills. Every button opens
 *  the contact modal with the matching interest chip pre-selected. */
export function Pricing({ onContact }: { onContact: (interest: string) => void }) {
  return (
    <section id="pricing" aria-labelledby="pricing-heading" className="relative scroll-mt-20 py-28 sm:py-36">
      <div aria-hidden className="lp-hair absolute inset-x-0 top-0" />
      <div className="mx-auto max-w-6xl px-5">
        <SectionHeading
          id="pricing-heading"
          eyebrow="Pricing"
          title="Plans for how your team works."
          lead="Priced per organization with seats and an included monthly usage allowance. Tell us about your studies and we will send a quote."
        />

        <Stagger as="ul" className="m-0 mt-16 grid list-none items-stretch gap-5 p-0 md:grid-cols-3">
          {PLANS.map(p => (
            <StaggerItem as="li" key={p.name} className="flex">
              <div
                className={`lp-card flex w-full flex-col p-7 sm:p-8 ${
                  p.featured
                    ? '!border-accent-300/50 shadow-[0_0_80px_-30px_rgba(34,211,238,0.45),inset_0_1px_0_rgba(255,255,255,0.08)]'
                    : ''
                }`}
              >
                <h3 className="lp-title !text-[1.625rem]">{p.name}</h3>
                <p className="m-0 mt-2 min-h-[2.5rem] text-small text-fg-muted">{p.blurb}</p>
                <p className="mb-7 mt-5 text-[1.625rem] font-semibold tracking-[-0.03em] text-fg">Price on request</p>
                <ul className="mb-9 flex flex-1 list-none flex-col gap-3.5 p-0">
                  {p.features.map(f => (
                    <li key={f} className="flex items-start gap-2.5 text-small text-fg-muted">
                      <Check aria-hidden className="mt-0.5 h-4 w-4 flex-none text-accent-300" />
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
            </StaggerItem>
          ))}
        </Stagger>
      </div>
    </section>
  )
}
